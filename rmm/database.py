"""RimSort community databases -> local SQLite (+FTS5): fetch with fallbacks, search and query.

Sources (each updated independently, one failing never blocks the others):
  steamDB  (RimSort Steam-Workshop-Database)  codeload zip -> raw JSON -> jsDelivr -> local RimSort copy -> keep existing
  rules    (RimSort Community-Rules-Database) raw -> jsDelivr -> local RimSort copy -> keep existing
  UTI      (emipa606 UseThisInstead)          raw -> jsDelivr -> local RimSort copy -> keep existing
  NVW      (emipa606 NoVersionWarning)        raw (per version) -> jsDelivr -> local RimSort copy -> keep existing
  Steam    (Steam Web API QueryFiles, needs the user's key)  mods changed since the steamDB snapshot, merged in
  packs    (Steam Web API QueryFiles, needs the user's key)  player-made Workshop collections: which mods go together
"""
from __future__ import annotations

import gc
import gzip
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from .models import DLC_BY_APPID, JUNK_PACKAGE_IDS, DbMod, Progress, compat_versions, noop_progress, short_version

log = logging.getLogger("rmm.database")

STEAMDB_ZIP = "https://codeload.github.com/RimSort/Steam-Workshop-Database/zip/refs/heads/main"
STEAMDB_RAW = "https://raw.githubusercontent.com/RimSort/Steam-Workshop-Database/main/steamDB.json"
STEAMDB_CDN = "https://cdn.jsdelivr.net/gh/RimSort/Steam-Workshop-Database@main/steamDB.json"
RULES_URLS = [("raw", "https://raw.githubusercontent.com/RimSort/Community-Rules-Database/main/communityRules.json"),
              ("jsDelivr", "https://cdn.jsdelivr.net/gh/RimSort/Community-Rules-Database@main/communityRules.json")]
UTI_URLS = [("raw", "https://raw.githubusercontent.com/emipa606/UseThisInstead/main/replacements.json.gz"),
            ("jsDelivr", "https://cdn.jsdelivr.net/gh/emipa606/UseThisInstead@main/replacements.json.gz")]
NVW_URLS = [("raw", "https://raw.githubusercontent.com/emipa606/NoVersionWarning/main/{}/ModIdsToFix.xml"),
            ("jsDelivr", "https://cdn.jsdelivr.net/gh/emipa606/NoVersionWarning@main/{}/ModIdsToFix.xml")]
NVW_VERSIONS = [f"1.{i}" for i in range(10)]
QUERY_FILES_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
_SYNC_MAX_PAGES = 300        # 30k changed mods: past that a fresh steamDB snapshot is the better source
_PACKS_DAYS = 365            # first collections sync: the ones updated in the last year (~25k, the 1.6 era)
_PACKS_MAX_PAGES = 600       # backstop: 60k collections

_SCHEMA_VERSION = 1
_DETAILS_KEEP_S = 30 * 86400
_W_NAME, _W_STEAM, _W_AUTHORS, _W_PID = 10.0, 8.0, 1.5, 4.0   # bm25 column weights (name favoured)
_W_POP = 0.6                                                  # popularity (dependents) boost in relevance

_ENTRY_KEYS = {"url", "steamName", "packageId", "packageid", "name", "authors", "gameVersions", "tags",
               "dependencies", "unpublished", "appid"}
_PID_RE = re.compile(r"[a-z0-9_\-]+(?:\.[a-z0-9_\-]+)+")
_WID_RES = (re.compile(r"(\d{5,})"), re.compile(r".*?[?&]id=(\d{5,}).*"), re.compile(r"steam://\S*?/(\d{5,})/?"))

_COLS = ("m.workshop_id, m.package_id, m.name, m.steam_name, m.authors, m.versions, m.deps, m.unpublished, m.is_dlc, "
         "d.subscriptions, d.time_updated, d.data, m.kind")
