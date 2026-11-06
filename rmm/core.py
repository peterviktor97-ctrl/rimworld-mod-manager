"""Manager: the single façade the GUI talks to (paths, scan, active list, modlists, downloads, game, DB, AI)."""
from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time

from . import config, mods, paths, sorting, steam
from .database import ModDatabase
from .models import (CORE_ID, DLC_BY_APPID, DLCS, HARMONY_ID, LEVEL_RANK, LOADABLE_SOURCES, OFFICIAL_IDS, RIMWORLD_APPID,
                     DbMod, DownloadResult, Issue, LogEntry, Mod, ModList, ModRef, Progress, noop_progress)

MAX_UNDO = 50
GAME_EXE = "RimWorldWin64.exe"
LOG_TAIL_BYTES = 8 * 1024 * 1024
# SteamCMD failures Steam cannot fix either (no Steamworks fallback for these)
_PERMANENT_FAIL = re.compile(r"cancelled|invalid workshop id|official content|removed|not on steam|no longer"
                             r"|not updated", re.I)
BROWSER_PAUSE_S = 15         # pause between browser batches so a weak laptop is not flooded with tabs
SPACE_MARGIN = 500 * 1024 ** 2   # free space kept on top of 110 % of the download size
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class GameRunningError(RuntimeError):
    """RimWorld is running; it may overwrite ModsConfig.xml, so the GUI must ask before saving with force=True."""


class NotEnoughSpaceError(RuntimeError):
    """A download would not fit on a drive (estimated from the Workshop file sizes)."""


class ExternalChangeError(Exception):
    """ModsConfig.xml changed on disk since the app loaded/saved it (RimWorld's own mod menu?); save with force=True
    to overwrite it, or reload it."""