_FROM = "mods m LEFT JOIN steam_details d ON d.workshop_id = m.workshop_id"
_NAME_KEY = "lower(COALESCE(NULLIF(m.name, ''), NULLIF(m.steam_name, ''), m.workshop_id))"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS mods(
    workshop_id TEXT PRIMARY KEY, package_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
    steam_name TEXT NOT NULL DEFAULT '', authors TEXT NOT NULL DEFAULT '', versions TEXT NOT NULL DEFAULT '',
    latest INTEGER NOT NULL DEFAULT 0, deps TEXT NOT NULL DEFAULT '', unpublished INTEGER NOT NULL DEFAULT 0,
    is_dlc INTEGER NOT NULL DEFAULT 0, dependents INTEGER NOT NULL DEFAULT 0, pop REAL NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS mods_pid ON mods(package_id);
CREATE TABLE IF NOT EXISTS rules(package_id TEXT NOT NULL, kind TEXT NOT NULL, other TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '', PRIMARY KEY(package_id, kind, other)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS uti(old_wid TEXT, old_pid TEXT, new_wid TEXT, new_pid TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS uti_wid ON uti(old_wid);
CREATE INDEX IF NOT EXISTS uti_pid ON uti(old_pid);
CREATE TABLE IF NOT EXISTS nvw(version TEXT NOT NULL, package_id TEXT NOT NULL, PRIMARY KEY(version, package_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS steam_details(workshop_id TEXT PRIMARY KEY, fetched_at REAL NOT NULL,
    subscriptions INTEGER NOT NULL DEFAULT 0, time_updated INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS packs(cid INTEGER PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
    descr TEXT NOT NULL DEFAULT '', updated INTEGER NOT NULL DEFAULT 0, n INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS pack_mods(cid INTEGER NOT NULL, wid INTEGER NOT NULL, PRIMARY KEY(cid, wid)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS pack_mods_wid ON pack_mods(wid);
"""
_FTS_SCHEMA = ("CREATE VIRTUAL TABLE IF NOT EXISTS mods_fts USING fts5(name, steam_name, authors, package_id, "
               "content='mods', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2')")
_PACKS_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS packs_fts USING fts5(title, descr, content='packs', "
              "content_rowid='cid', tokenize='unicode61 remove_diacritics 2')")


class _Cancelled(Exception):
    pass


# ---------------------------------------------------------------- small helpers (module level so tests can patch I/O)
def _abortable(fn, cancel):
    """fn() on a helper thread, polling cancel every 0.25 s: a stalled connection (socket timeouts are 20-60 s) must
    not keep Cancel waiting. On cancel the thread is abandoned (daemon; it ends at its own timeout)."""
    if cancel is None:
        return fn()
    box: dict = {}

    def work():
        try:
            box["ok"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            box["err"] = e

    t = threading.Thread(target=work, name="rmm-db-http", daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.25)
        if _cancelled(cancel):
            raise _Cancelled
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _http_get(url: str, timeout: float = 30, headers: dict | None = None, cancel=None) -> bytes:
    from . import config
    return _abortable(lambda: config.http_get(url, timeout=timeout, headers=headers), cancel)


def _download(url: str, dest: str, progress: Progress, cancel) -> str:
    from . import config
    return _abortable(lambda: config.download_file(url, dest, progress=progress, timeout=60, cancel=cancel), cancel)


def _rimsort_dir() -> str:
    from . import paths
    d = paths.rimsort_dbs_dir()
    return os.path.dirname(d) if d else \
        os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"), "RimSort")


def _local_files(rel: str, settings_key: str = "") -> list[str]:
    """Existing local RimSort copies of a DB file: the path configured in RimSort's settings.json, then its dbs/ folder."""
    base = _rimsort_dir()
    cands = []
    if settings_key:
        try:
            with open(os.path.join(base, "settings.json"), encoding="utf-8-sig") as f:
                p = json.load(f).get(settings_key)
            if isinstance(p, str) and p.strip():
                cands.append(os.path.expandvars(p.strip()))
        except (OSError, ValueError, AttributeError):
            pass
    cands.append(os.path.join(base, "dbs", *rel.split("/")))
    out, seen = [], set()
    for p in cands:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen and os.path.isfile(p):
            seen.add(key)
            out.append(p)
    return out


def _cancelled(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _sub(progress: Progress, a: float, b: float) -> Progress:
    return lambda f=None, t="": progress(None if f is None else a + (b - a) * max(0.0, min(1.0, f)), t)


def _err(e: BaseException) -> str:
    s = str(e) or type(e).__name__
    return s if len(s) < 160 else s[:157] + "..."


def _is_404(e: BaseException) -> bool:
    return getattr(e, "code", None) == 404 or getattr(e, "status", None) == 404


def _pid(value) -> str:
    """Lowercased package id; '' for junk placeholders."""
    p = str(value or "").strip().lower()
    return "" if p in JUNK_PACKAGE_IDS else p


def _text(v) -> str:
    if isinstance(v, (list, tuple)):
        return "; ".join(str(x).strip() for x in v if str(x).strip())
    return "" if v is None else str(v).strip()


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _loads(raw: bytes):
    """JSON from bytes that may be gzip-compressed and/or carry a UTF-8 BOM."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8-sig"))


def _newer(v: str, stored: str) -> bool:
    """Version/timestamp comparison: numeric when both are numbers (unix timestamps), else string (ISO dates)."""
    if v.isdigit() and stored.isdigit():
        return int(v) > int(stored)
    return v > stored


def _peek_version(head: bytes) -> str | None:
    """steamDB 'version' from the first KBs of the file (it is the first key) without parsing 49 MB."""
    m = re.search(rb'"version"\s*:\s*"?(\d+)', head.split(b'"database"', 1)[0])
    return m.group(1).decode() if m else None


def _entry_row(o: dict, dependents: Counter) -> tuple:
    """steamDB entry -> (package_id, name, steam_name, authors, versions, latest, deps, unpublished, is_dlc, kind)."""
    tags = [t for t in (o.get("tags") or ()) if isinstance(t, str)]
    vers = set()
    for v in [*(o.get("gameVersions") or ()), *tags]:
        s = short_version(str(v))
        if s:
            vers.add(s)
    ordered = sorted(vers, key=lambda s: tuple(map(int, s.split("."))))
    latest = max((int(a) * 1000 + int(b) for a, b in (s.split(".") for s in ordered)), default=0)
    deps_in = o.get("dependencies")
    deps = {}
    if isinstance(deps_in, dict):
        for k, v in deps_in.items():
            k = str(k).strip()
            if k:
                deps[k] = str(v[0]) if isinstance(v, (list, tuple)) and v else ("" if isinstance(v, (list, tuple)) else str(v))
                dependents[k] += 1
    authors = o.get("authors") or ""
    if isinstance(authors, (list, tuple)):
        authors = ", ".join(map(str, authors))
    kind = "translation" if "Translation" in tags else "scenario" if "Scenario" in tags else ""
    return (_pid(o.get("packageId") or o.get("packageid")), str(o.get("name") or ""), str(o.get("steamName") or ""),
            str(authors), "|" + "|".join(ordered) + "|" if ordered else "", latest,
            json.dumps(deps, ensure_ascii=False, separators=(",", ":")) if deps else "",
            1 if o.get("unpublished") else 0, 1 if o.get("appid") else 0, kind)


def _parse_steam(fp) -> tuple[str, dict, Counter]:
    """Parse steamDB.json from a binary stream. Entries are compacted to tuples while parsing (keeps peak RAM low)."""
    dependents: Counter = Counter()

    def hook(o: dict):
        if len(o) == 2 and "tag" in o and "display_name" in o:
            return o["tag"]
        if "database" not in o and not _ENTRY_KEYS.isdisjoint(o):
            return _entry_row(o, dependents)
        return o

    data = json.load(fp, object_hook=hook)
    db = data.get("database") if isinstance(data, dict) else None
    if not isinstance(db, dict):
        raise ValueError("not a steamDB.json (no 'database' object)")
    return str(data.get("version") or ""), db, dependents


@contextmanager
def _open_steamdb(path: str):
    """Binary stream of steamDB.json from a .zip (read in place, never extracted), .gz or plain file."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith("steamdb.json")] or \
                    [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not names:
                raise ValueError("no steamDB.json inside the zip")
            with zf.open(min(names, key=len)) as fp:
                yield fp
        return
    with open(path, "rb") as f:
        if f.read(2) == b"\x1f\x8b":
            f.seek(0)
            with gzip.GzipFile(fileobj=f) as g:
                yield g
        else:
            f.seek(0)
            yield f


def _peek_file_version(path: str) -> str | None:
    try:
        with _open_steamdb(path) as fp:
            return _peek_version(fp.read(4096))
    except (OSError, ValueError, zipfile.BadZipFile):
        return None


def _parse_nvw(raw: bytes) -> set[str]:
    text = raw.decode("utf-8-sig", "replace")
    if "<ModIdsToFix" not in text:
        raise ValueError("not a ModIdsToFix.xml")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return {p for p in (_pid(m) for m in re.findall(r"<li>\s*([^<]*?)\s*</li>", text)) if p}


def _rules_rows(rules: dict):
    for key, r in rules.items():
        p = _pid(key)
        if not p or not isinstance(r, dict):
            continue
        for src, kind in (("loadAfter", "load_after"), ("loadBefore", "load_before"), ("incompatibleWith", "incompatible_with")):
            entries = r.get(src)
            if isinstance(entries, dict):
                for other, info in entries.items():
                    o = _pid(other)
                    if o and o != p:
                        info = info if isinstance(info, dict) else {}
                        yield p, kind, o, _text(info.get("name")), _text(info.get("comment"))
        for src, kind in (("loadTop", "load_top"), ("loadBottom", "load_bottom")):
            v = r.get(src)
            if v is True or (isinstance(v, dict) and v.get("value")):
                yield p, kind, "", "", _text(v.get("comment")) if isinstance(v, dict) else ""


def _tokens(q: str) -> list[str]:
    return [t for t in q.split() if any(ch.isalnum() for ch in t)]


def _like(t: str) -> str:
    return "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _as_workshop_id(q: str) -> str:
    for rx in _WID_RES:
        m = rx.fullmatch(q)
        if m:
            return m.group(1)
    return ""


def _dbmod(r) -> DbMod:
    data = {}
    if r[11]:
        try:
            data = json.loads(r[11])
        except ValueError:
            pass
    return DbMod(workshop_id=r[0], package_id=r[1], name=r[2], steam_name=r[3], authors=r[4],
                 game_versions=[v for v in r[5].split("|") if v], dependencies=json.loads(r[6]) if r[6] else {},
                 unpublished=bool(r[7]), is_dlc=bool(r[8]), subscriptions=_int(r[9]), time_updated=_int(r[10]),
                 description=str(data.get("description") or ""), preview_url=str(data.get("preview_url") or ""), kind=r[12])


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------- database
class ModDatabase:
    def __init__(self, path: str | None = None):
        if path is None:
            from . import config
            path = config.app_path("moddb.sqlite")
        self.path = path
        self._lock = threading.RLock()
        self._update_lock = threading.Lock()
        self._last_summary = ""
        self._fts = False
        self._net_ok = False
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        try:
            self._con = self._connect()
        except sqlite3.DatabaseError as e:        # corrupt file: move it aside and start fresh
            if path == ":memory:" or isinstance(e, sqlite3.OperationalError):
                raise                             # 'database is locked' (another instance) etc. is not corruption
            log.warning("Mod database %s unreadable (%s); recreating", path, e)
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.replace(path + suffix, path + suffix + ".corrupt")
                except OSError:
                    pass
            self._con = self._connect()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=15)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            uv = con.execute("PRAGMA user_version").fetchone()[0]
            if uv not in (0, _SCHEMA_VERSION):
                for t in ("mods_fts", "mods", "rules", "uti", "nvw", "steam_details", "meta"):
                    con.execute(f"DROP TABLE IF EXISTS {t}")
            con.executescript(_SCHEMA)
            if "kind" not in {c[1] for c in con.execute("PRAGMA table_info(mods)")}:
                con.execute("ALTER TABLE mods ADD COLUMN kind TEXT NOT NULL DEFAULT ''")
                # the next update (due now) re-imports steamDB, which fills kind
                con.execute("DELETE FROM meta WHERE key IN ('steam_version', 'updated_at')")
            try:
                con.execute(_FTS_SCHEMA)
                con.execute(_PACKS_FTS)
                self._fts = True
            except sqlite3.OperationalError as e:   # SQLite built without FTS5 -> LIKE search only
                log.warning("FTS5 unavailable (%s); using LIKE search", e)
            if uv != _SCHEMA_VERSION:     # a write: only when needed, so opening never waits on another instance's rebuild
                con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        except BaseException:
            con.close()
            raise
        return con

    def close(self) -> None:
        # a rebuild (cancelled at app exit) can hold the lock for seconds: don't freeze the closing window for it;
        # the process exit then drops the connection and SQLite rolls the unfinished transaction back
        if self._lock.acquire(timeout=2):
            try:
                self._con.close()
            finally:
                self._lock.release()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                yield self._con
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def _meta(self, key: str, default: str = "") -> str:
        with self._lock:
            r = self._con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r and r[0] is not None else default

    @staticmethod
    def _set_meta(con: sqlite3.Connection, **kv) -> None:
        con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", [(k, str(v)) for k, v in kv.items()])

    def _count(self, sql: str) -> int:
        with self._lock:
            return self._con.execute(sql).fetchone()[0]

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        m = lambda k: self._meta(k) or "none"
        return {
            "steam_count": self._count("SELECT COUNT(*) FROM mods"),
            "steam_version": self._meta("steam_version"),
            "rules_count": self._count("SELECT COUNT(DISTINCT package_id) FROM rules"),
            "rules_timestamp": _int(self._meta("rules_timestamp")),
            "uti_count": self._count("SELECT COUNT(*) FROM uti"),
            "uti_version": self._meta("uti_version"),
            "nvw_count": self._count("SELECT COUNT(*) FROM nvw"),
            "packs_count": self._count("SELECT COUNT(*) FROM packs"),
            "updated_at": float(self._meta("updated_at") or 0),
            "source": f"steamDB: {m('src_steam')}; rules: {m('src_rules')}; UTI: {m('src_uti')}; NVW: {m('src_nvw')}",
            "fts": self._fts,
        }

    def is_empty(self) -> bool:
        with self._lock:
            return self._con.execute("SELECT 1 FROM mods LIMIT 1").fetchone() is None

    def needs_update(self, max_age_days: float) -> bool:
        """True when empty, or when the last successful online update is older than max_age_days (<= 0 disables)."""
        if self.is_empty():
            return True
        if not max_age_days or max_age_days <= 0:
            return False
        return time.time() - float(self._meta("updated_at") or 0) > max_age_days * 86400

    # ------------------------------------------------------------ update
    def update(self, progress: Progress = noop_progress, cancel=None, force: bool = False, steam_key: str = "") -> str:
        """A call that finds another update running waits for it (cancel stops the wait) and, unless force, returns
        that update's summary instead of downloading everything again."""
        got = self._update_lock.acquire(blocking=False)
        waited = not got
        if waited:
            progress(None, "Waiting for the mod database update that is already running...")
        while not got and not _cancelled(cancel):
            got = self._update_lock.acquire(timeout=0.25)
        summary = "Mod database: cancelled while waiting for the running update"
        if got:
            try:
                summary = self._last_summary = \
                    self._last_summary if waited and not force else self._run_update(progress, cancel, force, steam_key)
            finally:
                self._update_lock.release()
        if self.is_empty():
            raise RuntimeError("The mod database is still empty after trying every source:\n" + summary)
        return summary

    def _run_update(self, progress: Progress, cancel, force: bool, steam_key: str = "") -> str:
        t0 = time.time()
        self._net_ok = False
        lines = []
        steps = (("Steam Workshop DB", self._update_steam, 0.0, 0.7),
                 *((("Steam sync", lambda p, c, f: self._sync_steam(steam_key, p, c), 0.7, 0.75),
                    ("Workshop collections", lambda p, c, f: self._sync_packs(steam_key, p, c), 0.75, 0.8))
                   if steam_key else ()),
                 ("Community rules", self._update_rules, 0.8, 0.88),
                 ("Use This Instead", self._update_uti, 0.88, 0.93), ("No Version Warning", self._update_nvw, 0.93, 1.0))
        for label, fn, a, b in steps:
            if _cancelled(cancel):
                lines.append(f"{label}: cancelled")
                continue
            sub = _sub(progress, a, b)
            sub(0.0, f"{label}: checking...")
            try:
                msg = fn(sub, cancel, force)
            except _Cancelled:
                msg = "cancelled"
            except Exception as e:
                log.exception("%s update failed", label)
                msg = f"failed ({_err(e)}); kept existing"
            log.info("%s: %s", label, msg)
            lines.append(f"{label}: {msg}")
        with self._tx() as con:
            con.execute("DELETE FROM steam_details WHERE fetched_at < ?", (time.time() - _DETAILS_KEEP_S,))
            if self._net_ok and not _cancelled(cancel):
                self._set_meta(con, updated_at=time.time())
        lines.append(f"Finished in {time.time() - t0:.1f}s")
        progress(1.0, "Mod database cancelled" if _cancelled(cancel) else "Mod database up to date")
        return "\n".join(lines)

    def _update_steam(self, progress: Progress, cancel, force: bool) -> str:
        stored = self._meta("steam_version")
        have = not self.is_empty()
        unchanged = lambda v: have and not force and v is not None and v == stored
        fails = []
        for label, url in (("codeload zip", STEAMDB_ZIP), ("raw JSON", STEAMDB_RAW), ("jsDelivr", STEAMDB_CDN)):
            if _cancelled(cancel):
                raise _Cancelled
            tmpdir = tempfile.mkdtemp(prefix="rmm-db-")
            try:
                if url != STEAMDB_ZIP:        # cheap "conditional GET": read the version from the first 4 KB
                    head = _http_get(url, timeout=30, headers={"Range": "bytes=0-4095"}, cancel=cancel)
                    v = _peek_version(head[:4096])
                    if unchanged(v):
                        self._net_ok = True
                        return f"up to date (v{v}, checked via {label})"
                dest = os.path.join(tmpdir, "steamDB.zip" if url == STEAMDB_ZIP else "steamDB.json")
                _download(url, dest, _sub(progress, 0.0, 0.35), cancel)
                v = _peek_file_version(dest)
                if unchanged(v):
                    self._net_ok = True
                    return f"up to date (v{v}, checked via {label})"
                n = self._import_steam_path(dest, label, _sub(progress, 0.35, 1.0), cancel, min_rows=1000)
                self._net_ok = True
                return f"{n:,} mods from {label} (v{self._meta('steam_version')})"
            except _Cancelled:
                raise
            except Exception as e:
                if _cancelled(cancel):
                    raise _Cancelled from e
                log.warning("steamDB via %s failed: %s", label, e)
                fails.append(f"{label}: {_err(e)}")
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        failed = "network failed (" + "; ".join(fails) + ")"
        local = [(_int(_peek_file_version(p)), p) for p in
                 _local_files("Steam-Workshop-Database/steamDB.json", "external_steam_metadata_file_path")]
        if local:
            v, p = max(local, key=lambda c: c[0])
            if have and not force and v <= _int(stored):
                return f"{failed}; kept existing v{stored} (local RimSort copy v{v} is not newer)"
            try:
                n = self._import_steam_path(p, "local RimSort copy", progress, cancel, min_rows=1000)
                return f"{failed}; {n:,} mods from local RimSort copy {p} (v{v})"
            except _Cancelled:
                raise
            except Exception as e:
                log.warning("local steamDB %s failed: %s", p, e)
                failed += f"; local copy: {_err(e)}"
        return f"{failed}; kept existing" if have else f"{failed}; no local RimSort copy found"

    def _import_steam_path(self, path: str, source: str, progress: Progress, cancel, min_rows: int) -> int:
        progress(0.05, "Parsing steamDB.json...")
        with _open_steamdb(path) as fp:
            version, db, dependents = _parse_steam(fp)
        if len(db) < min_rows:
            raise ValueError(f"steamDB has only {len(db)} entries")
        if _cancelled(cancel):
            raise _Cancelled
        try:
            return self._store_steam(version, db, dependents, source, _sub(progress, 0.2, 1.0), cancel)
        finally:
            del db, dependents
            gc.collect()

    def _store_steam(self, version: str, db: dict, dependents: Counter, source: str, progress: Progress, cancel) -> int:
        total = max(len(db), 1)
        count = 0

        def rows():
            nonlocal count
            for i, (wid, r) in enumerate(db.items()):
                if i % 5000 == 0:
                    if _cancelled(cancel):
                        raise _Cancelled
                    progress(0.8 * i / total, f"Importing mods {i:,}/{total:,}")
                wid = str(wid).strip()
                if isinstance(r, dict):
                    r = _entry_row(r, Counter())
                if not wid or not isinstance(r, tuple):
                    continue
                n = dependents.get(wid, 0)
                count += 1
                pop = math.log1p(n) - (1.5 if r[9] else 0.0)     # translations/scenarios rank below mods
                yield (wid, *r[:9], n, round(pop, 4), r[9])

        with self._tx() as con:
            con.execute("DELETE FROM mods")
            con.executemany("INSERT OR REPLACE INTO mods(workshop_id, package_id, name, steam_name, authors, versions, latest, "
                            "deps, unpublished, is_dlc, dependents, pop, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows())
            if self._fts:
                progress(0.85, "Building search index...")
                con.execute("INSERT INTO mods_fts(mods_fts) VALUES('rebuild')")
            self._set_meta(con, steam_version=version, src_steam=source)
            con.execute("DELETE FROM meta WHERE key='steam_sync_at'")    # the snapshot replaced every synced row
        progress(1.0, f"Imported {count:,} mods")
        return count

    def _sync_steam(self, key: str, progress: Progress, cancel) -> str:
        """Merge the Workshop items changed since the steamDB snapshot (or the last sync) straight from Steam's
        QueryFiles, newest first: titles, version tags, required items, subscribers. packageId, About.xml name and
        authors stay as steamDB had them ('' for mods newer than the snapshot)."""
        since = max(_int(self._meta("steam_version")), _int(self._meta("steam_sync_at"))) - 86400   # 1 day overlap
        if since <= 0:
            return "skipped (no steamDB snapshot yet)"
        items: dict[str, dict] = {}
        cursor, first = "*", 0
        for _ in range(_SYNC_MAX_PAGES):
            if _cancelled(cancel):
                raise _Cancelled
            q = {"key": key, "appid": 294100, "query_type": 21, "numperpage": 100, "cursor": cursor, "filetype": 0,
                 "return_tags": 1, "return_children": 1, "return_short_description": 1}   # 21 = by last update
            resp = json.loads(_http_get(QUERY_FILES_URL + "?" + urllib.parse.urlencode(q), timeout=60,
                                        cancel=cancel)).get("response") or {}
            batch = resp.get("publishedfiledetails") or []
            done = not batch
            for d in batch:
                t = _int(d.get("time_updated"))
                if t < since:
                    done = True
                    break
                first = first or t
                if d.get("result") == 1 and str(d.get("publishedfileid", "")).isdigit():
                    items.setdefault(str(d["publishedfileid"]), d)
            if first > since:
                progress(min(0.9, (first - t) / (first - since)), f"Steam sync: {len(items):,} changed mods...")
            cursor = resp.get("next_cursor")
            if done or not cursor:
                break
        new = self._store_sync(items, first)
        self._net_ok = True
        return f"{len(items):,} mods changed since {time.strftime('%Y-%m-%d', time.gmtime(since + 86400))} ({new:,} new)"

    def store_items(self, items: dict[str, dict]) -> int:
        """Upsert Steam PublishedFileDetails (the AI generator's live Workshop finds) like a sync, without moving the
        sync point. Returns how many are new."""
        return self._store_sync(items, None)

    def _store_sync(self, items: dict[str, dict], newest: int | None) -> int:
        """Upsert synced items over the steamDB rows; returns how many were not in the database yet."""
        if not items:
            return 0
        ids, old = list(items), {}
        with self._lock:
            for chunk in _chunks(ids, 500):
                old.update((r[0], r) for r in self._con.execute(
                    "SELECT workshop_id, package_id, name, authors, versions, deps, dependents, unpublished FROM mods "
                    f"WHERE workshop_id IN ({','.join('?' * len(chunk))})", chunk))
        rows, details = [], {}
        for wid, d in items.items():
            o = old.get(wid)
            old_deps = json.loads(o[5]) if o and o[5] else {}
            deps = {k: v for k, v in old_deps.items() if k in DLC_BY_APPID}     # DLCs: Steam lists only workshop items
            for c in d.get("children") or ():
                cid = str(c.get("publishedfileid", "")) if isinstance(c, dict) else ""
                if cid.isdigit():
                    deps[cid] = old_deps.get(cid, "")
            tags = [t.get("tag") for t in d.get("tags") or () if isinstance(t, dict) and isinstance(t.get("tag"), str)]
            title = str(d.get("title") or "")
            r = _entry_row({"packageId": o[1] if o else "", "name": o[2] if o else "", "steamName": title,
                            "authors": o[3] if o else "", "tags": tags + (o[4].split("|") if o else []),
                            "dependencies": deps}, Counter())
            n = o[6] if o else 0
            rows.append((wid, *r[:7], o[7] if o else 0, r[8], n, round(math.log1p(n) - (1.5 if r[9] else 0.0), 4), r[9]))
            details[wid] = {"publishedfileid": wid, "result": 1, "consumer_app_id": 294100, "title": title,
                            "subscriptions": _int(d.get("subscriptions")), "time_updated": _int(d.get("time_updated")),
                            "file_size": d.get("file_size"), "banned": _int(d.get("banned")),
                            "description": str(d.get("short_description") or ""), "preview_url": d.get("preview_url") or ""}
        with self._tx() as con:
            con.executemany("INSERT OR REPLACE INTO mods(workshop_id, package_id, name, steam_name, authors, versions, latest, "
                            "deps, unpublished, is_dlc, dependents, pop, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            if self._fts:
                con.execute("INSERT INTO mods_fts(mods_fts) VALUES('rebuild')")
            if newest is not None:
                self._set_meta(con, steam_sync_at=newest)
        self.cache_details(details)
        return len(items) - len(old)

    def _sync_packs(self, key: str, progress: Progress, cancel) -> str:
        """Player-made Workshop collections changed since the last sync (the last year on the first run), newest
        first: title, short description and member mods - the AI generator's 'similar packs'. Pages are fetched 4
        at a time; a collection updated meanwhile shifts the order a little, which only repeats one (stored once)."""
        last = _int(self._meta("packs_sync_at"))
        since = last - 86400 if last else int(time.time()) - _PACKS_DAYS * 86400

        def page(n: int) -> list[dict]:
            q = {"key": key, "appid": 294100, "query_type": 21, "filetype": 1, "numperpage": 100, "page": n,
                 "return_children": 1, "return_short_description": 1}                 # 21 = by last update
            resp = json.loads(_http_get(QUERY_FILES_URL + "?" + urllib.parse.urlencode(q), timeout=90,
                                        cancel=cancel)).get("response") or {}
            return resp.get("publishedfiledetails") or []

        stored, newest, n = 0, 0, 1
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                while n <= _PACKS_MAX_PAGES:
                    if _cancelled(cancel):
                        raise _Cancelled
                    batches = list(pool.map(page, range(n, n + 4)))
                    n += 4
                    times = [_int(d.get("time_updated")) for b in batches for d in b]
                    newest = newest or max(times, default=0)
                    stored += self._store_packs([d for b in batches for d in b if _int(d.get("time_updated")) >= since])
                    oldest = min(times, default=0)
                    if newest > since:
                        progress(min(0.95, (newest - oldest) / (newest - since)), f"Workshop collections: {stored:,}...")
                    if not all(batches) or oldest < since:
                        break
        finally:
            if stored and self._fts:
                with self._tx() as con:
                    con.execute("INSERT INTO packs_fts(packs_fts) VALUES('rebuild')")
        with self._tx() as con:
            self._set_meta(con, packs_sync_at=newest or int(time.time()))
        self._net_ok = True
        return f"{stored:,} collections changed since {time.strftime('%Y-%m-%d', time.gmtime(max(0, since)))}"

    def _store_packs(self, items: list[dict]) -> int:
        """Upsert collections with 5-1500 mods (nested collections and guides left out); returns how many."""
        rows, members = [], []
        for d in items:
            cid = _int(d.get("publishedfileid"))
            kids = {_int(c.get("publishedfileid")) for c in d.get("children") or ()
                    if isinstance(c, dict) and not _int(c.get("file_type"))} - {0}
            if d.get("result", 1) != 1 or not cid or not 5 <= len(kids) <= 1500:
                continue
            rows.append((cid, _text(d.get("title"))[:200], _text(d.get("short_description"))[:500],
                         _int(d.get("time_updated")), len(kids)))
            members += [(cid, w) for w in kids]
        if rows:
            with self._tx() as con:
                for chunk in _chunks([r[0] for r in rows], 500):
                    con.execute(f"DELETE FROM pack_mods WHERE cid IN ({','.join('?' * len(chunk))})", chunk)
                con.executemany("INSERT OR REPLACE INTO packs(cid, title, descr, updated, n) VALUES (?,?,?,?,?)", rows)
                con.executemany("INSERT OR IGNORE INTO pack_mods(cid, wid) VALUES (?,?)", members)
        return len(rows)

    def _update_simple(self, what: str, urls: list, local_rel: str, settings_key: str, meta_key: str,
                       store, count_sql: str, progress: Progress, cancel, force: bool) -> str:
        """Shared flow for the small JSON sources (rules, UTI): network URLs -> local RimSort copy -> keep existing."""
        stored = self._meta(meta_key)
        have = self._count(count_sql) > 0
        fails = []
        for i, (label, url) in enumerate(urls):
            if _cancelled(cancel):
                raise _Cancelled
            progress(i / (len(urls) + 1), f"Downloading {what} ({label})...")
            try:
                data = _loads(_http_get(url, timeout=45, cancel=cancel))
                v = str(data.get("timestamp") or data.get("version") or "") if isinstance(data, dict) else ""
                if have and not force and v and v == stored:
                    return f"up to date ({v}, checked via {label})"
                n = store(data, label, 20)
                return f"{n:,} entries from {label} ({v or 'no version'})"
            except _Cancelled:
                raise
            except Exception as e:
                log.warning("%s via %s failed: %s", what, label, e)
                fails.append(f"{label}: {_err(e)}")
        failed = "network failed (" + "; ".join(fails) + ")"
        for p in _local_files(local_rel, settings_key):
            try:
                with open(p, "rb") as f:
                    data = _loads(f.read())
                v = str(data.get("timestamp") or data.get("version") or "")
                if have and not force and not _newer(v, stored):
                    failed += f"; local RimSort copy {v or '?'} is not newer"
                    continue
                n = store(data, "local RimSort copy", 20)
                return f"{failed}; {n:,} entries from local RimSort copy {p}"
            except Exception as e:
                log.warning("local %s %s failed: %s", what, p, e)
                failed += f"; {p}: {_err(e)}"
        return f"{failed}; kept existing" if have else f"{failed}; nothing available"

    def _update_rules(self, progress: Progress, cancel, force: bool) -> str:
        return self._update_simple("community rules", RULES_URLS, "Community-Rules-Database/communityRules.json",
                                   "external_community_rules_file_path", "rules_timestamp", self._store_rules,
                                   "SELECT COUNT(*) FROM rules", progress, cancel, force)

    def _update_uti(self, progress: Progress, cancel, force: bool) -> str:
        return self._update_simple("Use This Instead", UTI_URLS, "UseThisInstead/replacements.json.gz",
                                   "external_use_this_instead_file_path", "uti_version", self._store_uti,
                                   "SELECT COUNT(*) FROM uti", progress, cancel, force)

    def _store_rules(self, data, source: str, min_rows: int = 1) -> int:
        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, dict):
            raise ValueError("not a communityRules.json (no 'rules' object)")
        rows = list(_rules_rows(rules))
        if len(rows) < min_rows:
            raise ValueError(f"only {len(rows)} rules")
        with self._tx() as con:
            con.execute("DELETE FROM rules")
            con.executemany("INSERT OR IGNORE INTO rules(package_id, kind, other, name, comment) VALUES (?,?,?,?,?)", rows)
            self._set_meta(con, rules_timestamp=_int(data.get("timestamp")), src_rules=source)
            return con.execute("SELECT COUNT(DISTINCT package_id) FROM rules").fetchone()[0]

    def _store_uti(self, data, source: str, min_rows: int = 1) -> int:
        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, list):
            raise ValueError("not a UseThisInstead replacements file (no 'rules' list)")
        rows = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            old_wid, new_wid = str(r.get("oldWorkshopId") or "").strip(), str(r.get("newWorkshopId") or "").strip()
            if (old_wid or r.get("oldPackageId")) and old_wid != new_wid:
                rows.append((old_wid, _pid(r.get("oldPackageId")), new_wid, _pid(r.get("newPackageId")),
                             json.dumps(r, ensure_ascii=False)))
        if len(rows) < min_rows:
            raise ValueError(f"only {len(rows)} replacement rules")
        with self._tx() as con:
            con.execute("DELETE FROM uti")
            con.executemany("INSERT INTO uti(old_wid, old_pid, new_wid, new_pid, data) VALUES (?,?,?,?,?)", rows)
            self._set_meta(con, uti_version=str(data.get("version") or ""), src_uti=source)
        return len(rows)

    def _update_nvw(self, progress: Progress, cancel, force: bool) -> str:
        def fetch(ver):
            errors = []
            for label, url in NVW_URLS:
                if _cancelled(cancel):
                    return ver, None, "cancelled"
                try:
                    return ver, _parse_nvw(_http_get(url.format(ver), timeout=20, cancel=cancel)), label
                except Exception as e:
                    if _is_404(e):
                        return ver, set(), "404"        # no list for this version upstream
                    errors.append(_err(e))
            return ver, None, "; ".join(errors)

        progress(None, "Downloading No Version Warning lists...")
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = {ver: (ids, src) for ver, ids, src in ex.map(fetch, NVW_VERSIONS)}
        if _cancelled(cancel):
            raise _Cancelled
        all_404 = all(src == "404" for _, src in results.values())   # repo moved/renamed: don't wipe, use local copies
        store, used = {}, Counter()
        for ver, (ids, src) in results.items():
            if ids is not None and not all_404:
                store[ver] = ids
                used[src if src != "404" else "absent"] += 1
                continue
            for p in _local_files(f"NoVersionWarning/{ver}/ModIdsToFix.xml"):
                try:
                    with open(p, "rb") as f:
                        store[ver] = _parse_nvw(f.read())
                    used["local RimSort copy"] += 1
                    break
                except (OSError, ValueError) as e:
                    log.warning("local NVW %s failed: %s", p, e)
            else:
                used["kept existing"] += 1
        if store:
            with self._tx() as con:
                for ver, ids in store.items():
                    con.execute("DELETE FROM nvw WHERE version=?", (ver,))
                    con.executemany("INSERT OR IGNORE INTO nvw(version, package_id) VALUES (?, ?)", [(ver, p) for p in ids])
                self._set_meta(con, src_nvw=", ".join(f"{k} x{n}" for k, n in used.items()))
        total = sum(len(v) for v in store.values())
        return f"{total:,} ids for {', '.join(v for v in NVW_VERSIONS if store.get(v)) or 'no versions'} " \
               f"({', '.join(f'{k}: {n}' for k, n in used.items())})"

    def import_steamdb_file(self, path) -> int:
        """Manual import of a steamDB.json (also .zip containing it, or .gz). Always rebuilds."""
        return self._import_steam_path(str(path), f"file {os.path.basename(str(path))}", noop_progress, None, min_rows=1)

    def import_rules_file(self, path) -> int:
        with open(path, "rb") as f:
            return self._store_rules(_loads(f.read()), f"file {os.path.basename(str(path))}", 1)

    # ------------------------------------------------------------ queries
    def get(self, workshop_id: str) -> DbMod | None:
        with self._lock:
            r = self._con.execute(f"SELECT {_COLS} FROM {_FROM} WHERE m.workshop_id=?", (str(workshop_id).strip(),)).fetchone()
        return _dbmod(r) if r else None

    def get_many(self, ids: list[str]) -> dict[str, DbMod]:
        ids = list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip()))
        out = {}
        with self._lock:
            for chunk in _chunks(ids):
                q = f"SELECT {_COLS} FROM {_FROM} WHERE m.workshop_id IN ({','.join('?' * len(chunk))})"
                for r in self._con.execute(q, chunk):
                    out[r[0]] = _dbmod(r)
        return out

    def by_package_id(self, package_id: str) -> list[DbMod]:
        """Rows with this package id: published first, then newest supported version, then popularity."""
        pid = _pid(package_id)
        if not pid:
            return []
        with self._lock:
            rows = self._con.execute(
                f"SELECT {_COLS} FROM {_FROM} WHERE m.package_id=? ORDER BY m.unpublished, m.latest DESC, "
                "COALESCE(d.subscriptions, 0) DESC, m.dependents DESC, CAST(m.workshop_id AS INTEGER)", (pid,)).fetchall()
        return [_dbmod(r) for r in rows]

    def search(self, query: str, version: str = "", limit: int = 50, offset: int = 0, require_package_id: bool = False,
               sort: str = "relevance") -> list[DbMod]:
        q = " ".join(str(query or "").split())
        limit, offset = max(0, int(limit)), max(0, int(offset))
        toks = _tokens(q)
        where, params = ["m.unpublished = 0"], []
        vs = compat_versions(version)
        if len(vs) == 1:
            where.append("m.versions LIKE ?")
            params.append(f"%|{vs[0]}|%")
        elif vs:
            where.append("(" + " OR ".join("m.versions LIKE ?" for _ in vs) + ")")
            params.extend(f"%|{x}|%" for x in vs)
        v = ""
        if v:
            where.append("m.versions LIKE ?")
            params.append(f"%|{v}|%")
        if require_package_id:
            where.append("m.package_id != ''")
        with self._lock:
            exact = self._exact_ids(q, where, params) if q else []
            head = exact[offset:offset + limit]
            rest_limit = limit - len(head)
            rest = []
            if rest_limit > 0 and (toks or not q):
                args = (toks, q.lower(), where, params, exact, sort, rest_limit, max(0, offset - len(exact)))
                try:
                    rest = self._search_rows(*args, use_fts=self._fts)
                except sqlite3.OperationalError as e:      # never expected (tokens are quoted) but never fatal either
                    log.warning("FTS search failed (%s); LIKE fallback", e)
                    rest = self._search_rows(*args, use_fts=False)
            found = self.get_many(head)
        return [found[i] for i in head if i in found] + [_dbmod(r) for r in rest]

    def _exact_ids(self, q: str, where: list, params: list) -> list[str]:
        """Exact workshop id (bare, URL or steam:// link) or exact package id matches, best first."""
        wid = _as_workshop_id(q)
        cond, arg = ("m.workshop_id=?", wid) if wid else ("m.package_id=?", q.lower()) if _PID_RE.fullmatch(q.lower()) else ("", "")
        if not cond:
            return []
        rows = self._con.execute(
            f"SELECT m.workshop_id FROM {_FROM} WHERE {cond} AND {' AND '.join(where)} "
            "ORDER BY m.latest DESC, COALESCE(d.subscriptions, 0) DESC, m.dependents DESC", [arg, *params]).fetchall()
        return [r[0] for r in rows]

    def _search_rows(self, toks, ql, where, params, exclude, sort, limit, offset, use_fts: bool) -> list:
        where, params, cte, join, jparams = list(where), list(params), "", "", []
        if exclude:
            where.append(f"m.workshop_id NOT IN ({','.join('?' * len(exclude))})")
            params += exclude
        if toks:
            like = " AND ".join("(m.name LIKE ? ESCAPE '\\' OR m.steam_name LIKE ? ESCAPE '\\' OR "
                                "m.authors LIKE ? ESCAPE '\\' OR m.package_id LIKE ? ESCAPE '\\')" for _ in toks)
            lparams = [_like(t) for t in toks for _ in range(4)]
            if use_fts:     # FTS hits (prefix per token, AND) + substring hits FTS can't see (CJK runs, "hud" in "RimHUD")
                expr = " AND ".join('"' + t.replace('"', '""') + '"*' for t in toks)
                # MATERIALIZED: without it SQLite re-runs the MATCH per mods row (seconds instead of ~20 ms)
                cte = (f"WITH f AS MATERIALIZED (SELECT rowid AS rid, bm25(mods_fts, {_W_NAME}, {_W_STEAM}, {_W_AUTHORS}, "
                       f"{_W_PID}) AS score FROM mods_fts WHERE mods_fts MATCH ?) ")
                join = "LEFT JOIN f ON f.rid = m.rowid"
                jparams = [expr]
                where.append(f"(f.rid IS NOT NULL OR ({like}))")
            else:
                where.append(f"({like})")
            params += lparams
        popular = f"COALESCE(d.subscriptions, 0) DESC, m.dependents DESC, {_NAME_KEY}"
        if sort == "name":
            order, oparams = f"{_NAME_KEY}, m.workshop_id", []
        elif sort == "popular" or not toks:
            order, oparams = popular, []
        else:
            exact = "(lower(m.name) = ? OR lower(m.steam_name) = ? OR m.package_id = ?) DESC"
            if use_fts:
                order = f"{exact}, (f.rid IS NULL), COALESCE(f.score, 0) - m.pop * {_W_POP}, {popular}"
            else:
                order = f"{exact}, (lower(m.name) LIKE ? ESCAPE '\\' OR lower(m.steam_name) LIKE ? ESCAPE '\\') DESC, " \
                        f"m.pop DESC, {popular}"
            oparams = [ql, ql, ql] + ([] if use_fts else [_like(toks[0])[1:]] * 2)
        sql = (f"{cte}SELECT {_COLS} FROM {_FROM} {join} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?")
        return self._con.execute(sql, [*jparams, *params, *oparams, limit, offset]).fetchall()

    def rules(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        with self._lock:
            rows = self._con.execute("SELECT package_id, kind, other, comment FROM rules").fetchall()
        for pid, kind, other, comment in rows:
            r = out.get(pid)
            if r is None:
                r = out[pid] = {"load_after": {}, "load_before": {}, "incompatible_with": {}, "load_top": False, "load_bottom": False}
            if kind in ("load_top", "load_bottom"):
                r[kind] = True
            else:
                r[kind][other] = comment
        return out

    def replacement_for(self, workshop_id: str = "", package_id: str = "") -> dict | None:
        """UseThisInstead rule for an old mod. Matches by workshop id; by package id only when the workshop id is unknown
        and the replacement uses a different package id (else a new copy could be told to replace itself)."""
        wid, pid = str(workshop_id or "").strip(), _pid(package_id)
        with self._lock:
            if wid:
                r = self._con.execute("SELECT data FROM uti WHERE old_wid=? ORDER BY rowid LIMIT 1", (wid,)).fetchone()
            elif pid:
                r = self._con.execute("SELECT data FROM uti WHERE old_pid=? AND new_pid != old_pid ORDER BY rowid LIMIT 1",
                                      (pid,)).fetchone()
            else:
                r = None
        return json.loads(r[0]) if r else None

    def no_version_warning(self, version_short: str) -> set[str]:
        with self._lock:
            return {r[0] for r in self._con.execute("SELECT package_id FROM nvw WHERE version=?",
                                                    (short_version(version_short) or str(version_short),))}

    def cache_details(self, details: dict[str, dict]) -> None:
        now = time.time()
        rows = [(str(wid), now, _int(d.get("subscriptions")), _int(d.get("time_updated")), json.dumps(d, ensure_ascii=False))
                for wid, d in details.items() if isinstance(d, dict)]
        if rows:
            with self._tx() as con:
                con.executemany("INSERT OR REPLACE INTO steam_details(workshop_id, fetched_at, subscriptions, time_updated, data) "
                                "VALUES (?,?,?,?,?)", rows)

    def cached_details(self, ids: list[str], max_age_s: float = 86400) -> dict[str, dict]:
        ids = list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip()))
        cutoff, out = time.time() - max_age_s, {}
        with self._lock:
            for chunk in _chunks(ids):
                q = f"SELECT workshop_id, data FROM steam_details WHERE fetched_at >= ? AND workshop_id IN ({','.join('?' * len(chunk))})"
                for wid, data in self._con.execute(q, [cutoff, *chunk]):
                    try:
                        out[wid] = json.loads(data)
                    except ValueError:
                        pass
        return out

    # ------------------------------------------------------------ player-made Workshop collections
    def similar_packs(self, words: list[str], seeds: list[str], limit: int = 300) -> list[int]:
        """The collections most like a request, interleaved: by shared mods with seeds (overlap^2 / size, i.e. set
        cosine, at least 2 shared) and by request words in the title/description (FTS bm25). [] without packs."""
        seeds = [int(w) for w in dict.fromkeys(str(s) for s in seeds) if w.isdigit()][:200]
        terms = [re.sub(r"[^\w]", "", w) for w in words]
        match = " OR ".join(f'"{w}"' + ("*" if len(w) >= 4 else "") for w in dict.fromkeys(terms) if len(w) >= 3)
        by_seed, by_text = [], []
        with self._lock:
            if seeds:
                by_seed = [r[0] for r in self._con.execute(
                    "SELECT m.cid FROM pack_mods m JOIN packs p ON p.cid = m.cid "
                    f"WHERE m.wid IN ({','.join('?' * len(seeds))}) GROUP BY m.cid HAVING COUNT(*) >= 2 "
                    "ORDER BY COUNT(*) * COUNT(*) * 1.0 / MAX(p.n, 1) DESC LIMIT ?", [*seeds, limit])]
            if match and self._fts:
                by_text = [r[0] for r in self._con.execute(
                    "SELECT rowid FROM packs_fts WHERE packs_fts MATCH ? ORDER BY bm25(packs_fts, 3.0, 1.0) LIMIT ?",
                    (match, limit))]
        mixed = [c for pair in zip(by_seed, by_text) for c in pair] + by_seed[len(by_text):] + by_text[len(by_seed):]
        return list(dict.fromkeys(mixed))[:limit]

    def pack_votes(self, cids: list[int] | None, wids: list[str] | None = None) -> dict[str, float]:
        """workshop id -> share of the given collections (None: all of them) that contain it; only wids if given."""
        ids = [int(w) for w in wids or () if str(w).isdigit()]
        where, params = [], []
        if cids is not None:
            if not cids:
                return {}
            where.append(f"cid IN ({','.join('?' * len(cids))})")
            params += cids
        if wids is not None:
            if not ids:
                return {}
            where.append(f"wid IN ({','.join('?' * len(ids))})")
            params += ids
        with self._lock:
            total = len(cids) if cids is not None else self._con.execute("SELECT COUNT(*) FROM packs").fetchone()[0]
            rows = self._con.execute(f"SELECT wid, COUNT(*) FROM pack_mods {'WHERE ' + ' AND '.join(where) if where else ''} "
                                     "GROUP BY wid", params).fetchall()
        return {str(w): n / total for w, n in rows} if total else {}


# ---------------------------------------------------------------- self tests
def _fake_steamdb(version: int) -> dict:
    tag = lambda *ts: [{"tag": t, "display_name": t} for t in ts]
    db = {
        "294100": {"appid": True, "url": "https://store.steampowered.com/app/294100", "packageid": "ludeon.rimworld",
                   "name": "RimWorld", "authors": "Ludeon Studios", "packageId": "ludeon.rimworld"},
        "1149640": {"appid": True, "packageId": "ludeon.rimworld.royalty", "name": "RimWorld - Royalty"},
        "2009463077": {"url": "u", "steamName": "Harmony", "packageId": "brrainz.harmony", "name": "Harmony",
                       "authors": "Andreas Pardeike", "gameVersions": ["1.5", "1.6"], "tags": tag("1.5", "1.6", "Mod")},
        "1111111": {"steamName": "Harmony Patches Collection", "packageId": "Some.HarmonyPatches", "name": "Harmony Patches",
                    "authors": "Someone", "gameVersions": ["1.6.4633 rev1261"],
                    "dependencies": {"2009463077": ["Harmony", "u"], "294100": ["Core", ""], "1149640": ["Royalty", ""]}},
        "2222222": {"steamName": "高级战斗系统 Advanced Combat", "packageId": "scenario.rsc", "tags": tag("1.6", "Mod"),
                    "dependencies": {"1111111": ["HP", ""]}},
        "3333333": {"steamName": "Old Thing", "packageId": "old.thing", "name": "Old Thing", "gameVersions": ["1.4"],
                    "unpublished": True},
        "4444444": {"steamName": "Pokémon Pack", "name": "Pokémon Pack", "packageId": "MISSING.PACKAGEID", "gameVersions": ["1.6"]},
        "5555555": {"steamName": "RimHUD", "name": "RimHUD", "packageId": "Jaxxa.RimHUD", "authors": "Jaxxa",
                    "gameVersions": ["1.5", "1.6"], "dependencies": {"2009463077": ["Harmony", ""]}},
        "6666666": {"steamName": "Harmony Russian Translation", "tags": tag("1.6", "Translation")},
        "7777777": {"steamName": "Old Thing Continued", "packageId": "old.thing", "name": "Old Thing", "gameVersions": ["1.6"]},
        "8888888": {"unpublished": True},
    }
    for i in range(990):   # filler so the network-source minimum row check passes
        db[str(9000000 + i)] = {"steamName": f"Filler {i}", "tags": tag("1.6")}
    return {"version": version, "database": db}


def _selftest() -> None:
    import io
    global _http_get, _download, _rimsort_dir
    tmp = tempfile.mkdtemp(prefix="rmm-dbtest-")
    saved = (_http_get, _download, _rimsort_dir, log.level)
    log.setLevel(logging.ERROR)     # the fallback tests log expected warnings
    db = None
    try:
        # --- manual file import (.json), junk ids, versions
        p = os.path.join(tmp, "steamDB.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_fake_steamdb(100), f, ensure_ascii=False)
        db = ModDatabase(os.path.join(tmp, "t.sqlite"))
        assert db.is_empty() and db.needs_update(3)
        assert db.import_steamdb_file(p) == 1001
        assert not db.is_empty() and db.status()["steam_version"] == "100"
        h = db.get("2009463077")
        assert h and h.package_id == "brrainz.harmony" and h.game_versions == ["1.5", "1.6"], h
        assert db.get("2222222").package_id == "" and db.get("2222222").game_versions == ["1.6"]
        assert db.get("4444444").package_id == "" and db.get("1111111").game_versions == ["1.6"]
        assert db.get("1111111").package_id == "some.harmonypatches" and db.get("294100").is_dlc
        assert db.get("6666666").kind == "translation" and h.kind == ""                  # Steam tag kind persisted
        assert db.get("nope") is None and set(db.get_many(["1111111", "nope", "5555555"])) == {"1111111", "5555555"}
        assert [m.workshop_id for m in db.by_package_id("OLD.THING")] == ["7777777", "3333333"]
        assert db.by_package_id("") == [] and db.by_package_id("missing.packageid") == []
        # --- search
        ids = lambda res: [m.workshop_id for m in res]
        r = ids(db.search("harmony", version="1.6"))
        assert r[0] == "2009463077" and "1111111" in r and "6666666" in r and r.index("1111111") < r.index("6666666"), r
        assert ids(db.search("HARMONY", version="1.6.4871 rev590"))[0] == "2009463077"
        assert ids(db.search("pokemon")) == ["4444444"]                         # diacritics folded by FTS
        assert ids(db.search("战斗")) == ["2222222"]                             # CJK substring via LIKE
        assert ids(db.search("hud"))[0] == "5555555"                            # substring inside a token
        assert ids(db.search("2009463077")) == ["2009463077"]
        assert ids(db.search("https://steamcommunity.com/sharedfiles/filedetails/?id=5555555")) == ["5555555"]
        assert ids(db.search("jaxxa.rimhud"))[0] == "5555555"
        assert ids(db.search("old thing")) == ["7777777"]                       # unpublished excluded
        assert ids(db.search("old thing", version="1.4")) == []
        assert "2222222" not in ids(db.search("combat", require_package_id=True)) and ids(db.search("combat")) == ["2222222"]
        assert ids(db.search('"; DROP TABLE mods; --')) == [] and ids(db.search("*")) == [] and ids(db.search("a AND OR")) is not None
        allh = ids(db.search("harmony", limit=100))
        paged = ids(db.search("harmony", limit=2)) + ids(db.search("harmony", limit=2, offset=2)) + \
            ids(db.search("harmony", limit=100, offset=4))
        assert paged == allh and len(set(allh)) == len(allh)
        pk = ids(db.search("brrainz.harmony", limit=1)) + ids(db.search("brrainz.harmony", limit=5, offset=1))
        assert pk[0] == "2009463077" and len(set(pk)) == len(pk)
        assert ids(db.search("", sort="popular", limit=1)) == ["2009463077"]     # most dependents
        assert ids(db.search("harmony", sort="name"))[0] == "2009463077"
        assert len(db.search("", version="1.6", limit=5000)) == 997 and len(db.search("", version="1.7", limit=5000)) == 997
        # --- details cache + enrichment
        db.cache_details({"5555555": {"publishedfileid": "5555555", "subscriptions": 999999, "time_updated": 5,
                                      "description": "HUD", "preview_url": "http://x"}})
        e = db.get("5555555")
        assert e.subscriptions == 999999 and e.description == "HUD" and e.preview_url == "http://x"
        assert ids(db.search("", sort="popular", limit=1)) == ["5555555"]
        assert set(db.cached_details(["5555555", "1"])) == {"5555555"} and db.cached_details(["5555555"], max_age_s=-1) == {}
        # --- dependencies stored per row (Core/DLC app ids are kept; callers drop them)
        assert "1111111" in db.get("2222222").dependencies, db.get("2222222").dependencies
        # --- Steam sync: items changed since the snapshot are merged in, newest first, stopping at older ones
        assert "skipped" in db._sync_steam("KEY", noop_progress, None)          # snapshot version 100: no real date
        snap = 10 ** 9
        with db._tx() as con:
            ModDatabase._set_meta(con, steam_version=str(snap))
        pages = [{"response": {"next_cursor": "c2", "publishedfiledetails": [
                     {"publishedfileid": "8765432101", "result": 1, "title": "Brand New Turrets", "time_updated": snap + 500,
                      "tags": [{"tag": "Mod"}, {"tag": "1.6"}], "children": [{"publishedfileid": "2009463077"}],
                      "subscriptions": 42},
                     {"publishedfileid": "5555555", "result": 1, "title": "RimHUD Updated", "time_updated": snap + 400,
                      "tags": [{"tag": "1.7"}]}]}},
                 {"response": {"next_cursor": "c3", "publishedfiledetails": [
                     {"publishedfileid": "1111111", "result": 1, "title": "Too Old", "time_updated": snap - 2 * 86400}]}}]
        asked = []

        def fake_query(url, timeout=30, headers=None, cancel=None):
            asked.append(url)
            return json.dumps(pages[len(asked) - 1]).encode()
        _http_get = fake_query
        msg = db._sync_steam("KEY", noop_progress, None)
        assert len(asked) == 2 and "2 mods changed" in msg and "1 new" in msg and "key=KEY" in asked[0], msg
        n = db.get("8765432101")
        assert n.game_versions == ["1.6"] and "2009463077" in n.dependencies and n.subscriptions == 42, n
        assert ids(db.search("brand new turrets")) == ["8765432101"]                # search index rebuilt
        h = db.get("5555555")
        assert h.package_id == "jaxxa.rimhud" and "1.7" in h.game_versions and h.steam_name == "RimHUD Updated", h
        assert db.get("1111111").steam_name != "Too Old" and db._meta("steam_sync_at") == str(snap + 500)
        # --- the AI's live finds: stored and searchable, authors filled where unknown, the sync point untouched
        assert db.store_items({"8765432102": {"publishedfileid": "8765432102", "title": "Nice Research Tab",
                                              "tags": [{"tag": "Mod"}, {"tag": "1.6"}], "time_updated": 5,
                                              "children": [{"publishedfileid": "3773448562"}]},
                               "8765432101": {"publishedfileid": "8765432101", "title": "Brand New Turrets",
                                              "tags": [{"tag": "1.6"}]}}) == 1
        assert ids(db.search("nice research")) == ["8765432102"] and "3773448562" in db.get("8765432102").dependencies
        assert db._meta("steam_sync_at") == str(snap + 500)
        with db._tx() as con:                                                    # back to the snapshot the tests below expect
            ModDatabase._set_meta(con, steam_version="100")
        # --- rules / UTI / NVW stores
        rules = {"timestamp": 7, "rules": {
            "KRKR.RocketMan": {"loadBottom": {"value": True, "comment": "last"}, "loadAfter": {"Brrainz.Harmony": {"name": ["H"]}}},
            "a.b": {"loadBefore": {"C.D": {"name": "x", "comment": ["c1", "c2"]}}, "loadTop": {"value": True},
                    "incompatibleWith": {"e.f": {"name": "E", "comment": "boom"}}},
            "missing.packageid": {"loadAfter": {"x.y": {}}}}}
        rp = os.path.join(tmp, "communityRules.json")
        with open(rp, "w", encoding="utf-8-sig") as f:
            json.dump(rules, f)
        assert db.import_rules_file(rp) == 2
        rr = db.rules()
        assert rr["krkr.rocketman"]["load_bottom"] and rr["krkr.rocketman"]["load_after"] == {"brrainz.harmony": ""}
        assert rr["a.b"]["load_before"] == {"c.d": "c1; c2"} and rr["a.b"]["load_top"] and rr["a.b"]["incompatible_with"] == {"e.f": "boom"}
        uti = {"version": "v1", "rules": [
            {"oldWorkshopId": "3333333", "oldPackageId": "Old.Thing", "newWorkshopId": "7777777", "newPackageId": "Old.Thing"},
            {"oldWorkshopId": "42", "oldPackageId": "X.Old", "newWorkshopId": "43", "newPackageId": "X.New"}]}
        db._store_uti(uti, "test")
        assert db.replacement_for("3333333")["newWorkshopId"] == "7777777"
        assert db.replacement_for("7777777", "old.thing") is None                 # the new copy is never replaced by itself
        assert db.replacement_for(package_id="OLD.THING") is None                  # same package id: ambiguous without a wid
        assert db.replacement_for(package_id="x.old")["newPackageId"] == "X.New"
        assert _parse_nvw(b'\xef\xbb\xbf<?xml version="1.0"?><ModIdsToFix><!-- <li>no.no</li> --><li> A.B </li><li>scenario.rsc</li>'
                          b'</ModIdsToFix>') == {"a.b"}
        # --- update(): network down -> local RimSort copies
        def no_net(*_a, **_k):
            raise OSError("network down (selftest)")
        _http_get, _download = no_net, no_net
        rs = os.path.join(tmp, "RimSort")
        _rimsort_dir = lambda: rs
        for rel, content in (("Steam-Workshop-Database/steamDB.json", json.dumps(_fake_steamdb(200))),
                             ("Community-Rules-Database/communityRules.json", json.dumps({"timestamp": 8, "rules": {
                                 f"m{i}.x": {"loadAfter": {"brrainz.harmony": {}}} for i in range(30)}})),
                             ("NoVersionWarning/1.6/ModIdsToFix.xml", "<ModIdsToFix><li>Foo.Bar</li></ModIdsToFix>")):
            fp = os.path.join(rs, "dbs", *rel.split("/"))
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)
        os.makedirs(os.path.join(rs, "dbs", "UseThisInstead"))
        with gzip.open(os.path.join(rs, "dbs", "UseThisInstead", "replacements.json.gz"), "wt", encoding="utf-8-sig") as f:
            json.dump({"version": "v2", "rules": [{"oldWorkshopId": str(i), "newWorkshopId": str(i + 1)} for i in range(25)]}, f)
        s = db.update()
        st = db.status()
        assert st["steam_version"] == "200" and st["rules_timestamp"] == 8 and st["uti_count"] == 25, (s, st)
        assert db.no_version_warning("1.6") == {"foo.bar"} and st["updated_at"] == 0 and db.needs_update(3), s
        assert "kept existing" in db.update()                                     # local copy not newer -> no rebuild
        # --- update(): codeload zip path (served from memory), skip when version unchanged, force rebuild
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Steam-Workshop-Database-main/steamDB.json", json.dumps(_fake_steamdb(300)))
        calls = []

        def fake_download(url, dest, progress, cancel):
            calls.append(url)
            if url != STEAMDB_ZIP:
                raise OSError("only zip")
            with open(dest, "wb") as f:
                f.write(buf.getvalue())
            return dest

        def fake_get(url, timeout=30, headers=None, cancel=None):
            if "communityRules" in url:
                return json.dumps({"timestamp": 9, "rules": {f"n{i}.x": {"loadBottom": {"value": True}} for i in range(25)}}).encode()
            raise OSError("down")
        _download, _http_get = fake_download, fake_get
        s = db.update()
        assert db.status()["steam_version"] == "300" and db.status()["rules_timestamp"] == 9 and not db.needs_update(3), s
        assert "codeload" in db.status()["source"] and calls == [STEAMDB_ZIP]
        assert "up to date" in db.update().splitlines()[0]
        db.update(force=True)
        assert db.status()["steam_count"] == 1001 and ids(db.search("harmony"))[0] == "2009463077"
        ev = threading.Event()
        ev.set()
        assert "cancelled" in db.update(cancel=ev)
        # --- an update called while another runs waits (Cancel stops the wait) and shares its summary: one download
        gate, started, res = threading.Event(), threading.Event(), {}

        def slow_download(url, dest, progress, cancel):
            started.set()
            gate.wait(10)
            return fake_download(url, dest, progress, cancel)
        _download = slow_download
        calls.clear()
        th = threading.Thread(target=lambda: res.setdefault("first", db.update(force=True)))
        th.start()
        assert started.wait(10)
        assert "cancelled while waiting" in db.update(cancel=ev) and not gate.is_set()
        threading.Timer(0.3, gate.set).start()
        second = db.update()
        th.join(10)
        assert second == res["first"] and calls == [STEAMDB_ZIP], (second, res, calls)
        # --- a pre-'kind' DB gets the column; the next update (due now) re-imports steamDB to fill it
        o = ModDatabase(os.path.join(tmp, "old.sqlite"))
        with o._tx() as con:
            ModDatabase._set_meta(con, steam_version="5", updated_at=time.time())
            con.execute("ALTER TABLE mods DROP COLUMN kind")
        o.close()
        o = ModDatabase(os.path.join(tmp, "old.sqlite"))
        assert o.get("x") is None and o.status()["steam_version"] == "" and o.status()["updated_at"] == 0
        o.close()
        # --- empty DB + every source failing -> raises
        _http_get, _download, _rimsort_dir = no_net, no_net, (lambda: os.path.join(tmp, "none"))
        empty = ModDatabase(os.path.join(tmp, "e.sqlite"))
        try:
            empty.update()
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
        empty.close()
    finally:
        _http_get, _download, _rimsort_dir, level = saved
        log.setLevel(level)
        if db:
            db.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_online() -> None:
    tmp = tempfile.mkdtemp(prefix="rmm-dbtest-")
    db = ModDatabase(os.path.join(tmp, "online.sqlite"))
    try:
        summary = db.update()
        st = db.status()
        assert st["steam_count"] > 40000 and st["rules_count"] > 100 and st["updated_at"] > 0, (summary, st)
        assert db.search("harmony", version="1.6")[0].package_id == "brrainz.harmony"
        assert db.rules().get("krkr.rocketman", {}).get("load_bottom")
        assert "up to date" in db.update().splitlines()[0]
    finally:
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)