def _deep_mtime(path: str) -> float:
    """Newest mtime of any file or folder under path (a junction root is followed, nested links are not)."""
    newest, stack = 0.0, [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return newest


def _dedupe(items) -> list:
    return list(dict.fromkeys(i for i in items if i))


def _base_pid(uid: str) -> str:
    """'foo.bar_steam' / 'foo.bar_dup2' -> 'foo.bar'."""
    return re.sub(r"(?:_steam|_dup\d+)$", "", (uid or "").strip().lower())


def compare_lists(listed: list[str | None], active: list[str]) -> tuple[list[int], list[str], int]:
    """Saved list (installed uid per entry, None = not installed) vs active uids, matched by package id base:
    (indices of list entries not active, active uids not in the list, moves needed for the shared mods' order)."""
    act = {_base_pid(u): i for i, u in enumerate(active)}
    lst = {_base_pid(u) for u in listed if u}
    only_list = [i for i, u in enumerate(listed) if not u or _base_pid(u) not in act]
    only_active = [u for u in active if _base_pid(u) not in lst]
    seq = [act[b] for b in dict.fromkeys(_base_pid(u) for u in listed if u) if b in act]
    tails: list[int] = []                   # longest increasing run = shared mods already in order
    for x in seq:
        j = bisect.bisect_left(tails, x)
        tails[j:j + 1] = [x]
    return only_list, only_active, len(seq) - len(tails)


def _title_key(s: str) -> str:
    """'Combat Bionics (Continued)' -> 'combatbionicscontinued': case and punctuation do not matter."""
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def _safe_name(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")[:120] or "Unnamed"
    return "_" + s if s.lower() in _RESERVED_NAMES else s


# ---------------------------------------------------------------- Player.log parsing
_LOG_ERR = re.compile(r"exception|\berror\b|could not resolve|could not execute|\bfatal\b", re.I)
_LOG_WARN = re.compile(r"\bwarn(?:ing)?\b|^could not|^couldn't|\bfailed\b|^tried to|duplicate|\bcannot\b|not found"
                       r"|doesn't have|\bnull\b|\bmissing\b|obsolete|deprecated", re.I)
_LOG_CONT = re.compile(r"^(?:\s|at |Stacktrace|\(wrapper|Verse\.|RimWorld\.|UnityEngine\.|System\.|\[Ref [0-9A-Fa-f]+\]"
                       r"|Parameter name|Rethrow as|--- |\(Filename|Source file:)")
_LOG_TRACE_START = re.compile(r"^\[(.+?) - Start of stack trace\]\s*$")
_LOG_TOKEN = re.compile(r"[A-Za-z0-9_][\w.\-]*")
_LOG_MODPATH = re.compile(r"[\\/](?:Mods|294100)[\\/]([^\\/\r\n\"']+)", re.I)
_LOG_MAX_LINES = 80           # per block kept in LogEntry.detail
_LOG_MAX_MODS = 8
# assembly stems shipped by many mods (libraries) or too generic to point at one mod
_COMMON_ASSEMBLIES = {"0harmony", "harmony", "assembly-csharp", "unityengine", "system", "mscorlib", "mono", "netstandard",
                      "core", "common", "shared", "utils", "utility", "main", "verse", "rimworld", "0multiplayerapi",
                      "multiplayerapi", "0prepatcherapi", "prepatcherapi", "0modbase", "modbase"}


def _parse_log_blocks(lines, max_entries: int) -> list[LogEntry]:
    """Group Player.log lines into error/warning blocks and collapse identical ones (by level + first line)."""
    entries: dict[tuple[str, str], LogEntry] = {}
    cur: list | None = None           # [level, text, lines]
    in_trace = False
    trace_tag = ""                    # set until the first line inside "[Mod - Start of stack trace]" names the block

    def flush():
        if not cur:
            return
        level, text, blines = cur
        key = (level, text[:300])
        e = entries.get(key)
        if e:
            e.count += 1
        else:
            if len(blines) > _LOG_MAX_LINES:
                blines = blines[:_LOG_MAX_LINES] + [f"… ({len(blines) - _LOG_MAX_LINES} more lines)"]
            entries[key] = LogEntry(level, text[:300], "\n".join(blines))

    for line in lines:
        if len(line) > 1000:
            line = line[:1000] + "…"
        if in_trace:                              # "[Mod - Start of stack trace]" … "[End of stack trace]"
            cur[2].append(line)
            if line.startswith("[End of stack trace]"):
                in_trace = False
            elif trace_tag and line.strip():
                cur[1], trace_tag = f"[{trace_tag}] {line.strip()}", ""
            continue
        m = _LOG_TRACE_START.match(line)
        if m:
            flush()
            cur, in_trace, trace_tag = ["error", f"[{m.group(1)}]", [line]], True, m.group(1)
            continue
        if cur and _LOG_CONT.match(line):
            cur[2].append(line)
            continue
        if not line.strip():
            continue
        flush()
        cur = None
        level = "error" if _LOG_ERR.search(line) else "warning" if _LOG_WARN.search(line) else None
        if level:
            cur = [level, line.strip(), [line]]
    flush()
    out = list(entries.values())
    if len(out) > max_entries:           # over the cap: errors win over warnings, file order is kept
        keep = {id(e) for e in sorted(out, key=lambda e: e.level != "error")[:max_entries]}
        out = [e for e in out if id(e) in keep]
    return out


class Manager:
    def __init__(self, settings: config.Settings | None = None, db: ModDatabase | None = None,
                 data_dir: str | None = None):
        """data_dir: where modlists/backups/user_rules.json live (default config.APP_DIR; tests pass a temp dir)."""
        self.settings = settings if settings is not None else config.Settings()
        self.db = db if db is not None else ModDatabase()
        self.log = config.get_logger()
        self.data_dir = data_dir or config.APP_DIR
        self.game_dir: str | None = None
        self.config_dir: str | None = None
        self.workshop_dir: str | None = None
        self.steam_path: str | None = None
        self.game_version = ""
        self.version_short = ""
        self.installed: dict[str, Mod] = {}
        self.active: list[str] = []
        self.known_expansions: list[str] = []
        self.dirty = False
        self._config_version = ""
        self._loaded = False              # save_active_to_game refuses until the game's list was loaded (or set by the user)
        self._load_tried = False
        self._cfg_hash: str | None = None  # ModsConfig.xml content hash at the last load/save (None = missing)
        self._install_lock = threading.Lock()   # installs vs. stale-install cleanup in scan()
        self._ml_lock = threading.Lock()        # modlist collision check + write
        self._undo: list[list[str]] = []
        self._redo: list[list[str]] = []
        self._by_pid: dict[str, list[str]] = {}
        self._by_wid: dict[str, list[str]] = {}
        self._by_name: dict[str, list[str]] = {}
        self._hints: dict[str, ModRef] = {}   # not-installed active uid -> ref it came from (keeps its workshop id)
        self.user_rules: dict = self._load_user_rules()

    # ------------------------------------------------------------ helpers
    def _dir(self, *parts) -> str:
        p = os.path.join(self.data_dir, *parts)
        os.makedirs(p, exist_ok=True)
        return p

    def _db(self, method: str, *args, default=None):
        """Call a ModDatabase method; a broken/locked DB must never break the mod manager."""
        try:
            return getattr(self.db, method)(*args)
        except Exception as e:  # noqa: BLE001
            self.log.warning("Mod database %s failed: %s", method, e)
            return default

    def _load_user_rules(self) -> dict:
        ours = os.path.join(self.data_dir, "user_rules.json")
        src = ours
        if not os.path.isfile(ours):                    # first run: adopt RimSort's user rules if present
            rs = paths.rimsort_dbs_dir()
            src = os.path.join(rs, "userRules.json") if rs else ""
        if not src or not os.path.isfile(src):
            return {}
        try:
            with open(src, encoding="utf-8-sig") as f:
                return sorting.rules_from_json(json.load(f))
        except (OSError, ValueError) as e:
            self.log.warning("Could not read user rules %s: %s", src, e)
            if src == ours and isinstance(e, ValueError):   # keep the broken file: the next save would replace it
                try:
                    os.replace(ours, f"{ours}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
                except OSError:
                    pass
            return {}

    def save_user_rules(self) -> None:
        """Persist self.user_rules (edited by the Tools tab) to <data_dir>/user_rules.json."""
        self._dir()
        config.atomic_write(os.path.join(self.data_dir, "user_rules.json"),
                            json.dumps(self.user_rules, indent=1, ensure_ascii=False).encode("utf-8"))

    # ------------------------------------------------------------ paths / scan
    def detect_paths(self) -> dict:
        s = self.settings
        self.steam_path = paths.find_steam()
        self.game_dir = paths.find_game_dir(s)
        self.config_dir = paths.find_config_dir(s)
        self.workshop_dir = paths.find_workshop_dir(self.game_dir, s)
        self.game_version = paths.game_version(self.game_dir)
        if not self.game_version and self.config_dir:     # no Version.txt: fall back to the last-run version
            self.game_version = mods.read_mods_config(self.config_dir)[2]
        self.version_short = paths.short_version(self.game_version)
        info = {"game_dir": self.game_dir, "config_dir": self.config_dir, "workshop_dir": self.workshop_dir,
                "game_version": self.game_version, "steam": self.steam_path}
        self.log.info("Detected paths: %s", info)
        return info

    def scan(self, progress: Progress = noop_progress) -> dict[str, Mod]:
        mods_dir = os.path.join(self.game_dir, "Mods") if self.game_dir else ""
        if mods_dir and os.path.isdir(mods_dir) and self._install_lock.acquire(blocking=False):  # skip mid-install
            try:
                for msg in steam.cleanup_stale_installs(mods_dir):
                    self.log.warning("%s", msg)
            except Exception as e:  # noqa: BLE001 - a leftover folder must not block the scan
                self.log.warning("Cleaning up interrupted installs failed: %s", e)
            finally:
                self._install_lock.release()
        sc = self.steamcmd().content_dir
        extra = [d for d in (self.settings.get("extra_mod_dirs") or []) if isinstance(d, str) and os.path.isdir(d)]
        found = mods.scan_all(self.game_dir, self.workshop_dir, extra, sc if os.path.isdir(sc) else None,
                              self.version_short, progress)
        # Leftover DLC folders from an older RimWorld install (e.g. 1.6 Data/Odyssey on 1.7) are not owned/loadable.
        v = self.version_short
        found = {u: m for u, m in found.items()
                 if not (m.source == "dlc" and v and m.supported_versions and v not in m.supported_versions)}
        # SteamCMD's download folder is only a cache (the game never loads it): hide items installed elsewhere.
        elsewhere = {m.workshop_id for m in found.values() if m.source != "steamcmd" and m.workshop_id}
        found = {u: m for u, m in found.items() if not (m.source == "steamcmd" and m.workshop_id in elsewhere)}
        if any(m.source == "extra" for m in found.values()):     # hide extra folders already linked into Mods/
            linked = {os.path.normcase(os.path.realpath(m.path)) for m in found.values()
                      if m.source == "local" and (os.path.islink(m.path) or os.path.isjunction(m.path))}
            found = {u: m for u, m in found.items()
                     if not (m.source == "extra" and os.path.normcase(os.path.realpath(m.path)) in linked)}
        by_pid: dict[str, list[str]] = {}
        by_wid: dict[str, list[str]] = {}
        by_name: dict[str, list[str]] = {}
        for uid, m in found.items():
            by_pid.setdefault(m.package_id, []).append(uid)
            if m.workshop_id:
                by_wid.setdefault(m.workshop_id, []).append(uid)
            if m.name:
                by_name.setdefault(m.name.strip().lower(), []).append(uid)
        for lst in (*by_pid.values(), *by_wid.values()):
            lst.sort(key=lambda u: (found[u].source not in LOADABLE_SOURCES, u != found[u].package_id, u))
        self.installed, self._by_pid, self._by_wid, self._by_name = found, by_pid, by_wid, by_name
        self.log.info("Scanned %d mods", len(found))
        return found

    def owned_dlcs(self) -> set[str]:
        v = self.version_short
        if self.installed:
            return {m.package_id for m in self.installed.values()
                    if m.source == "dlc" and (not v or not m.supported_versions or v in m.supported_versions)}
        if self.game_dir:
            out: set[str] = set()
            for pid, (_, name) in DLCS.items():
                d = os.path.join(self.game_dir, "Data", name)
                if os.path.isdir(d):
                    vers = mods.parse_about(d, v)["supported_versions"]
                    if not v or not vers or v in vers:
                        out.add(pid)
            return out
        return set()

    def db_for_mod(self, mod_or_uid) -> DbMod | None:
        m = mod_or_uid if isinstance(mod_or_uid, Mod) else self.installed.get(mod_or_uid)
        wid = m.workshop_id if m else ""
        pid = m.package_id if m else _base_pid(str(mod_or_uid))
        row = self._db("get", wid) if wid else None
        if row:
            return row
        rows = self._db("by_package_id", pid, default=[]) if pid else []
        return rows[0] if rows else None

    # ------------------------------------------------------------ active list editing
    def _set(self, new: list[str]) -> None:
        new = _dedupe(new)
        if new == self.active:
            return
        self._undo.append(self.active)
        del self._undo[:-MAX_UNDO]
        self._redo.clear()
        self.active = new
        self.dirty = True

    def _config_hash(self) -> str | None:
        try:
            with open(os.path.join(self.config_dir or "", "ModsConfig.xml"), "rb") as f:
                return hashlib.sha1(f.read()).hexdigest()
        except OSError:
            return None

    def load_active_from_game(self) -> list[str]:
        self._load_tried = True
        cfg_hash = self._config_hash()                  # before parsing: a change after this point is detected
        known, version = [], ""
        cfg = os.path.join(self.config_dir, "ModsConfig.xml") if self.config_dir else ""
        if cfg and os.path.isfile(cfg):
            # strict read: a locked/damaged file must not load as an empty list (the next Save would wipe it)
            ids, known, version, found = mods._modsconfig_parts(mods._read_text(cfg))
            if not found:
                raise RuntimeError("ModsConfig.xml has no <activeMods> list (damaged, or being written by "
                                   "RimWorld); not loading it. Try Reload again, or restore a backup.")
            known = mods._uniq(known, lower=True)
        else:                                           # never launched: RimWorld's default is Core + owned DLCs
            ids = [CORE_ID, *sorted(self.owned_dlcs(), key=OFFICIAL_IDS.index)]
        new = _dedupe(i.strip().lower() for i in ids)
        if self.active and new != self.active:         # a revert is undoable
            self._undo.append(self.active)
            del self._undo[:-MAX_UNDO]
            self._redo.clear()
        self.active = new
        self.known_expansions = list(known)
        self._config_version = version
        self._cfg_hash = cfg_hash
        self._loaded = True
        self.dirty = False
        return self.active

    def save_active_to_game(self, force: bool = False, running_ok: bool = False) -> str:
        """force=True (Overwrite) skips the 'game is running' and 'changed on disk' checks; running_ok=True
        (Save anyway) skips only the 'game is running' one, so edits made in the game's mod menu still ask."""
        if not self.config_dir:
            raise RuntimeError("RimWorld's config folder was not found. Set it in Settings.")
        if not self._loaded:            # saving the empty startup list would wipe the user's load order
            raise RuntimeError("The mod list has not been loaded yet")
        if not (force or running_ok) and self.is_game_running():
            raise GameRunningError("RimWorld is running. Close it first — the game may overwrite the mod list "
                                   "when it exits.")
        if not force and self._config_hash() != self._cfg_hash:
            raise ExternalChangeError("ModsConfig.xml was changed outside the manager (by RimWorld?) since it was "
                                      "loaded. Overwrite it, or reload it from the game?")
        # Every owned DLC is marked "known" so RimWorld does not re-enable a DLC the user switched off.
        known = _dedupe([*self.known_expansions, *sorted(self.owned_dlcs(), key=OFFICIAL_IDS.index)])
        os.makedirs(self.config_dir, exist_ok=True)
        snap = self.active               # copy-on-write list: an edit made during the write replaces the object
        # The installed version, not the file's: RimWorld resets a ModsConfig from another major.minor.
        version = self.game_version or self._config_version
        backup = mods.write_mods_config(self.config_dir, list(snap), known or None, version, self._dir("backups"),
                                        int(self.settings.get("backup_keep") or 30))
        self._cfg_hash = self._config_hash()
        self._config_version = version
        self.known_expansions = known
        self.dirty = self.active is not snap
        self.log.info("Saved %d active mods to ModsConfig.xml (backup %s)", len(snap), backup or "-")
        return backup or ""

    def set_active(self, uids: list[str]) -> None:
        self._loaded = self._loaded or self._load_tried
        self._set(list(uids))

    def activate(self, uids: list[str], index: int | None = None) -> None:
        add = [u for u in _dedupe(uids) if u not in self.active]
        if not add:
            return
        new = list(self.active)
        i = len(new) if index is None else max(0, min(index, len(new)))
        new[i:i] = add
        self._set(new)

    def deactivate(self, uids: list[str]) -> None:
        drop = set(uids)
        self._set([u for u in self.active if u not in drop])

    def move(self, uids: list[str], to_index: int) -> None:
        """Move the block `uids` (kept in their current relative order) so its first mod ends up at `to_index`
        (clamped): top = 0, bottom = len(active), up one = first index - 1, down one = first index + 1."""
        sel = set(uids)
        block = [u for u in self.active if u in sel]
        if not block:
            return
        rest = [u for u in self.active if u not in sel]
        i = max(0, min(to_index, len(rest)))
        self._set(rest[:i] + block + rest[i:])

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.active)
        self.active = self._undo.pop()
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.active)
        self.active = self._redo.pop()
        self.dirty = True
        return True

    def auto_sort(self) -> list[str]:
        rules = self._db("rules", default={}) or {}      # may wait for a DB rebuild: fetch before the snapshot
        while True:                                       # compare-and-set: re-sort if the user edited meanwhile
            snap = self.active
            order, warnings = sorting.sort_order(snap, self.installed, rules, self.user_rules,
                                                 self.settings.get("sort_tiebreak") or "current")
            if self.active is snap:
                break
        self._set(order)
        return warnings

    def check(self) -> list[Issue]:
        issues = sorting.check(self.active, self.installed, self.db, self.version_short, self.owned_dlcs(),
                               self.user_rules)
        extra = []
        for uid in self.active:
            m = self.installed.get(uid)
            if m and m.source == "steamcmd":
                extra.append(Issue("error", "not_in_game", uid,
                                   f"{m.name} is only in the SteamCMD download folder; RimWorld will not load it. "
                                   "Download it again to install it into the game's Mods folder.",
                                   fix={"action": "download", "refs": [mods.mod_to_ref(m)]} if m.workshop_id else None))
            elif m and m.source == "extra":
                extra.append(Issue("error", "not_in_game", uid,
                                   f"{m.name} is in an extra mod folder; RimWorld only loads its Data and Mods folders "
                                   "and Workshop subscriptions. Link it into the game's Mods folder.",
                                   fix={"action": "install_local", "uids": [uid]}))
        if extra:
            idx = {u: i for i, u in enumerate(self.active)}
            issues = sorted(issues + extra, key=lambda x: (LEVEL_RANK.get(x.level, 3), idx.get(x.uid, len(idx))))
        return issues

    def apply_fix(self, issue: Issue) -> list[ModRef]:
        fix = issue.fix or {}
        action = fix.get("action")
        before = self.active.index(issue.uid) if issue.uid in self.active else None
        if action == "add":
            uids = list(fix.get("uids") or [])
            for u in [u for u in uids if _base_pid(u) == HARMONY_ID]:     # Harmony always loads first
                self.activate([u], 0)
                uids.remove(u)
            if CORE_ID in uids:
                self.activate([CORE_ID], 1 if self.active[:1] == [HARMONY_ID] else 0)
                uids.remove(CORE_ID)
            self.activate(uids, before)
        elif action == "remove":
            self.deactivate(fix.get("uids") or [issue.uid])
        elif action == "sort":
            self.auto_sort()
        elif action == "replace":
            ref, old = fix.get("ref"), fix.get("uid") or issue.uid
            if not ref:
                return []
            w = (ref.workshop_id or "").strip()
            # by workshop id when known: a replacement that keeps the old packageId is still a different upload
            new = next((u for u in self._by_wid.get(w, ()) if self._loadable(u)), None) if w.isdigit() \
                else self.match_ref(ref)
            if not new or new == old:
                return [self._fill_ref(ref, keep_wid=True)]
            o = self.installed.get(old)
            p = os.path.abspath(o.path) if o else ""
            in_mods = bool(self.game_dir) and os.path.normcase(os.path.dirname(p)) == os.path.normcase(
                os.path.abspath(os.path.join(self.game_dir, "Mods")))
            if w.isdigit() and o and o.package_id == self.installed[new].package_id and in_mods \
                    and not os.path.islink(p) and not os.path.isjunction(p) and not os.path.exists(os.path.join(p, ".git")):
                # a same-packageId upload was asked for by id: RimWorld loads one copy only, so the replaced
                # folder goes to the app's backups (a '_dupN' fix has no id and never moves anything)
                if self.is_game_running():
                    raise RuntimeError("Close RimWorld before replacing a mod folder.")
                bak = os.path.join(self._dir("backups", "Mods"), f"{os.path.basename(p)}-{time.strftime('%Y%m%d-%H%M%S')}")
                try:
                    os.rename(p, bak)                   # atomic on one volume; else back up, then delete
                except OSError:
                    try:
                        shutil.copytree(p, bak, symlinks=True)
                    except OSError as e:
                        shutil.rmtree(bak, ignore_errors=True)
                        raise RuntimeError(f"Could not back up {p}: {e}") from None
                    try:
                        steam._remove(p)                # clears read-only attributes
                    except OSError as e:
                        self.scan()
                        raise RuntimeError(f"{p} was only partly removed ({e}); a full copy is in {bak}. "
                                           "Close whatever uses it and delete the folder.") from None
                self.scan()
                new = next((u for u in self._by_wid.get(w, ()) if self._loadable(u)), new)
            self._set([new if u == old else u for u in self.active])
        elif action == "download":
            need = []
            for ref in fix.get("refs") or []:
                uid = self.match_ref(ref)
                if self._loadable(uid):
                    self.activate([uid], before)
                else:
                    need.append(self._fill_ref(ref, keep_wid=True))
            return need
        elif action == "install_local":
            for uid in fix.get("uids") or [issue.uid]:
                self._install_local(uid)
        return []

    def _install_local(self, uid: str) -> None:
        """Junction (or copy, when junctions fail) an 'extra'-folder mod into <game>/Mods, rescan, keep it active."""
        m = self.installed.get(uid)
        if not m or m.source != "extra":
            return
        if not self.game_dir:
            raise RuntimeError("RimWorld's folder was not found. Set it in Settings.")
        mods_dir = os.path.join(self.game_dir, "Mods")
        os.makedirs(mods_dir, exist_ok=True)
        src = os.path.abspath(m.path)
        dest = os.path.join(mods_dir, os.path.basename(os.path.normpath(src)))
        if os.path.lexists(dest):
            raise FileExistsError(f"{dest} already exists; move or rename it first.")
        try:
            import _winapi
            _winapi.CreateJunction(src, dest)
        except (ImportError, AttributeError, OSError) as e:
            self.log.info("Junction %s -> %s failed (%s); copying instead", dest, src, e)
            try:
                shutil.copytree(src, dest)
            except BaseException:
                shutil.rmtree(dest, ignore_errors=True)
                raise
        self.scan()
        new = next((u for u, x in self.installed.items()
                    if os.path.normcase(os.path.normpath(x.path)) == os.path.normcase(dest)), None)
        if new and new != uid:
            self._set([new if u == uid else u for u in self.active])

    def resolve_dependencies(self) -> tuple[list[str], list[ModRef], list[str]]:
        add, to_download, unresolvable = sorting.resolve_dependencies(self.active, self.installed, self.db,
                                                                      self.version_short)
        add = [u for u in _dedupe(add) if u not in self.active]
        if add:
            new = self.active + add

            def first_dependent(uid: str) -> int | None:
                pid = self.installed[uid].package_id if uid in self.installed else _base_pid(uid)
                return next((i for i, u in enumerate(new) if (m := self.installed.get(u))
                             and any(pid in d.ids() for d in m.dependencies)), None)

            for _ in range(len(add) + 1):              # place each dependency right before its first dependent
                moved = False
                for uid in add:
                    i, j = new.index(uid), first_dependent(uid)
                    if j is not None and j < i:
                        new.insert(j, new.pop(i))
                        moved = True
                if not moved:
                    break
            new.sort(key=lambda u: not (u in add and _base_pid(u) == HARMONY_ID))   # an added Harmony goes first
            self._set(new)
        return add, to_download, unresolvable

    # ------------------------------------------------------------ refs / matching
    def match_ref(self, ref: ModRef) -> str | None:
        """Installed uid for a ref: exact uid, package id (non-_steam first), workshop id, then a unique name
        (the name fallback is used only when the ref has no package id — a different id means a different mod)."""
        pid = (ref.package_id or "").strip().lower()
        wid = (ref.workshop_id or "").strip()
        inst = self.installed                   # a worker-thread scan() may swap these: drop stale uids
        if pid and pid in inst:
            return pid
        if pid:
            cands = [u for u in self._by_pid.get(_base_pid(pid), ()) if u in inst]
            if cands:
                return next((u for u in cands if wid and inst[u].workshop_id == wid), cands[0])
        if wid:
            cands = [u for u in self._by_wid.get(wid, ()) if u in inst]
            if cands:
                return cands[0]
        name = (ref.name or "").strip().lower()
        if name and not pid:
            cands = [u for u in self._by_name.get(name, ()) if u in inst]
            if cands and len(cands) == 1:
                return cands[0]
        return None

    def _resolve(self, ref: ModRef) -> tuple[ModRef, str | None]:
        """(copy of ref with package id / name / workshop id filled from installed mods, then the DB; installed uid)."""
        r = ModRef((ref.package_id or "").strip().lower(), (ref.name or "").strip(), (ref.workshop_id or "").strip())
        if r.workshop_id == "0":                                  # .rml: '0' = not a workshop item
            r = ModRef(r.package_id, r.name, "")
        elif r.workshop_id == RIMWORLD_APPID or r.workshop_id in DLC_BY_APPID:   # .rml Core/DLC ids are appids
            r = ModRef(r.package_id or DLC_BY_APPID.get(r.workshop_id, CORE_ID), r.name, "")
        uid = self.match_ref(r)
        m = self.installed.get(uid) if uid else None    # the installed copy is authoritative
        if m:
            return ModRef(r.package_id or m.package_id, m.name or r.name, m.workshop_id or r.workshop_id), uid
        uid = None                                      # stale uid from a concurrent rescan
        if _base_pid(r.package_id) in OFFICIAL_IDS:
            dlc = DLCS.get(_base_pid(r.package_id))
            return ModRef(r.package_id, r.name or (dlc[1] if dlc else "Core"), ""), None
        row = None
        if r.workshop_id:
            row = self._db("get", r.workshop_id)
        elif r.package_id:
            rows = self._db("by_package_id", _base_pid(r.package_id), default=[]) or []
            row = next((x for x in rows if not x.unpublished), rows[0] if rows else None)
        if row and not row.is_dlc:
            r = ModRef(r.package_id or row.package_id, r.name or row.display_name, r.workshop_id or row.workshop_id)
            uid = self.match_ref(r)
        return r, uid

    def _fill_ref(self, ref: ModRef, keep_wid: bool = False) -> ModRef:
        """_resolve()'s ref. keep_wid (downloads): an explicit workshop id is never swapped for the installed
        copy's (a 'Dev Snapshot' / continuation can share the packageId). A ref with no workshop id and no
        installed copy gets one from a unique exact DB title match (.rml lists say '0' for every local mod)."""
        r, uid = self._resolve(ref)
        w = (ref.workshop_id or "").strip()
        gh_repo = steam.parse_github_repo(w)
        if gh_repo:
            w = f"gh:{gh_repo}"
        if keep_wid and (w.isdigit() or w.startswith("gh:")) and w != "0" and w != r.workshop_id and _base_pid(r.package_id) not in OFFICIAL_IDS:
            r = ModRef(r.package_id, (ref.name or "").strip() or r.name, w)
        pid = _base_pid(r.package_id)
        key = _title_key(r.name) if not r.workshop_id and not uid and pid not in OFFICIAL_IDS else ""
        if key:
            hits = [x for x in self._db("search", r.name, "", 20, default=[]) or []
                    if not x.is_dlc and x.package_id in ("", pid or x.package_id)
                    and key in (_title_key(x.name), _title_key(x.steam_name))]
            ok = [x for x in hits if sorting._supports(x.game_versions, self.version_short)]
            one = ok if len(ok) == 1 else hits if len(hits) == 1 else []
            if one:                                       # values from the DB row only, never guessed
                r = ModRef(r.package_id or one[0].package_id, r.name, one[0].workshop_id)
        return r

    def _loadable(self, uid: str | None) -> bool:
        """Installed where RimWorld loads it (not only in the SteamCMD cache or an extra folder)."""
        m = self.installed.get(uid) if uid else None
        return m is not None and m.source in LOADABLE_SOURCES

    def _fill_refs(self, refs: list[ModRef]) -> list[ModRef]:
        out, seen = [], set()
        for r in refs:
            f = self._fill_ref(r)
            k = f.package_id or f.workshop_id or f.name.lower()
            if k and k not in seen:
                seen.add(k)
                out.append(f)
        return out

    def _ref_for_uid(self, uid: str) -> ModRef:
        m = self.installed.get(uid)
        if m:   # like RimWorld, a workshop duplicate keeps its '_steam' id so both copies survive in lists
            return ModRef(uid if uid.endswith("_steam") else m.package_id, m.name, m.workshop_id)
        return self._fill_ref(self._hints.get(uid) or ModRef(package_id=uid))

    def refs_status(self, refs: list[ModRef]) -> list[tuple[ModRef, str]]:
        out = []
        for ref in refs:
            r, uid = self._resolve(ref)
            out.append((r, "installed" if self._loadable(uid) else "missing" if r.workshop_id else "unknown"))
        return out

    # ------------------------------------------------------------ modlists
    def _modlist_file(self, name: str) -> str:
        return os.path.join(self._dir("modlists"), _safe_name(name) + ".json")

    def list_modlists(self) -> list[ModList]:
        d = self._dir("modlists")
        out = []
        for fn in os.listdir(d):
            if fn.lower().endswith(".json"):
                p = os.path.join(d, fn)
                try:
                    ml = mods.load_modlist_json(p)
                except Exception as e:  # noqa: BLE001 - one bad file must not hide the others
                    self.log.warning("Skipping unreadable modlist %s: %s", fn, e)
                    continue
                if os.path.normcase(p) != os.path.normcase(self._modlist_file(ml.name)):
                    ml.name = os.path.splitext(fn)[0]   # copied/renamed file: show, rename, delete it by its file
                ml._file = p                            # the stem may not map back (' Copy', >120 chars)
                out.append(ml)
        return sorted(out, key=lambda m: m.name.lower())

    def unique_modlist_name(self, name: str) -> str:
        """name, or the first 'name (2)', 'name (3)'… whose file is free. File names ignore case and map
        characters like ':' and '?' to '_', so different names can share a file."""
        name = (name or "").strip() or "Unnamed"
        base, out, n = name[:110].rstrip(), name, 2    # _safe_name cuts at 120: keep the suffix inside it
        while os.path.exists(self._modlist_file(out)):
            out, n = f"{base} ({n})", n + 1
        return out

    def save_modlist(self, ml: ModList, overwrite: bool = False) -> None:
        """Never clobbers a DIFFERENT list: when ml's file holds another name (e.g. 'Colony Run' vs 'colony run'),
        ml.name becomes the first free 'Name (2)'… (callers read ml.name back). overwrite=True writes regardless."""
        with self._ml_lock:
            p = self._modlist_file(ml.name)
            if not overwrite and os.path.exists(p):
                try:
                    same = mods.load_modlist_json(p).name == ml.name
                except Exception:  # noqa: BLE001 - an unreadable file is somebody else's: keep it
                    same = False
                if not same:
                    ml.name = self.unique_modlist_name(ml.name)
                    p = self._modlist_file(ml.name)
            now = time.time()
            ml.created = ml.created or now
            ml.updated = now
            mods.save_modlist_json(ml, p)

    def _modlist_path(self, name: str) -> str:
        """The file list_modlists showed name from; _modlist_file(name) for a list not on disk."""
        return next((ml._file for ml in self.list_modlists() if ml.name == name), None) or self._modlist_file(name)

    def delete_modlist(self, name: str) -> None:
        p = self._modlist_path(name)
        if not os.path.isfile(p):
            raise FileNotFoundError(f'No modlist file for "{name}".')
        os.remove(p)

    def rename_modlist(self, old: str, new: str) -> None:
        new = (new or "").strip()
        if not new:
            raise ValueError("The new name is empty.")
        src, dst = self._modlist_path(old), self._modlist_file(new)
        same = os.path.normcase(src) == os.path.normcase(dst)
        if not same and os.path.exists(dst):
            raise FileExistsError(f'A modlist named "{new}" already exists.')
        ml = mods.load_modlist_json(src)
        ml.name = new
        self.save_modlist(ml, overwrite=True)          # a case-only rename writes over its own file
        if not same:
            os.remove(src)

    def modlist_from_active(self, name: str) -> ModList:
        now = time.time()
        return ModList(name=name, mods=[self._ref_for_uid(u) for u in self.active], game_version=self.game_version,
                       source="manual", created=now, updated=now)

    def apply_modlist(self, ml: ModList) -> list[ModRef]:
        """Active = the list's installed mods in list order; not-installed mods with a known package id stay in the
        active list (shown red) so they are active once downloaded. RimWorld itself drops not-installed ids at
        startup (and writes that back if the mod list is saved in game), so download them before launching.
        A list without any Core/DLC entry (AI picks, collections, pasted ids) keeps the Core/DLCs active now.
        Returns every not-installed ref (with workshop ids filled from the DB) for downloading."""
        self._loaded = self._loaded or self._load_tried
        new, missing = [], []
        for ref in ml.mods:
            r, uid = self._resolve(ref)
            if self._loadable(uid):
                new.append(uid)
                continue
            missing.append(r)
            if r.package_id:
                new.append(r.package_id)
                self._hints[r.package_id] = r
        if not any(_base_pid(u) in OFFICIAL_IDS for u in new):
            owned = self.owned_dlcs()
            keep_off = {CORE_ID, *(u for u in self.active if _base_pid(u) in OFFICIAL_IDS)}
            if ml.source == "ai":
                keep_off.update(owned)
            else:
                for u in new:
                    m = self.installed.get(u)
                    if m:
                        keep_off.update(d for d in m.dependencies if d in owned)
            off = [p for p in OFFICIAL_IDS if p in keep_off]
            i = 1 if new and _base_pid(new[0]) == HARMONY_ID else 0     # Harmony stays first
            new[i:i] = off
        if ml.source == "ai":
            rules = self._db("rules", default={}) or {}
            new = sorting.sort_order(new, self.installed, rules, self.user_rules,
                                     self.settings.get("sort_tiebreak") or "current")[0]
        self._set(new)
        return missing

    def import_path(self, path: str) -> ModList:
        ml = mods.import_any(path)
        ml.mods = self._fill_refs(ml.mods)
        return ml

    def import_text(self, text: str, progress: Progress = noop_progress) -> ModList:
        now = time.time()
        text = text or ""
        m = re.search(r"RMM1:", text, re.I)
        if m:                                   # decode_share_code handles line-wrapped codes and trailing words
            ml = mods.decode_share_code(text[m.start():])
            ml.mods = self._fill_refs(ml.mods)
            ml.source = "import:share"
            return ml
        refs = mods.parse_text_list(text)
        if not refs:
            raise ValueError("No mods found. Paste workshop links/ids, package ids, a collection link or a share code.")
        # Only ids unknown to both the installed mods and the DB can be collections.
        unknown = _dedupe(r.workshop_id for r in refs if r.workshop_id and not r.package_id
                          and not self.match_ref(r) and not self._db("get", r.workshop_id))
        kids: dict[str, list[str]] = {}
        if unknown:
            progress(None, f"Checking {len(unknown)} workshop id(s) for collections…")
            try:
                tree = steam._collection_tree(unknown)
                kids = {c: steam._flatten([c], tree) for c in unknown if c in tree}
            except Exception as e:  # noqa: BLE001 - offline: keep the ids as they are
                self.log.warning("Collection expansion failed: %s", e)
        # each collection's children go where its link was; _fill_refs drops repeats
        refs = [x for r in refs for x in ([ModRef(workshop_id=w) for w in kids[r.workshop_id]]
                                          if not r.package_id and r.workshop_id in kids else [r])]
        collections = list(kids)
        mods_ = self._fill_refs(refs)
        name, notes = "Imported list", ""
        nameless = [r.workshop_id for r in mods_ if r.workshop_id and not r.name]
        if nameless or collections:
            progress(None, f"Fetching {len(nameless) + len(collections)} title(s) from Steam…")
            try:
                det = steam.get_details(nameless + collections, db=self.db)
            except Exception as e:  # noqa: BLE001
                self.log.warning("Could not fetch workshop titles: %s", e)
                det = {}
            bad, asked = [], set(nameless)
            for i, r in enumerate(mods_):
                d = det.get(r.workshop_id) or {}
                if d.get("title") and not r.name:
                    mods_[i] = ModRef(r.package_id, d["title"], r.workshop_id)
                if det and r.workshop_id in asked and (not d or d.get("result", 1) != 1
                                                       or str(d.get("consumer_app_id", RIMWORLD_APPID)) != RIMWORLD_APPID):
                    bad.append(r.workshop_id)
            titles = [det.get(c, {}).get("title") for c in collections]
            if len(collections) == 1 and titles[0]:
                name = titles[0]
            if bad:
                notes = f"Not RimWorld workshop items (or removed): {', '.join(bad)}"
        progress(1.0, f"Imported {len(mods_)} mod(s)")
        return ModList(name=name, mods=mods_, game_version=self.game_version, notes=notes,
                       source="import:collection" if collections else "import:text", created=now, updated=now)

    def export_modlist(self, ml: ModList, path: str, fmt: str) -> None:
        ml = ModList(ml.name, self._fill_refs(ml.mods), ml.game_version or self.game_version, ml.notes, ml.source,
                     ml.created, ml.updated)
        if fmt == "rml":
            mods.write_rml(ml, path, ml.game_version, self.installed)
        elif fmt == "modsconfig":
            uids = _dedupe(self.match_ref(r) or r.package_id for r in ml.mods)
            with tempfile.TemporaryDirectory(prefix="rmm_export_") as tmp:   # installed version: RimWorld resets others
                mods.write_mods_config(tmp, uids, list(DLCS), self.game_version or ml.game_version)
                shutil.copyfile(os.path.join(tmp, "ModsConfig.xml"), path)
        elif fmt == "txt":
            mods.write_text_list(ml, path)
        elif fmt == "json":
            mods.save_modlist_json(ml, path)
        elif fmt == "html":
            mods.write_html_list(ml, path)
        else:
            raise ValueError(f"Unknown export format: {fmt}")

    def _game_modlists_dir(self) -> str:
        if not self.config_dir:
            raise RuntimeError("RimWorld's config folder was not found. Set it in Settings.")
        return os.path.join(paths.user_data_dir(self.config_dir), "ModLists")

    def export_to_game_modlists(self, ml: ModList, overwrite: bool = False) -> str:
        """Write <user data>/ModLists/<name>.rml. A same-name list (ignoring case) raises FileExistsError unless
        overwrite=True; then it is backed up into the app's backups/ModLists first."""
        d = self._game_modlists_dir()
        os.makedirs(d, exist_ok=True)
        fn = _safe_name(ml.name) + ".rml"
        p = os.path.join(d, next((f for f in os.listdir(d) if f.lower() == fn.lower()), fn))
        if os.path.isfile(p):
            if not overwrite:
                raise FileExistsError(f'RimWorld already has a saved mod list named "{os.path.basename(p)[:-4]}".')
            shutil.copy2(p, os.path.join(self._dir("backups", "ModLists"),
                                         f"{os.path.basename(p)[:-4]}-{time.strftime('%Y%m%d-%H%M%S')}.rml"))
        self.export_modlist(ml, p, "rml")
        return p

    def game_modlists(self) -> list[str]:
        d = self._game_modlists_dir() if self.config_dir else ""
        if not d or not os.path.isdir(d):
            return []
        return sorted((os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".rml")),
                      key=lambda p: os.path.basename(p).lower())

    def share_code(self, ml: ModList) -> str:
        return mods.encode_share_code(ModList(ml.name, self._fill_refs(ml.mods), ml.game_version, ml.notes, ml.source,
                                              ml.created, ml.updated))

    # ------------------------------------------------------------ downloads
    def missing_refs(self, refs: list[ModRef] | None = None) -> list[ModRef]:
        if refs is None:   # like check(): an active 'x_steam' with a loadable 'x' installed needs a replace, not a download
            out = [self._ref_for_uid(u) for u in self.active if _base_pid(u) not in OFFICIAL_IDS
                   and not any(self._loadable(x) for x in self._by_pid.get(_base_pid(u), ()))]
            try:
                out += sorting.resolve_dependencies(self.active, self.installed, self.db, self.version_short)[1]
            except Exception as e:  # noqa: BLE001
                self.log.warning("Dependency resolution failed: %s", e)
            out = [self._fill_ref(r, keep_wid=True) for r in out]
        else:
            out = [r for r, uid in (self._resolve(x) for x in refs) if not self._loadable(uid)]
        seen, uniq = set(), []
        for r in out:
            k = r.workshop_id or r.package_id or r.name
            if k and k not in seen:
                seen.add(k)
                uniq.append(r)
        return uniq

    def dep_ref(self, pid: str, name: str, hint: str) -> ModRef:
        """Download ref for a missing About.xml dependency: the DB upload of pid that fits the game version beats a
        stale steamWorkshopUrl (e.g. a '[1.0]' legacy upload); an id the DB does not know is kept."""
        wid, dbname, _ = sorting._lookup_wid(pid, hint, self.db, self.version_short, self._by_wid)
        return ModRef(pid, name or dbname or pid, wid or hint)

    def expand_download_deps(self, refs: list[ModRef]) -> list[ModRef]:
        """refs + the DB dependencies that still need downloading (see sorting.download_closure)."""
        return sorting.download_closure(refs, self.installed, self.db, self.version_short)

    def download(self, refs: list[ModRef], method: str | None = None, progress: Progress = noop_progress,
                 cancel=None, *, activate: bool = False, with_deps: bool = True, update: bool = False
                 ) -> list[DownloadResult]:
        """with_deps: also the DB dependencies that still need downloading (results include them).
        activate: afterwards activate every downloaded mod that is not active yet, then auto-sort.
        update: a mod installed as Mods/<other name> is replaced in place when the download has its packageId
        (the old folder is copied to the app's backups first); otherwise every download goes to Mods/<id>."""
        method = method or self.settings.get("download_method") or "steamcmd"
        if with_deps:
            refs = self.expand_download_deps(refs)
        results: list[DownloadResult] = []
        ids: list[str] = []
        gh_ids: list[str] = []
        pids: dict[str, str] = {}
        for r in (self._fill_ref(x, keep_wid=True) for x in refs):
            label = r.name or r.package_id or r.workshop_id
            gh_repo = steam.parse_github_repo(r.workshop_id)
            if _base_pid(r.package_id) in OFFICIAL_IDS:
                results.append(DownloadResult(r.workshop_id, False, f"{label}: official content — install it in Steam"))
            elif gh_repo:
                gh_wid = f"gh:{gh_repo}"
                if gh_wid not in gh_ids:
                    gh_ids.append(gh_wid)
                    pids[gh_wid] = r.package_id
            elif not r.workshop_id.isdigit():
                results.append(DownloadResult(r.workshop_id, False,
                                              f"{label}: no Steam Workshop or GitHub id known — find it in the Workshop tab"))
            elif r.workshop_id not in ids:
                ids.append(r.workshop_id)
                pids[r.workshop_id] = r.package_id
        if gh_ids:
            if not self.game_dir:
                for gh_wid in gh_ids:
                    results.append(DownloadResult(gh_wid, False, "RimWorld's folder was not found. Set it in Settings."))
            else:
                mods_dir = os.path.join(self.game_dir, "Mods")
                with self._install_lock:
                    gh_res = steam.download_github_mods(gh_ids, mods_dir, progress, cancel)
                results += [gh_res.get(g) or DownloadResult(g, False, "GitHub download failed") for g in gh_ids]
        if not ids and not gh_ids:
            return results
        if ids:
            if method in ("steamcmd", "steamworks"):
                self._check_space(ids, method)
            self.log.info("Downloading %d item(s) via %s", len(ids), method)
            if method == "steamcmd":
                res = self._download_steamcmd(ids, progress, cancel, update)
                # an update's Mods/ copy stays: a Steam subscription would install the packageId a second time
                results += res if update else self._steamworks_fallback(res, progress, cancel)
            elif method == "steamworks":
                results += self._download_steamworks(ids, progress, cancel)
            elif method == "steam_client":
                in_steam = steam.open_in_steam(ids)               # False: no steam:// handler, all went to the browser
                results += [DownloadResult(i, False, f"opened in {'Steam' if in_steam and not n else 'the browser'}"
                                                     " — subscribe there")
                            for n, i in enumerate(ids)]           # the client shows one page: the rest open in the browser
            elif method == "browser":
                results += self._download_browser(ids, progress, cancel)
            else:
                raise ValueError(f"Unknown download method: {method}")
        done = [r.workshop_id for r in results if r.ok]
        if done:
            progress(None, "Rescanning mods…")
            self.scan(progress)
        got = [next((u for u in self._by_wid.get(w, ()) if self._loadable(u)), None)
               or self.match_ref(ModRef(pids.get(w, ""))) for w in done]
        deps: list[str] = []
        if with_deps and any(got):   # About.xml dependencies Steam does not list as required items (HugsLib → Harmony)
            deps, more, _ = sorting.resolve_dependencies([u for u in got if u], self.installed, self.db,
                                                         self.version_short)
            more = [r for r in more if r.workshop_id not in ids and r.workshop_id not in gh_ids]   # each round only adds newly installed mods
            if more:
                results += self.download(more, method, progress, cancel, activate=activate, update=update)
        if activate and done:
            have, add = {_base_pid(u) for u in self.active}, []
            for uid in got + deps:   # a copy whose packageId is active already would only be a duplicate
                if self._loadable(uid) and self.installed[uid].package_id not in have:
                    add.append(uid)
                    have.add(self.installed[uid].package_id)
            if add:
                self.activate(add)
                self.auto_sort()                          # dependencies before dependents, Harmony first
        return results

    def _check_space(self, ids: list[str], method: str) -> None:
        """Raise NotEnoughSpaceError when the Workshop sizes of ids do not fit; offline / unknown sizes skip the check.
        SteamCMD needs room in Mods/ and in its content dir (copy/link keep that copy; move only frees it afterwards)."""
        try:
            det = steam.get_details(ids, db=self.db)
        except Exception as e:  # noqa: BLE001 - offline: download anyway
            self.log.info("Disk space check skipped: %s", e)
            return
        total = sum(int(d.get("file_size") or 0) for d in det.values() if str(d.get("file_size") or 0).isdigit())
        if not total:
            return
        mods_dir = os.path.join(self.game_dir, "Mods") if self.game_dir else ""
        dirs = [mods_dir] if method == "steamcmd" else [self.workshop_dir or mods_dir]
        if method == "steamcmd":
            dirs.append(self.steamcmd().content_dir)
        need: dict[str, int] = {}
        for n, d in enumerate(d for d in dirs if d):
            drive = os.path.splitdrive(os.path.abspath(d))[0] + os.sep
            if n and drive in need and (self.settings.get("install_mode") or "move") == "move":
                continue              # moved within one drive: the content dir copy is gone before Mods/ grows
            need[drive] = need.get(drive, 0) + total
        for drive, size in need.items():
            size = int(size * 1.1) + SPACE_MARGIN
            try:
                free = shutil.disk_usage(drive).free
            except OSError:
                continue
            if free < size:
                raise NotEnoughSpaceError(f"Not enough disk space: need about {size / 1024 ** 3:.1f} GB on "
                                          f"{drive.rstrip(os.sep) or drive}, only {free / 1024 ** 3:.1f} GB free.")

    def _download_steamcmd(self, ids: list[str], progress: Progress, cancel, update: bool = False
                           ) -> list[DownloadResult]:
        if not self.game_dir:
            raise RuntimeError("RimWorld's folder was not found. Set it in Settings before downloading with SteamCMD.")
        sc = self.steamcmd()                    # sc.download installs SteamCMD itself (cancel/offline -> per-item)
        res = sc.download(ids, progress, cancel, retries=int(self.settings.get("steamcmd_retries", 3)))   # 0 = no retries
        mods_dir = os.path.join(self.game_dir, "Mods")
        os.makedirs(mods_dir, exist_ok=True)
        with self._install_lock:
            for msg in steam.cleanup_stale_installs(mods_dir):
                self.log.warning("%s", msg)
            res = steam.install_items(res, mods_dir, self.settings.get("install_mode") or "move",
                                      targets=self._install_targets(res, mods_dir) if update else None)
        return [res.get(i) or DownloadResult(i, False, "no result from SteamCMD") for i in ids]

    def _steamworks_fallback(self, res: list[DownloadResult], progress: Progress, cancel) -> list[DownloadResult]:
        """SteamCMD failures (not cancels, bad ids or removed items) are subscribed through Steam when it runs;
        they land in Steam's workshop folder, which scan() reads. A Steam failure keeps SteamCMD's message too."""
        failed = [r.workshop_id for r in res if not r.ok and not _PERMANENT_FAIL.search(r.message or "")]
        if not failed or not self.settings.get("steamcmd_fallback_steamworks", True) \
                or (cancel is not None and cancel.is_set()) \
                or not self.game_dir or not steam.steamworks_available(self.game_dir):
            return res
        progress(None, f"SteamCMD could not get {len(failed)} mod(s); subscribing them through Steam instead…")
        try:
            sw = {r.workshop_id: r for r in self._download_steamworks(failed, progress, cancel)}
        except RuntimeError as e:
            sw = {i: DownloadResult(i, False, str(e)) for i in failed}
        return [r if r.workshop_id not in sw else sw[r.workshop_id] if sw[r.workshop_id].ok or sw[r.workshop_id].message == "cancelled"
                else DownloadResult(r.workshop_id, False, f"{r.message}; Steam: {sw[r.workshop_id].message}", r.path)
                for r in res]

    def _uninstall_roots(self) -> set[str]:
        roots = [os.path.join(self.game_dir, "Mods") if self.game_dir else "", self.workshop_dir or "",
                 self.steamcmd().content_dir]
        return {steam._norm(r) for r in roots if r and os.path.isdir(r)}

    def unused_mods(self) -> list[tuple[Mod, int]]:
        """(mod, folder size in bytes) for deletable mods that are neither active nor in any saved modlist.
        A linked folder counts 0 bytes: links are never followed."""
        if not self._loaded:   # an empty active list would offer every mod the user plays with for deletion
            raise RuntimeError("The mod list has not been loaded yet — press F5 (Reload) first")
        used = set(self.active) | {self.match_ref(r) for ml in self.list_modlists() for r in ml.mods}
        roots = self._uninstall_roots()

        def size(path: str) -> int:
            total = 0
            try:
                with os.scandir(path) as it:
                    for e in it:
                        if e.is_symlink() or e.is_junction():
                            continue
                        total += size(e.path) if e.is_dir() else e.stat().st_size
            except OSError:
                pass
            return total

        return [(m, 0 if os.path.islink(m.path) or os.path.isjunction(m.path) else size(m.path))
                for u, m in list(self.installed.items()) if u not in used and not m.is_official
                and steam._norm(os.path.dirname(os.path.abspath(m.path))) in roots]

    def uninstall(self, uids: list[str], unsubscribe: bool = True, progress: Progress = noop_progress) -> list[str]:
        """Delete mod folders and (for Steam workshop mods) unsubscribe. Core/DLCs and folders outside the game's
        Mods/, the workshop folder and the SteamCMD cache are never touched; a Mods/ install's SteamCMD cache copy is
        deleted with it unless another installed copy has that workshop id. Returns one line per problem."""
        if self.is_game_running():
            raise GameRunningError("Close RimWorld before deleting mods.")
        roots = self._uninstall_roots()
        mods_ = [m for u in _dedupe(uids) if (m := self.installed.get(u)) and not m.is_official]
        gone = {m.uid for m in mods_}
        problems: list[str] = []
        subs = [m.workshop_id for m in mods_ if m.source == "workshop" and m.workshop_id]
        if unsubscribe and subs:
            if not self.game_dir or not steam.steamworks_available(self.game_dir):
                problems.append("Could not unsubscribe (Steam not available): Steam may download those mods again.")
            else:
                r = steam.steamworks_call("unsubscribe", subs, self.game_dir, progress, timeout=120)
                items = r.get("items") or {}
                bad = [w for w in subs if not (items.get(w) or {}).get("ok")]
                if bad:
                    problems.append(f"Unsubscribe failed for {len(bad)} mod(s) ({r.get('error') or 'Steam error'}): "
                                    "Steam may download them again.")
        with self._install_lock:
            for k, m in enumerate(mods_):
                progress(k / max(1, len(mods_)), f"Deleting {m.name}…")
                # the folder itself may be a junction (resolve its parent only): delete the link, never its target
                parent = steam._norm(os.path.dirname(os.path.abspath(m.path)))
                if parent not in roots:
                    problems.append(f"{m.name}: not deleted, it is outside the game/workshop folders ({m.path})")
                    continue
                try:
                    if os.path.lexists(m.path):
                        steam._remove(m.path)
                except OSError as e:
                    problems.append(f"{m.name}: {e}")
                    continue
                # a 'link'/'copy' install leaves SteamCMD's cache copy, which the rescan would show again
                wid = m.workshop_id
                cache = os.path.join(self.steamcmd().content_dir, wid) if wid.isdigit() else ""
                if not cache or m.source != "local" or steam._norm(os.path.dirname(cache)) not in roots \
                        or not os.path.lexists(cache) or any(
                            o.workshop_id == wid and o.uid not in gone and o.source != "steamcmd"
                            for o in self.installed.values()):
                    continue                            # another installed copy may still link to it
                try:
                    steam._remove(cache)
                except OSError as e:
                    problems.append(f"{m.name}: SteamCMD cache copy not deleted: {e}")
        self.deactivate([m.uid for m in mods_])
        self.scan()
        return problems

    def _install_targets(self, res: dict[str, DownloadResult], mods_dir: str) -> dict[str, str]:
        """Update of a mod living in Mods/<other name>: replace that folder instead of adding Mods/<id> (a second
        copy = duplicate packageId), but only when the download has that mod's packageId (a stale
        PublishedFileId.txt must not cost the user their own mod). The old folder is copied to the app's backups
        first (a failed copy fails that item); git checkouts are never replaced."""
        nd = os.path.normcase(os.path.abspath(mods_dir))
        out: dict[str, str] = {}
        for wid, r in res.items():
            if not r.ok or not r.path or os.path.lexists(os.path.join(mods_dir, wid)):
                continue                                  # the default Mods/<id> copy is the one replaced
            pid = mods.parse_about(r.path).get("package_id")
            for uid in self._by_wid.get(wid, ()):
                m = self.installed[uid]
                p = os.path.normpath(os.path.abspath(m.path))
                if not pid or m.package_id != pid or os.path.normcase(os.path.dirname(p)) != nd \
                        or os.path.exists(os.path.join(p, ".git")):
                    continue
                # ponytail: backups/Mods is never pruned; prune like ModsConfig backups if hand-named updates get common
                bak = os.path.join(self._dir("backups", "Mods"), f"{os.path.basename(p)}-{time.strftime('%Y%m%d-%H%M%S')}")
                try:
                    shutil.copytree(p, bak, symlinks=True)
                    out[wid] = p
                except OSError as e:
                    shutil.rmtree(bak, ignore_errors=True)
                    res[wid] = DownloadResult(wid, False, f"not updated: could not back up {p}: {e}", r.path)
                break
        return out

    def _download_steamworks(self, ids: list[str], progress: Progress, cancel=None) -> list[DownloadResult]:
        if not self.game_dir or not steam.steamworks_available(self.game_dir):
            raise RuntimeError("Steam download is unavailable: Steam must be running and the Steam version of "
                               "RimWorld installed. Use SteamCMD instead.")
        # the helper waits while Steam makes progress (300 mods: 5-15 min); this cap is only a backstop
        r = steam.steamworks_call("download", ids, self.game_dir, progress, timeout=max(1800.0, 20.0 * len(ids)),
                                  cancel=cancel)
        items = r.get("items") or {}
        if not r.get("ok") and not items:
            if cancel is not None and cancel.is_set():
                return [DownloadResult(i, False, "cancelled") for i in ids]
            raise RuntimeError(r.get("error") or "Steamworks call failed")
        out = []
        for i in ids:
            it = items.get(i) or {}
            ok = bool(it.get("ok", it.get("installed")))
            msg = it.get("message") or ("installed" if ok else r.get("error") or "not installed yet")
            out.append(DownloadResult(i, ok, str(msg), str(it.get("path") or "")))
        if not self.workshop_dir:           # first subscription creates Steam's workshop folder
            self.workshop_dir = paths.find_workshop_dir(self.game_dir, self.settings)
        return out

    def _download_browser(self, ids: list[str], progress: Progress, cancel) -> list[DownloadResult]:
        n = max(1, int(self.settings.get("browser_batch") or 10))
        batches = [ids[i:i + n] for i in range(0, len(ids), n)]
        opened = 0
        for k, batch in enumerate(batches):
            steam.open_in_browser(batch)
            opened += len(batch)
            if k + 1 < len(batches):
                progress(opened / len(ids), f"Opened {opened}/{len(ids)} pages — next batch in {BROWSER_PAUSE_S}s")
                stopped = cancel.wait(BROWSER_PAUSE_S) if cancel is not None else (time.sleep(BROWSER_PAUSE_S) or False)
                if stopped:
                    break
        progress(1.0, f"Opened {opened}/{len(ids)} workshop pages")
        return [DownloadResult(i, False, "opened in browser — subscribe there" if k < opened else "cancelled")
                for k, i in enumerate(ids)]

    def check_updates(self, progress: Progress = noop_progress) -> list[tuple[Mod, int]]:
        cands = [m for m in self.installed.values()
                 if m.source in ("workshop", "steamcmd", "local") and m.workshop_id.isdigit()]
        if not cands:
            return []
        progress(None, f"Checking {len(cands)} mods on Steam…")
        details = steam.get_details(_dedupe(m.workshop_id for m in cands), db=self.db, max_age_s=300)  # raises offline
        out = []
        for m in cands:
            t = int((details.get(m.workshop_id) or {}).get("time_updated") or 0)
            # Mod.mtime only sees the top level; Steam delta updates / copy2 installs touch deeper files only.
            if t > m.mtime + 60 and t > _deep_mtime(m.path) + 60:
                out.append((m, t))
        progress(1.0, f"{len(out)} update(s) available")
        return sorted(out, key=lambda x: x[0].name.lower())

    def steamcmd(self) -> steam.SteamCMD:
        return steam.SteamCMD(self.settings.get("steamcmd_dir") or os.path.join(self.data_dir, "steamcmd"),
                              self.settings.get("steamcmd_user") or "")

    def steamcmd_login(self, username: str, progress: Progress = noop_progress, cancel=None) -> None:
        sc = self.steamcmd()
        sc.username = steam.SteamCMD(sc.root, username).username
        if not sc.is_installed():
            progress(None, "Installing SteamCMD…")
            sc.install(progress, cancel)
        sc.login_console(progress, cancel)

    # ------------------------------------------------------------ game
    def launch_game(self) -> None:
        raw = self.settings.get("launch_args") or ""
        try:
            if raw.count('"') % 2:                      # shlex raises on some, silently mis-splits others
                raise ValueError("unbalanced quote")
            args = [a.strip('"') for a in shlex.split(raw, posix=False)]
        except ValueError:
            raise RuntimeError("Launch arguments have an unbalanced quote; fix them in Settings.") from None
        # Only a Steam copy (<library>/steamapps/common/RimWorld) starts through Steam; GOG/DRM-free use the exe.
        steam_copy = not self.game_dir or os.path.isfile(
            os.path.join(self.game_dir, os.pardir, os.pardir, f"appmanifest_{RIMWORLD_APPID}.acf"))
        if self.settings.get("launch_via_steam", True) and steam_copy:
            steam_exe = os.path.join(self.steam_path or paths.find_steam() or "", "steam.exe")
            try:
                if args and os.path.isfile(steam_exe):
                    subprocess.Popen([steam_exe, "-applaunch", RIMWORLD_APPID, *args])
                else:
                    os.startfile(f"steam://rungameid/{RIMWORLD_APPID}")
                return
            except OSError as e:
                self.log.warning("Launching through Steam failed (%s); starting the exe directly", e)
        exe = paths.game_exe(self.game_dir) if self.game_dir else None
        if not exe:
            raise RuntimeError("RimWorld's executable was not found. Set the game folder in Settings.")
        subprocess.Popen([exe, *args], cwd=os.path.dirname(exe))

    def is_game_running(self) -> bool:
        return config.is_process_running(GAME_EXE) or config.is_process_running("RimWorld.exe")

    def backups(self) -> list[str]:
        return mods.list_backups(self._dir("backups"))

    def restore_backup(self, path: str) -> None:
        if not self.config_dir:
            raise RuntimeError("RimWorld's config folder was not found. Set it in Settings.")
        mods.restore_backup(path, self.config_dir, self._dir("backups"))
        self.load_active_from_game()
        if self.version_short and paths.short_version(self._config_version) != self.version_short:
            self.save_active_to_game(force=True)      # an old-version file would be reset by RimWorld

    def read_player_log(self, max_entries: int = 2000) -> list[LogEntry]:
        if not self.config_dir:
            raise RuntimeError("RimWorld's config folder was not found. Set it in Settings.")
        ud = paths.user_data_dir(self.config_dir)
        candidates = [os.path.join(ud, "Logs", "Player.log"), os.path.join(ud, "Player.log")]
        existing = [c for c in candidates if os.path.isfile(c)]
        if not existing:
            raise FileNotFoundError(f"No Player.log yet ({candidates[0]}). Start RimWorld once, then reload.")
        p = max(existing, key=os.path.getmtime)
        with open(p, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - LOG_TAIL_BYTES))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if size > LOG_TAIL_BYTES and lines:
            lines = lines[1:]                          # first line is cut in the middle
        entries = _parse_log_blocks(lines, max_entries)
        maps = self._log_matchers()
        for e in entries:
            e.mods = self._attribute(e.text, e.detail, maps)
        return entries

    def _log_matchers(self):
        pid_map: dict[str, list[str]] = {}
        stem_map: dict[str, list[str]] = {}
        name_map: dict[str, list[str]] = {}
        folder_map: dict[str, list[str]] = {}
        for uid, m in self.installed.items():
            if m.is_official:
                continue
            pid_map.setdefault(m.package_id, []).append(uid)
            for a in {a.lower() for a in m.assemblies}:
                if len(a) >= 4 and a not in _COMMON_ASSEMBLIES:
                    stem_map.setdefault(a, []).append(uid)
            if len(m.name.strip()) >= 5:
                name_map.setdefault(m.name.strip().lower(), []).append(uid)
            folder_map.setdefault(os.path.basename(os.path.normpath(m.path)).lower(), []).append(uid)
        stem_map = {k: v for k, v in stem_map.items() if len(v) <= 2}     # bundled libraries point nowhere
        return pid_map, stem_map, name_map, folder_map

    @staticmethod
    def _attribute(first_line: str, detail: str, maps) -> list[str]:
        pid_map, stem_map, name_map, folder_map = maps
        found: dict[str, None] = {}
        for tok in _LOG_TOKEN.findall(detail):
            parts = tok.lower().strip(".-").split(".")
            for i in range(1, len(parts) + 1):
                found.update(dict.fromkeys(pid_map.get(".".join(parts[:i]), ())))
            for part in parts:
                found.update(dict.fromkeys(stem_map.get(part, ())))
        for folder in _LOG_MODPATH.findall(detail):
            found.update(dict.fromkeys(folder_map.get(folder.strip().lower(), ())))
        head = first_line.lower()
        for name, uids in name_map.items():
            if name in head:
                found.update(dict.fromkeys(uids))
        return list(found)[:_LOG_MAX_MODS]

    def close(self) -> None:
        """App exit: stop every SteamCMD / Steamworks helper this process started, release the DB."""
        try:
            steam.kill_children()
        except Exception as e:  # noqa: BLE001
            self.log.warning("Could not stop helper processes: %s", e)
        try:
            if hasattr(self.db, "close"):
                self.db.close()
        except Exception as e:  # noqa: BLE001
            self.log.warning("Could not close the mod database: %s", e)

    # ------------------------------------------------------------ database / AI
    def update_db(self, progress: Progress = noop_progress, cancel=None, force: bool = False) -> str:
        return self.db.update(progress, cancel, force, steam_key=self.settings.get_secret("steam_api_key"))

    def search_db(self, query: str, **kw) -> list[DbMod]:
        kw.setdefault("version", self.version_short)
        return self.db.search(query, **kw)

    def generate_ai(self, prompt: str, target_count: int = 40, progress: Progress = noop_progress, cancel=None,
                    **kw):
        """kw overrides ai.GenRequest fields (require_package_id, include_popular_qol, live_verify, …)."""
        from . import ai
        s = self.settings
        if self._db("is_empty", default=True):
            progress(None, "The mod database is empty — downloading it first…")
            self.update_db(progress, cancel)
        provider = s.get("ai_provider") or "offline"
        llm = None if provider == "offline" else ai.LLM(provider, s.get("ai_model") or "", s.get_secret("ai_api_key"),
                                                        s.get("ai_base_url") or "", s.get("ai_effort") or "medium")
        installed = {m.workshop_id for m in self.installed.values() if m.workshop_id and m.source in LOADABLE_SOURCES}
        req = ai.GenRequest(**{"prompt": prompt, "version_short": self.version_short, "target_count": target_count,
                               "owned_dlcs": self.owned_dlcs(), "exclude_workshop_ids": set(),
                               "live_verify": bool(s.get("ai_live_verify", True)),
                               "auto_count": bool(target_count <= 0),
                               "no_lazy": bool(s.get("ai_no_lazy", True)),
                               "installed_ids": installed,
                               "steam_key": s.get_secret("steam_api_key") or os.environ.get("STEAM_API_KEY", ""), **kw})
        return ai.generate(req, self.db, llm, progress, cancel)

    def chat_idea(self, history: list[tuple[str, str]], current_prompt: str = "", version_short: str = "",
                  persona: str = "") -> dict:
        from . import ai
        s = self.settings
        provider = s.get("ai_provider") or "offline"
        llm = None if provider == "offline" else ai.LLM(provider, s.get("ai_model") or "", s.get_secret("ai_api_key"),
                                                        s.get("ai_base_url") or "", s.get("ai_effort") or "medium")
        chosen_persona = persona or s.get("ai_chat_persona") or "chaotic"
        return ai.chat_idea_architect(history, current_prompt, self.owned_dlcs(),
                                      version_short or self.version_short, llm, persona=chosen_persona)


# ---------------------------------------------------------------- self tests
def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _fake_mod(folder: str, pid: str, name: str, extra: str = "", wid: str = "", dll: str = "") -> None:
    _write(os.path.join(folder, "About", "About.xml"),
           f'<?xml version="1.0" encoding="utf-8"?>\n<ModMetaData><packageId>{pid}</packageId><name>{name}</name>'
           f"<author>Tester</author><supportedVersions><li>1.6</li></supportedVersions>{extra}</ModMetaData>")
    if wid:
        _write(os.path.join(folder, "About", "PublishedFileId.txt"), wid)
    if dll:
        _write(os.path.join(folder, "Assemblies", dll + ".dll"), "")
        _write(os.path.join(folder, "1.6", "Assemblies", dll + ".dll"), "")


def _selftest() -> None:
    tmp = tempfile.mkdtemp(prefix="rmm_core_test_")
    try:
        _run_selftest(tmp)
        _selftest_uninstall(os.path.join(tmp, "uninstall"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_uninstall(tmp: str) -> None:
    game, ws, extra = os.path.join(tmp, "RimWorld"), os.path.join(tmp, "workshop"), os.path.join(tmp, "extra")
    _fake_mod(os.path.join(game, "Data", "Core"), "Ludeon.RimWorld", "Core")
    _fake_mod(os.path.join(game, "Mods", "Local"), "t.local", "Local")
    _fake_mod(os.path.join(ws, "3333333333"), "t.ws", "Workshop", wid="3333333333")
    _fake_mod(os.path.join(extra, "Ext"), "t.ext", "Ext")
    _fake_mod(os.path.join(extra, "Target"), "t.link", "Linked")
    steam._make_junction(os.path.join(extra, "Target"), os.path.join(game, "Mods", "Link"))
    cache = os.path.join(tmp, "steamcmd", "steamapps", "workshop", "content", "294100", "4444444444")
    _fake_mod(cache, "t.sc", "Cached", wid="4444444444")                      # 'link' install: Mods/<id> -> cache
    steam._make_junction(cache, os.path.join(game, "Mods", "4444444444"))
    s = config.Settings(os.path.join(tmp, "settings.json"))
    for k, v in (("game_dir", game), ("config_dir", os.path.join(tmp, "cfg")), ("workshop_dir", ws),
                 ("steamcmd_dir", os.path.join(tmp, "steamcmd")), ("extra_mod_dirs", [extra])):
        s.set(k, v)
    os.makedirs(os.path.join(tmp, "cfg"), exist_ok=True)     # a missing cfg dir would make detect_paths look for the real one
    mgr = Manager(s, ModDatabase(os.path.join(tmp, "db.sqlite")), data_dir=os.path.join(tmp, "app"))
    mgr.is_game_running = lambda: False       # the player's real RimWorld may be open
    assert os.path.normcase(mgr.detect_paths()["config_dir"]) == os.path.normcase(os.path.join(tmp, "cfg"))
    mgr.scan()
    mgr._loaded = False
    try:
        mgr.unused_mods()
        raise AssertionError("unused_mods must refuse before the active list is loaded")
    except RuntimeError:
        pass
    mgr._loaded = True
    mgr.set_active([CORE_ID, "t.local", "t.ws"])
    mgr.save_modlist(ModList("Keep", [ModRef("t.link")]))
    _write(os.path.join(ws, "3333333333", "big.bin"), "x" * 1000)
    unused = {m.uid: n for m, n in mgr.unused_mods()}     # not Core, active, listed, or outside the delete roots
    assert set(unused) == {"t.sc"} and unused["t.sc"] == 0, unused   # link install: its target is not counted
    mgr.set_active([CORE_ID, "t.local"])
    assert {m.uid: n for m, n in mgr.unused_mods()}["t.ws"] > 1000
    mgr.set_active([CORE_ID, "t.local", "t.ws"])
    calls = []
    real_call, real_avail = steam.steamworks_call, steam.steamworks_available
    steam.steamworks_available = lambda _gd: True
    steam.steamworks_call = lambda action, ids, *a, **k: calls.append((action, ids)) or         {"ok": True, "items": {i: {"ok": True} for i in ids}}
    try:
        probs = mgr.uninstall([CORE_ID, "t.local", "t.ws", "t.ext", "t.link", "t.sc"])
    finally:
        steam.steamworks_call, steam.steamworks_available = real_call, real_avail
    assert calls == [("unsubscribe", ["3333333333"])], calls
    assert len(probs) == 1 and "Ext" in probs[0], probs                        # extra folder is never deleted
    assert os.path.isdir(os.path.join(extra, "Ext", "About"))
    assert os.path.isfile(os.path.join(extra, "Target", "About", "About.xml"))  # junction removed, target kept
    assert not os.path.lexists(os.path.join(game, "Mods", "Link"))
    assert not os.path.lexists(cache), "SteamCMD cache copy would reappear after the rescan"
    assert not os.path.exists(os.path.join(game, "Mods", "Local")) and not os.path.exists(os.path.join(ws, "3333333333"))
    assert os.path.isdir(os.path.join(game, "Data", "Core")) and mgr.active == [CORE_ID], mgr.active
    assert set(mgr.installed) == {CORE_ID, "t.ext", "t.link"}, sorted(mgr.installed)


def _run_selftest(tmp: str) -> None:
    game = os.path.join(tmp, "RimWorld")
    cfg = os.path.join(tmp, "user", "Config")
    ws = os.path.join(tmp, "workshop")
    os.makedirs(ws)
    _write(os.path.join(game, "Version.txt"), "1.6.4871 rev590\n")
    _write(os.path.join(game, GAME_EXE), "")
    _fake_mod(os.path.join(game, "Data", "Core"), "Ludeon.RimWorld", "Core")
    _fake_mod(os.path.join(game, "Data", "Royalty"), "Ludeon.RimWorld.Royalty", "Royalty")
    mdir = os.path.join(game, "Mods")
    _fake_mod(os.path.join(mdir, "Harmony"), "brrainz.harmony", "Harmony", wid="2009463077", dll="0Harmony")
    _fake_mod(os.path.join(mdir, "ModA"), "test.moda", "Mod Alpha", wid="1111111111", dll="ModAlpha",
              extra="<modDependencies><li><packageId>brrainz.harmony</packageId><displayName>Harmony</displayName></li>"
                    "<li><packageId>test.modb</packageId><displayName>Mod Beta</displayName></li></modDependencies>")
    _fake_mod(os.path.join(mdir, "ModB"), "test.modb", "Mod Beta", extra="<loadBefore><li>test.moda</li></loadBefore>")
    _fake_mod(os.path.join(mdir, "ModC"), "test.modc", "Mod Gamma")
    sc_content = os.path.join(tmp, "steamcmd", "steamapps", "workshop", "content", RIMWORLD_APPID)
    _fake_mod(os.path.join(sc_content, "1111111111"), "test.moda", "Mod Alpha", wid="1111111111")  # cache copy
    _fake_mod(os.path.join(sc_content, "2222222222"), "test.modd", "Mod Delta", wid="2222222222")  # not installed
    extra_dir = os.path.join(tmp, "extra")
    _fake_mod(os.path.join(extra_dir, "ModE"), "test.mode", "Mod Epsilon")      # RimWorld never loads extra dirs
    original_cfg = ('\ufeff<?xml version="1.0" encoding="utf-8"?>\n<ModsConfigData>\n  <version>1.6.4871 rev591</version>\n'
                    "  <activeMods>\n    <li>ludeon.rimworld</li>\n    <li>test.moda</li>\n    <li>unknown.mod</li>\n"
                    "  </activeMods>\n  <knownExpansions>\n    <li>ludeon.rimworld.royalty</li>\n  </knownExpansions>\n"
                    "</ModsConfigData>\n")
    _write(os.path.join(cfg, "ModsConfig.xml"), original_cfg)

    s = config.Settings(os.path.join(tmp, "settings.json"))
    for k, v in (("game_dir", game), ("config_dir", cfg), ("workshop_dir", ws),
                 ("steamcmd_dir", os.path.join(tmp, "steamcmd")), ("extra_mod_dirs", [extra_dir])):
        s.set(k, v)
    mgr = Manager(s, ModDatabase(os.path.join(tmp, "db.sqlite")), data_dir=os.path.join(tmp, "app"))
    norm = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731

    info = mgr.detect_paths()
    assert norm(info["game_dir"]) == norm(game) and norm(info["config_dir"]) == norm(cfg), info
    assert mgr.version_short == "1.6" and mgr.game_version == "1.6.4871 rev590", mgr.game_version

    mgr.scan()
    for uid in (CORE_ID, "ludeon.rimworld.royalty", HARMONY_ID, "test.moda", "test.modb", "test.modc", "test.modd"):
        assert uid in mgr.installed, (uid, sorted(mgr.installed))
    assert mgr.installed["test.moda"].source == "local" and mgr.installed["test.modd"].source == "steamcmd"
    assert not any(u.startswith("test.moda_") for u in mgr.installed), sorted(mgr.installed)  # cache copy hidden
    assert mgr.owned_dlcs() == {"ludeon.rimworld.royalty"}
    assert "modalpha" in {a.lower() for a in mgr.installed["test.moda"].assemblies}, mgr.installed["test.moda"].assemblies

    # matching
    assert mgr.match_ref(ModRef("brrainz.harmony_steam")) == HARMONY_ID
    assert mgr.match_ref(ModRef(workshop_id="1111111111")) == "test.moda"
    assert mgr.match_ref(ModRef(name="mod beta")) == "test.modb"
    assert mgr.match_ref(ModRef("other.id", "Mod Beta")) is None          # a different id is a different mod
    assert mgr.match_ref(ModRef(name="nope")) is None
    assert compare_lists(["a.b_steam", None, "c.d", "e.f"], ["e.f", "a.b", "c.d", "x.y"]) == ([1], ["x.y"], 1)
    st = dict((r.package_id, v) for r, v in mgr.refs_status([ModRef("test.modc"), ModRef("x.y", "", "123456"),
                                                             ModRef("z.z")]))
    assert st == {"test.modc": "installed", "x.y": "missing", "z.z": "unknown"}, st

    # saving before the game's list was loaded would write the empty startup list
    try:
        mgr.save_active_to_game(force=True)
        raise AssertionError("RuntimeError expected")
    except RuntimeError as e:
        assert "not been loaded" in str(e), e

    # active list editing + undo/redo
    assert mgr.load_active_from_game() == [CORE_ID, "test.moda", "unknown.mod"] and not mgr.dirty
    assert mgr.known_expansions == ["ludeon.rimworld.royalty"]
    good = mgr.active
    _write(os.path.join(cfg, "ModsConfig.xml"), "<ModsConfigData><version>1.6</version></ModsConfigData>")  # mid-write
    try:
        mgr.load_active_from_game()
        raise AssertionError("RuntimeError expected")
    except RuntimeError as e:
        assert "activeMods" in str(e) and mgr.active is good, e
    _write(os.path.join(cfg, "ModsConfig.xml"), original_cfg)
    assert mgr.load_active_from_game() == good and not mgr.dirty
    mgr._by_pid["zz.stale"] = ["zz.stale"]                  # index from a concurrent rescan: never a KeyError
    assert mgr.match_ref(ModRef("zz.stale", "", "")) is None and mgr._resolve(ModRef("zz.stale", "", ""))[1] is None
    del mgr._by_pid["zz.stale"]
    mgr.settings.set("launch_args", '-logfile "D:/Rim Logs')    # unbalanced quote: a clear error, nothing started
    try:
        mgr.launch_game()
        raise AssertionError("RuntimeError expected")
    except RuntimeError as e:
        assert "unbalanced quote" in str(e), e
    mgr.settings.set("launch_args", "")
    mgr.activate([HARMONY_ID, "test.moda"])
    assert mgr.active == [CORE_ID, "test.moda", "unknown.mod", HARMONY_ID] and mgr.dirty
    mgr.move([HARMONY_ID], 0)
    assert mgr.active[0] == HARMONY_ID
    mgr.move([HARMONY_ID, "unknown.mod"], 1)
    assert mgr.active == [CORE_ID, HARMONY_ID, "unknown.mod", "test.moda"], mgr.active
    mgr.move([CORE_ID], 99)
    assert mgr.active[-1] == CORE_ID
    assert mgr.undo() and mgr.undo() and mgr.active == [HARMONY_ID, CORE_ID, "test.moda", "unknown.mod"], mgr.active
    assert mgr.redo() and mgr.active == [CORE_ID, HARMONY_ID, "unknown.mod", "test.moda"]
    mgr.deactivate(["unknown.mod"])
    assert "unknown.mod" not in mgr.active and not mgr.redo()
    mgr.activate(["test.modc"], index=0)
    assert mgr.active[0] == "test.modc"
    mgr.set_active([CORE_ID, CORE_ID, HARMONY_ID, "test.moda"])
    assert mgr.active == [CORE_ID, HARMONY_ID, "test.moda"]

    # check / fixes / sort
    issues = mgr.check()
    dep = [i for i in issues if i.kind == "missing_dependency"]
    assert dep and dep[0].uid == "test.moda" and dep[0].fix and dep[0].fix.get("action") == "add", issues
    assert mgr.apply_fix(dep[0]) == []
    assert mgr.active.index("test.modb") < mgr.active.index("test.moda"), mgr.active
    mgr.set_active(["test.moda", "test.modb", CORE_ID, HARMONY_ID])
    real_sort, calls = sorting.sort_order, []

    def racing_sort(*a, **k):                  # the user activates a mod while Auto-sort is running
        if not calls:
            mgr.activate(["test.modc"])
        calls.append(1)
        return real_sort(*a, **k)
    sorting.sort_order = racing_sort
    try:
        warnings = mgr.auto_sort()
    finally:
        sorting.sort_order = real_sort
    assert len(calls) == 2 and mgr.active == [HARMONY_ID, CORE_ID, "test.modb", "test.moda", "test.modc"], mgr.active
    mgr.deactivate(["test.modc"])
    assert mgr.active == [HARMONY_ID, CORE_ID, "test.modb", "test.moda"], (mgr.active, warnings)
    assert not [i for i in mgr.check() if i.level == "error"], mgr.check()
    mgr.activate(["test.modd"])
    nig = [i for i in mgr.check() if i.kind == "not_in_game"]
    assert nig and nig[0].uid == "test.modd"
    need = mgr.apply_fix(nig[0])
    assert [r.workshop_id for r in need] == ["2222222222"], need
    mgr.deactivate(["test.modd"])
    # a mod in an extra folder is not loadable: flagged, and install_local links it into Mods/
    keep = list(mgr.active)
    assert [r.package_id for r in mgr.apply_modlist(ModList("e", [ModRef("test.mode")]))] == ["test.mode"]
    nig = [i for i in mgr.check() if i.kind == "not_in_game"]
    assert [(i.uid, i.fix) for i in nig] == [("test.mode", {"action": "install_local", "uids": ["test.mode"]})], nig
    assert mgr.apply_fix(nig[0]) == []
    m = mgr.installed["test.mode"]
    assert m.source == "local" and norm(m.path) == norm(os.path.join(mdir, "ModE")), m
    if os.path.isjunction(os.path.join(mdir, "ModE")):
        assert not [u for u in mgr.installed if u.startswith("test.mode_")], sorted(mgr.installed)
    # a list without Core/DLC entries keeps the Core that was active
    assert mgr.active == [CORE_ID, "test.mode"] and not [i for i in mgr.check() if i.kind == "not_in_game"]
    mgr.set_active(keep)
    mgr.set_active([CORE_ID, "test.modb", "test.moda"])
    assert mgr.resolve_dependencies()[0] == [HARMONY_ID] and mgr.active[0] == HARMONY_ID, mgr.active
    mgr.set_active([HARMONY_ID, CORE_ID, "test.moda"])
    added, to_dl, unres = mgr.resolve_dependencies()
    assert added == ["test.modb"] and mgr.active.index("test.modb") < mgr.active.index("test.moda"), mgr.active

    # save to the (temp) game config, with backup; GameRunningError when the game runs
    mgr.is_game_running = lambda: True
    try:
        mgr.save_active_to_game()
        raise AssertionError("GameRunningError expected")
    except GameRunningError:
        pass
    _write(os.path.join(cfg, "ModsConfig.xml"), original_cfg.replace("rev591", "rev592"))   # RimWorld rewrote it
    try:
        mgr.save_active_to_game(running_ok=True)            # 'Save anyway' still asks about the game's own edits
        raise AssertionError("ExternalChangeError expected")
    except ExternalChangeError:
        pass
    del mgr.is_game_running
    backup = mgr.save_active_to_game(force=True)
    assert backup and os.path.isfile(backup) and not mgr.dirty
    ids, known, ver = mods.read_mods_config(cfg)
    # the installed game's version, not the file's: RimWorld resets a list from another major.minor
    assert ids == mgr.active and "ludeon.rimworld.royalty" in known and ver == "1.6.4871 rev590", (ids, known, ver)
    assert mgr.backups() and norm(mgr.backups()[0]) == norm(backup)
    real_write = mods.write_mods_config             # an edit made while the file is written stays unsaved

    def edit_during_write(*a, **k):
        mgr.activate(["test.modc"])
        return real_write(*a, **k)
    mods.write_mods_config = edit_during_write
    try:
        mgr.save_active_to_game(force=True)
    finally:
        mods.write_mods_config = real_write
    assert mgr.dirty and "test.modc" not in mods.read_mods_config(cfg)[0]
    mgr.deactivate(["test.modc"])
    mgr.is_game_running = lambda: False
    mgr.save_active_to_game()                                  # our own last write is not an external change
    assert not mgr.dirty
    del mgr.is_game_running

    # modlists (profiles)
    saved_active = list(mgr.active)
    ml = mgr.modlist_from_active("My/List")
    assert [r.package_id for r in ml.mods] == saved_active and ml.mods[0].workshop_id == "2009463077"
    mgr.save_modlist(ml)
    assert [m.name for m in mgr.list_modlists()] == ["My/List"]
    mgr.rename_modlist("My/List", "Renamed")
    assert [m.name for m in mgr.list_modlists()] == ["Renamed"]
    mgr.save_modlist(ModList("Other"))
    try:
        mgr.rename_modlist("Renamed", "Other")
        raise AssertionError("FileExistsError expected")
    except FileExistsError:
        pass
    mgr.delete_modlist("Renamed")
    mgr.delete_modlist("Other")
    assert mgr.list_modlists() == []
    # names that share a file ('Raid: hard' / 'Raid? hard' -> 'Raid_ hard.json'; case on NTFS) never clobber
    mgr.save_modlist(ModList("Raid: hard", [ModRef("test.moda"), ModRef("test.modb")]))
    other = ModList("Raid? hard", [ModRef("test.modc")])
    mgr.save_modlist(other)
    assert other.name == "Raid? hard (2)", other.name
    assert [(m.name, len(m.mods)) for m in mgr.list_modlists()] == [("Raid: hard", 2), ("Raid? hard (2)", 1)]
    assert mgr.unique_modlist_name("Raid: hard") == "Raid: hard (3)"    # '(2)' maps to the taken 'Raid_ hard (2)'
    assert mgr.unique_modlist_name("x" * 130) == "x" * 130 and mgr.unique_modlist_name("Fresh") == "Fresh"
    mgr.save_modlist(ModList("x" * 130))
    assert mgr.unique_modlist_name("x" * 130 + "y") == "x" * 110 + " (2)"
    mgr.delete_modlist("x" * 130)
    same = ModList("Raid: hard", [ModRef("test.modc")])
    mgr.save_modlist(same)                                    # the same list: overwritten in place
    assert same.name == "Raid: hard" and len(mgr.list_modlists()) == 2
    mgr.rename_modlist("Raid: hard", "RAID: hard")            # case-only rename (same file on Windows)
    assert [m.name for m in mgr.list_modlists()] == ["RAID: hard", "Raid? hard (2)"], mgr.list_modlists()
    mgr.delete_modlist("RAID: hard")
    mgr.delete_modlist("Raid? hard (2)")
    assert mgr.list_modlists() == []
    mgr.save_modlist(ModList("Orig"))                         # a file copied by hand keeps the name inside it
    shutil.copy(mgr._modlist_file("Orig"), os.path.join(mgr._dir("modlists"), "Copy.json"))
    assert [m.name for m in mgr.list_modlists()] == ["Copy", "Orig"], mgr.list_modlists()
    mgr.rename_modlist("Copy", "Copy2")
    mgr.delete_modlist("Copy2")
    for stem in (" Orig", "x" * 130):                         # stems that _safe_name maps to another file
        shutil.copy(mgr._modlist_file("Orig"), os.path.join(mgr._dir("modlists"), stem + ".json"))
        mgr.rename_modlist(stem, "Moved")
        mgr.delete_modlist("Moved")
    assert [m.name for m in mgr.list_modlists()] == ["Orig"], mgr.list_modlists()
    mgr.delete_modlist("Orig")
    assert mgr.list_modlists() == []
    try:
        mgr.delete_modlist("Orig")
        raise AssertionError("FileNotFoundError expected")
    except FileNotFoundError:
        pass

    missing = mgr.apply_modlist(ModList("x", [ModRef("test.modb"), ModRef(workshop_id="2009463077"),
                                              ModRef("x.missing", "Missing", "999999999"), ModRef(name="Mod Gamma")]))
    assert [r.package_id for r in missing] == ["x.missing"], missing
    assert mgr.active == [CORE_ID, "test.modb", HARMONY_ID, "x.missing", "test.modc"], mgr.active
    assert [r.workshop_id for r in mgr.missing_refs()] == ["999999999"]
    # '_steam' id of an installed local mod = a replace, not a download; a SteamCMD-cache-only copy needs one
    mgr.activate(["test.modb_steam", "test.modd"])
    assert [r.workshop_id for r in mgr.missing_refs()] == ["999999999", "2222222222"], mgr.missing_refs()
    assert [s for _, s in mgr.refs_status([ModRef("test.modd")])] == ["missing"]
    assert [r.workshop_id for r in mgr.missing_refs([ModRef("test.modb"), ModRef("q.q", "", "42424242"),
                                                     ModRef("test.modd")])] == ["42424242", "2222222222"]
    res = mgr.download([ModRef("no.wid.mod"), ModRef(CORE_ID)], method="steam_client")   # nothing opened: no ids
    assert len(res) == 2 and not any(r.ok for r in res), res
    mgr.set_active(saved_active)

    # import: share code, text ids/urls, files in every export format
    code = mgr.share_code(ml)
    assert code.startswith("RMM1:")
    ml2 = mgr.import_text(f"here you go: {code} enjoy")
    assert [r.package_id for r in ml2.mods] == saved_active, ml2.mods
    wrapped = "\n".join(code[i:i + 20] for i in range(0, len(code), 20)).replace("RMM1:", "rmm1:")   # mail/chat wrap
    assert [r.package_id for r in mgr.import_text(wrapped).mods] == saved_active
    ml3 = mgr.import_text("https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077\ntest.modb\n1111111111")
    assert [r.package_id for r in ml3.mods] == [HARMONY_ID, "test.modb", "test.moda"], ml3.mods
    assert ml3.mods[2].name == "Mod Alpha" and ml3.mods[1].workshop_id == ""
    ml3 = mgr.import_text("test.modb | 1111111111")          # wrong id pairing: the installed copy wins
    assert ml3.mods[0].package_id == "test.modb" and ml3.mods[0].name == "Mod Beta", ml3.mods
    try:
        mgr.import_text("nothing useful here")
        raise AssertionError("ValueError expected")
    except ValueError:
        pass
    out = os.path.join(tmp, "exports")
    os.makedirs(out)
    for fmt, ext in (("rml", ".rml"), ("modsconfig", ".xml"), ("txt", ".txt"), ("json", ".json"), ("html", ".html")):
        p = os.path.join(out, "list" + ext)
        mgr.export_modlist(ml, p, fmt)
        assert os.path.getsize(p) > 0, fmt
        if fmt != "html":
            back = mgr.import_path(p)
            assert [mgr.match_ref(r) for r in back.mods] == saved_active, (fmt, back.mods)
    gp = mgr.export_to_game_modlists(ml)
    assert norm(os.path.dirname(gp)) == norm(os.path.join(tmp, "user", "ModLists"))
    assert [norm(p) for p in mgr.game_modlists()] == [norm(gp)]
    try:                                                      # same name ignoring case: ask first, then back up
        mgr.export_to_game_modlists(ModList("MY/LIST", ml.mods[:1]))
        raise AssertionError("FileExistsError expected")
    except FileExistsError:
        pass
    assert norm(mgr.export_to_game_modlists(ModList("MY/LIST", ml.mods[:1]), overwrite=True)) == norm(gp)
    assert len(mgr.import_path(gp).mods) == 1 and os.listdir(os.path.join(mgr.data_dir, "backups", "ModLists"))

    # a collection's children go where its link was; a member also pasted after it is not lost
    real_tree, real_details = steam._collection_tree, steam.get_details
    steam._collection_tree = lambda ids: {"9000000001": ["9000000002", "9000000003"]}
    steam.get_details = lambda ids, db=None, **k: {}
    try:
        got = mgr.import_text("test.modb\nhttps://steamcommunity.com/sharedfiles/filedetails/?id=9000000001\n9000000002")
    finally:
        steam._collection_tree, steam.get_details = real_tree, real_details
    assert [r.workshop_id or r.package_id for r in got.mods] == ["test.modb", "9000000002", "9000000003"], got.mods
    assert got.source == "import:collection"

    # .rml lists say '0' for local mods: a unique exact DB title (case/punctuation aside) fills the workshop id,
    # preferring the upload for the game version; ambiguous or other-packageId titles are not guessed
    rows = [DbMod("5550000016", "", "Combat Bionics (Continued)", game_versions=["1.6"]),
            DbMod("5550000014", "", "Combat Bionics (Continued)", game_versions=["1.4"]),
            DbMod("5550000001", "", "", "Twin", game_versions=["1.6"]), DbMod("5550000002", "", "Twin", game_versions=["1.6"]),
            DbMod("5550000003", "someone.else", "Other Id")]
    mgr.db.search = lambda q, *a, **k: rows
    try:
        got = mgr._fill_refs([ModRef("mlie.combatbionics", "combat bionics continued", "0"), ModRef(name="Twin"),
                              ModRef("my.other", "Other Id")])
    finally:
        del mgr.db.search
    assert [(r.package_id, r.workshop_id) for r in got] == [("mlie.combatbionics", "5550000016"), ("", ""),
                                                             ("my.other", "")], got

    # Player.log grouping / attribution / collapsing
    _write(os.path.join(tmp, "user", "Player.log"), "\n".join([
        "RimWorld 1.6.4871 rev591",
        "Exception in Mod Alpha tick: System.NullReferenceException: Object reference not set",
        "  at ModAlpha.Foo.Bar () [0x0] in <x>:0",
        "  at Verse.Root.Update () [0x0] in <x>:0",
        "Loaded fine",
        "Exception in Mod Alpha tick: System.NullReferenceException: Object reference not set",
        "  at ModAlpha.Foo.Bar () [0x0] in <x>:0",
        "[Mod Beta - Start of stack trace]",
        'Verse.PatchOperationAdd(xpath="Defs/X"): Failed to find a node with the given xpath',
        "[End of stack trace]",
        r"Source file: C:\Games\RimWorld\Mods\ModB\Patches\x.xml",
        "Could not load Texture2D at 'Things/Foo' in any active mod or in base resources.",
        "XML error: <foo> doesn't correspond to any field in type test.modc.Bar",
        "Terrorists arrived at the colony",       # 'error' inside a word is not an error
        "Just info",
    ]) + "\n")
    log = mgr.read_player_log()
    assert [(e.level, e.count) for e in log] == [("error", 2), ("error", 1), ("warning", 1), ("error", 1)], log
    assert log[0].mods == ["test.moda"] and "ModAlpha.Foo.Bar" in log[0].detail, log[0]
    assert log[1].text.startswith("[Mod Beta] Verse.PatchOperationAdd") and log[1].mods == ["test.modb"], log[1]
    assert "Source file:" in log[1].detail
    assert log[3].mods == ["test.modc"], log[3]
    assert len(mgr.read_player_log(max_entries=2)) == 2

    # restore the backup = the original config; the change is undoable
    mgr.deactivate([HARMONY_ID])
    mgr.restore_backup(backup)
    assert mgr.active == [CORE_ID, "test.moda", "unknown.mod"] and not mgr.dirty, mgr.active
    assert mgr.undo() and HARMONY_ID not in mgr.active
    old_cfg = os.path.join(tmp, "old-ModsConfig.xml")         # a pre-update backup gets the installed version
    _write(old_cfg, original_cfg.replace("1.6.4871 rev591", "1.5.4409 rev1118"))
    mgr.restore_backup(old_cfg)
    assert mods.read_mods_config(cfg)[1:] == (["ludeon.rimworld.royalty"], "1.6.4871 rev590"), mods.read_mods_config(cfg)
    assert mgr.active == [CORE_ID, "test.moda", "unknown.mod"] and not mgr.dirty

    # update check looks deeper than the top level
    now = time.time()
    for folder in (os.path.join(mdir, "ModA"), os.path.join(mdir, "Harmony")):
        for root, dirs, files in os.walk(folder):
            for n in dirs + files:
                os.utime(os.path.join(root, n), (now - 7200, now - 7200))
        os.utime(folder, (now - 7200, now - 7200))
    os.utime(os.path.join(mdir, "ModA", "1.6", "Assemblies", "ModAlpha.dll"), (now - 60, now - 60))
    mgr.scan()
    real_details = steam.get_details
    steam.get_details = lambda ids, db=None, max_age_s=0: {i: {"time_updated": int(now) - 3600} for i in ids}
    try:
        assert [m.uid for m, _ in mgr.check_updates()] == [HARMONY_ID], mgr.check_updates()
    finally:
        steam.get_details = real_details

    # SteamCMD downloads (stubbed): an explicit workshop id is kept and deduped; update=True replaces Mods/<name>
    # only for the same packageId (old folder backed up); activate=True activates what came down and sorts
    content, asked = mgr.steamcmd().content_dir, []
    _fake_mod(os.path.join(mdir, "MyPatch"), "me.patch", "My Patch", wid="3333333333")   # stale PublishedFileId.txt
    got = {"3333333333": "other.real", "2009463077": HARMONY_ID, "4444444444": "test.new", "5555555555": "test.needy",
           "6666666655": "test.lib"}
    needs = ("<modDependencies><li><packageId>test.lib</packageId><displayName>Lib</displayName><steamWorkshopUrl>"
             "https://steamcommunity.com/sharedfiles/filedetails/?id=6666666655</steamWorkshopUrl></li></modDependencies>")

    def fake_download(_sc, ids, *a, **k):
        asked.extend(ids)
        for i in ids:
            _fake_mod(os.path.join(content, i), got[i], "New " + i, needs if i == "5555555555" else "", wid=i)
        return {i: DownloadResult(i, True, "downloaded", os.path.join(content, i)) for i in ids}
    real_sc, real_details = (steam.SteamCMD.is_installed, steam.SteamCMD.download), steam.get_details
    steam.SteamCMD.is_installed, steam.SteamCMD.download = (lambda _sc: True), fake_download
    steam.get_details = lambda ids, db=None, **k: {}                  # no sizes known: no disk space check
    try:
        mgr.scan()
        res = mgr.download([ModRef("me.patch", "", "3333333333"), ModRef(HARMONY_ID, "", "2009463077")], "steamcmd",
                           update=True, with_deps=False)
        assert [(r.ok, os.path.basename(r.path)) for r in res] == [(True, "3333333333"), (True, "Harmony")], res
        assert os.path.isdir(os.path.join(mdir, "MyPatch")) and "me.patch" in mgr.installed
        assert [n.split("-")[0] for n in os.listdir(os.path.join(mgr.data_dir, "backups", "Mods"))] == ["Harmony"]
        mgr.set_active([CORE_ID, "test.moda", HARMONY_ID])
        asked.clear()
        res = mgr.download([ModRef("test.moda", "Alpha dev build", "4444444444"), ModRef("x", "again", "4444444444")],
                           "steamcmd", activate=True)
        assert asked == ["4444444444"] and [r.workshop_id for r in res] == ["4444444444"], (asked, res)
        assert mgr.active[0] == HARMONY_ID and "test.new" in mgr.active, mgr.active
        asked.clear()             # an About.xml dependency Steam does not list comes down in a second round
        mgr.download([ModRef("", "Needy", "5555555555")], "steamcmd", activate=True)
        assert asked == ["5555555555", "6666666655"], asked
        assert mgr.active.index("test.lib") < mgr.active.index("test.needy"), mgr.active
    finally:
        steam.SteamCMD.is_installed, steam.SteamCMD.download = real_sc
    # 'Use This Instead' with the same packageId: 1st Fix downloads the new upload, 2nd Fix retires the old folder
    _fake_mod(os.path.join(mdir, "1000000001"), "test.uti", "Uti", wid="1000000001")
    mgr.scan()
    iss = Issue("info", "replacement", "test.uti", "", fix={"action": "replace", "uid": "test.uti",
                                                          "ref": ModRef("test.uti", "Uti (Continued)", "7777777777")})
    mgr.set_active([CORE_ID, "test.uti"])
    mgr.is_game_running = lambda: False
    assert [r.workshop_id for r in mgr.apply_fix(iss)] == ["7777777777"] and mgr.active == [CORE_ID, "test.uti"]
    _fake_mod(os.path.join(mdir, "7777777777"), "test.uti", "Uti (Continued)", wid="7777777777")   # downloaded
    mgr.scan()
    assert mgr.apply_fix(iss) == [] and mgr.active == [CORE_ID, "test.uti"], mgr.active
    assert mgr.installed["test.uti"].workshop_id == "7777777777" and not os.path.exists(os.path.join(mdir, "1000000001"))
    _fake_mod(os.path.join(mdir, "UtiCopy"), "test.uti", "Uti copy")      # a hand-made duplicate: its fix never moves it
    mgr.scan()
    mgr.set_active([CORE_ID, "test.uti_dup1"])
    mgr.apply_fix(Issue("error", "not_in_game", "test.uti_dup1", "", fix={"action": "replace", "uid": "test.uti_dup1",
                                                                          "ref": ModRef("test.uti", "Uti copy", "")}))
    assert mgr.active == [CORE_ID, "test.uti"] and os.path.isdir(os.path.join(mdir, "UtiCopy")), mgr.active
    del mgr.is_game_running

    # Steamworks downloads honour Cancel
    real_call, real_avail, seen = steam.steamworks_call, steam.steamworks_available, {}
    steam.steamworks_available = lambda _gd: True
    steam.steamworks_call = lambda *a, cancel=None, **k: seen.update(cancel=cancel) or \
        {"ok": False, "error": "cancelled", "items": {}}
    ev = threading.Event()
    ev.set()
    try:
        res = mgr.download([ModRef("x.y", "X", "123456789")], "steamworks", cancel=ev)
    finally:
        steam.steamworks_call, steam.steamworks_available = real_call, real_avail
    assert seen["cancel"] is ev and [(r.ok, r.message) for r in res] == [(False, "cancelled")], res

    # SteamCMD failures fall back to a Steam subscription (not cancels/bad ids); the subscribed mod is found + activated
    sw_asked: list = []
    mgr._download_steamcmd = lambda ids, *a: [DownloadResult(i, i == "8000000001", {"8000000001": "downloaded",
                                              "8000000002": "Access Denied", "8000000003": "cancelled"}.get(i, "Failure")) for i in ids]

    def fake_sw(ids, *a):
        sw_asked.extend(ids)
        _fake_mod(os.path.join(ws, "8000000002"), "test.swok", "Sw ok", wid="8000000002")
        return [DownloadResult(i, i == "8000000002", "installed" if i == "8000000002" else "Timeout") for i in ids]
    mgr._download_steamworks = fake_sw
    steam.steamworks_available = lambda _gd: True
    wids = ["8000000001", "8000000002", "8000000003", "8000000004"]
    try:
        res = mgr.download([ModRef("", "", w) for w in wids], "steamcmd", with_deps=False, activate=True)
        assert sw_asked == ["8000000002", "8000000004"], sw_asked
        assert [(r.ok, r.message) for r in res] == [(True, "downloaded"), (True, "installed"), (False, "cancelled"),
                                                    (False, "Failure; Steam: Timeout")], res
        assert "test.swok" in mgr.active, mgr.active
        sw_asked.clear()
        s.set("steamcmd_fallback_steamworks", False)
        mgr.download([ModRef("", "", "8000000004")], "steamcmd", with_deps=False)
        assert sw_asked == [], sw_asked
        s.set("steamcmd_fallback_steamworks", True)
        mgr.download([ModRef("", "", "8000000004")], "steamcmd", with_deps=False, update=True)
        assert sw_asked == [], sw_asked                        # an update never subscribes (would duplicate Mods/ copy)
    finally:
        steam.steamworks_available, steam.get_details = real_avail, real_details
        s.set("steamcmd_fallback_steamworks", True)
        del mgr._download_steamcmd, mgr._download_steamworks

    # disk space preflight: steamcmd copy mode needs the size twice on one drive, move mode once; offline skips it
    real_usage, ran = shutil.disk_usage, []
    steam.get_details = lambda ids, db=None, **k: {i: {"file_size": str(1024 ** 3)} for i in ids}   # 1 GB each
    shutil.disk_usage = lambda p: type("U", (), {"free": 2.5 * 1024 ** 3})()                       # 2.5 GB free
    mgr._download_steamcmd = lambda ids, *a: ran.append(ids) or []
    try:
        s.set("install_mode", "copy")
        if os.path.splitdrive(mgr.steamcmd().content_dir)[0] == os.path.splitdrive(mdir)[0]:
            try:
                mgr.download([ModRef("", "", "8100000001")], "steamcmd", with_deps=False)
                raise AssertionError("copy mode: 2.7 GB needed on one drive")
            except NotEnoughSpaceError as e:
                assert "need about 2.7 GB" in str(e) and "2.5 GB free" in str(e) and not ran, e
        s.set("install_mode", "move")
        mgr.download([ModRef("", "", "8100000001")], "steamcmd", with_deps=False)
        assert ran == [["8100000001"]], ran
        try:
            mgr.download([ModRef("", "", w) for w in ("8100000001", "8100000002", "8100000003")], "steamcmd",
                         with_deps=False)
            raise AssertionError("3.8 GB does not fit in 2.5 GB")
        except NotEnoughSpaceError:
            pass
        steam.get_details = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
        mgr.download([ModRef("", "", "8100000004")], "steamcmd", with_deps=False)
        assert ran[-1] == ["8100000004"], ran
    finally:
        shutil.disk_usage, steam.get_details = real_usage, real_details
        s.set("install_mode", "move")
        del mgr._download_steamcmd

    # a GOG / DRM-free copy starts its exe even with launch_via_steam; a Steam copy goes through Steam
    launched = []
    real_popen, real_startfile = subprocess.Popen, os.startfile
    subprocess.Popen = lambda cmd, **k: launched.append(("exe", norm(cmd[0])))
    os.startfile = lambda p, *a: launched.append(("steam", p))
    try:
        mgr.launch_game()
        lib_game = os.path.join(tmp, "lib", "steamapps", "common", "RimWorld")
        _write(os.path.join(tmp, "lib", "steamapps", f"appmanifest_{RIMWORLD_APPID}.acf"), "")
        os.makedirs(lib_game)
        mgr.game_dir, gd = lib_game, mgr.game_dir
        mgr.launch_game()
        mgr.game_dir = gd
    finally:
        subprocess.Popen, os.startfile = real_popen, real_startfile
    assert launched == [("exe", norm(os.path.join(game, GAME_EXE))), ("steam", f"steam://rungameid/{RIMWORLD_APPID}")], \
        launched

    # undo stack is bounded
    for i in range(MAX_UNDO + 20):
        mgr.set_active([CORE_ID, f"x.{i}"])
    assert len(mgr._undo) == MAX_UNDO

    # user rules persist and accept RimSort's format
    mgr.user_rules = sorting.rules_from_json({"rules": {"Test.ModC": {"loadAfter": {"Test.ModA": {"name": ["A"], "comment": ["c"]}},
                                                               "loadBottom": {"value": True}}}})
    assert mgr.user_rules["test.modc"]["load_after"] == {"test.moda": "c"} and mgr.user_rules["test.modc"]["load_bottom"]
    mgr.save_user_rules()
    assert Manager(s, mgr.db, data_dir=mgr.data_dir).user_rules == mgr.user_rules
    mgr.set_active([CORE_ID, "test.modc", "test.moda", HARMONY_ID, "test.modb"])
    mgr.auto_sort()
    assert mgr.active[-1] == "test.modc", mgr.active
    rules_file = os.path.join(mgr.data_dir, "user_rules.json")   # a broken hand edit is kept, not overwritten
    with open(rules_file, "r+", encoding="utf-8") as f:
        f.truncate(os.path.getsize(rules_file) - 3)
    assert Manager(s, mgr.db, data_dir=mgr.data_dir).user_rules == {} and not os.path.exists(rules_file)
    assert [n for n in os.listdir(mgr.data_dir) if n.startswith("user_rules.json.corrupt-")]
    assert _safe_name("CON") == "_CON" and _safe_name('a/b:c') == "a_b_c"
    assert [r.workshop_id for r in mgr.expand_download_deps([ModRef("x.y", "", "42424242")])][:1] == ["42424242"]
    mgr.close()                          # stops helpers and releases the sqlite file so the temp dir can be removed


def _selftest_online() -> None:
    """Read-only session against the real install (if any) + Steam Web API update check. Writes only to %TEMP%."""
    tmp = tempfile.mkdtemp(prefix="rmm_core_online_")
    try:
        mgr = Manager(config.Settings(os.path.join(tmp, "settings.json")), ModDatabase(os.path.join(tmp, "db.sqlite")),
                      data_dir=tmp)
        info = mgr.detect_paths()
        if not info["game_dir"]:
            return
        t = time.perf_counter()
        mgr.scan()
        assert CORE_ID in mgr.installed, "Core not found in the real install"
        scan_s = time.perf_counter() - t
        mgr.load_active_from_game()
        mgr.check()
        if mgr.config_dir and os.path.isfile(os.path.join(paths.user_data_dir(mgr.config_dir), "Player.log")):
            t = time.perf_counter()
            log = mgr.read_player_log()
            assert all(e.level in ("error", "warning") for e in log)
            assert time.perf_counter() - t < 20, "Player.log parsing too slow"
        if any(m.workshop_id for m in mgr.installed.values() if m.source != "core"):
            ups = mgr.check_updates()
            assert all(t > m.mtime for m, t in ups)
        ml = mgr.import_text("https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077")
        assert ml.mods and ml.mods[0].workshop_id == "2009463077"
        assert scan_s < 30, f"scan too slow: {scan_s:.1f}s"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
