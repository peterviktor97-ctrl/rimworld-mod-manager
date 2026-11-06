"""Steam integration: Web API (details, collections), workshop search, steam:// + browser openers,
SteamCMD (install, self-update, batched downloads with retries), installing downloads into Mods/,
and a Steamworks (ctypes) worker that runs in a subprocess (subscribe / download / state via the real Steam client)."""
from __future__ import annotations

import atexit
import contextlib
import html as _html
import json
import logging
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from collections import deque

from .models import RIMWORLD_APPID, WORKSHOP_URL, DownloadResult, Progress, noop_progress

STEAMCMD_URLS = (
    "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip",
    "https://cdn.cloudflare.steamstatic.com/client/installer/steamcmd.zip",
    "https://cdn.akamai.steamstatic.com/client/installer/steamcmd.zip",
    "https://media.steampowered.com/installer/steamcmd.zip",
)
_API = "https://api.steampowered.com"
_DETAILS_URL = _API + "/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_COLLECTION_URL = _API + "/ISteamRemoteStorage/GetCollectionDetails/v1/"
_QUERY_URL = _API + "/IPublishedFileService/QueryFiles/v1/"
_BROWSE_URL = "https://steamcommunity.com/workshop/browse/"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_WORKER_CODE = "import sys; from rmm.steam import steamworks_worker_main; sys.exit(steamworks_worker_main(sys.argv[1]))"
_SW_ACTIONS = ("subscribe", "unsubscribe", "list_subscribed", "state", "download")
# failure lines only: every good cached login prints "Logging in using cached credentials."
_RE_LOGIN_FAIL = re.compile(r"not logged on|no cached credentials|cached credentials not found|login failure|"
                            r"FAILED login|\.\.\.\s*FAILED", re.I)
_STEAMCMD_LOCK = threading.Lock()   # one steamcmd process at a time (they share the install dir)
_INSTALL_LOCK = threading.Lock()    # installs vs cleanup_stale_installs (a running install's temp folders are not stale)
_CHILDREN: set = set()              # live Popen objects started by _run_stream (steamcmd / Steamworks helper)

_log = logging.getLogger("rmm.steam")


def _clean_ids(ids) -> list[str]:
    """Digits-only ids, stripped, deduped, order kept."""
    return list(dict.fromkeys(s for s in (str(i).strip() for i in ids or []) if s.isdigit()))


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _id_form(key: str, ids: list[str]) -> dict:
    return {key: len(ids), **{f"publishedfileids[{n}]": i for n, i in enumerate(ids)}}


def _post_json(url: str, form: dict) -> dict:
    from . import config   # http helpers already retry transient failures once
    data = json.loads(config.http_post_form(url, form, timeout=30))
    return data if isinstance(data, dict) else {}


def _resp_list(data: dict, key: str) -> list:
    """data['response'][key] if it is a list, else []."""
    r = data.get("response")
    v = r.get(key) if isinstance(r, dict) else None
    return v if isinstance(v, list) else []


# ------------------------------------------------------------------ Web API: details
def get_details(ids: list[str], db=None, max_age_s: float = 3600) -> dict[str, dict]:
    """GetPublishedFileDetails in batches of 100 (+ db cache). Ids Steam doesn't know (result != 1) are omitted.
    A batch that fails on the network is omitted too; if *every* batch failed, the last error is raised
    (so callers can tell "offline" from "not found")."""
    ids = _clean_ids(ids)
    out: dict[str, dict] = {}
    if db is not None and ids:
        try:
            out.update(db.cached_details(ids, max_age_s) or {})
        except Exception as e:  # noqa: BLE001 - a broken cache must not break lookups
            _log.warning("details cache read failed: %s", e)
    todo = [i for i in ids if i not in out]
    fresh: dict[str, dict] = {}
    batches = _chunks(todo, 100)
    last_err: Exception | None = None
    failed = 0
    for chunk in batches:
        try:
            data = _post_json(_DETAILS_URL, _id_form("itemcount", chunk))
        except Exception as e:  # noqa: BLE001 - network / HTTP / bad JSON
            failed, last_err = failed + 1, e
            _log.warning("GetPublishedFileDetails failed for %d ids: %s", len(chunk), e)
            continue
        wanted = set(chunk)
        for d in _resp_list(data, "publishedfiledetails"):
            if isinstance(d, dict) and d.get("result") == 1 and str(d.get("publishedfileid")) in wanted:
                fresh[str(d["publishedfileid"])] = d
    if batches and failed == len(batches) and last_err is not None:
        raise last_err
    if fresh and db is not None:
        try:
            db.cache_details(fresh)
        except Exception as e:  # noqa: BLE001
            _log.warning("details cache write failed: %s", e)
    out.update(fresh)
    return out


# ------------------------------------------------------------------ Web API: collections
def _collection_children(ids: list[str]) -> dict[str, list[tuple[str, int]]]:
    """{collection id: [(child id, filetype), ...] by sortorder} for those ids that are collections (result 1).
    Raises on network failure."""
    kids: dict[str, list[tuple[str, int]]] = {}
    for chunk in _chunks(ids, 100):
        data = _post_json(_COLLECTION_URL, _id_form("collectioncount", chunk))
        for c in _resp_list(data, "collectiondetails"):
            if not isinstance(c, dict) or c.get("result") != 1:
                continue       # 9 = not a collection
            ch = [x for x in c.get("children") or [] if isinstance(x, dict) and str(x.get("publishedfileid", "")).isdigit()]
            ch.sort(key=lambda x: int(x.get("sortorder") or 0))
            kids[str(c.get("publishedfileid"))] = [(str(x["publishedfileid"]), int(x.get("filetype") or 0)) for x in ch]
    return kids


def _collection_tree(ids: list[str], fetch=_collection_children) -> dict[str, list[str]]:
    """Fetch ids, then every nested collection (filetype 2) until done. Returns {collection: [children]}.
    The first round raises on failure; failures while resolving nested collections are logged."""
    kids: dict[str, list[tuple[str, int]]] = {}
    asked: set[str] = set()
    todo = list(ids)
    while todo:
        first = not asked
        asked.update(todo)
        try:
            kids.update(fetch(todo))
        except Exception as e:  # noqa: BLE001
            if first:
                raise
            _log.warning("nested collections not resolved: %s", e)
            break
        todo = list(dict.fromkeys(c for ch in kids.values() for c, ft in ch if ft == 2 and c not in asked))
    return {k: [c for c, _ in v] for k, v in kids.items()}


def _flatten(ids: list[str], kids: dict[str, list[str]]) -> list[str]:
    """Replace collections by their (recursive) children, keep order, dedupe; cycle-safe, no recursion."""
    out, seen, expanded = [], set(), set()
    stack = [iter(ids)]
    while stack:
        i = next(stack[-1], None)
        if i is None:
            stack.pop()
        elif i in kids:
            if i not in expanded:
                expanded.add(i)
                stack.append(iter(kids[i]))
        elif i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ------------------------------------------------------------------ workshop search
def search_workshop(query: str, page: int = 1, api_key: str = "") -> list[dict]:
    """[{'workshop_id','title','author'?,'subscriptions'?,'description'?,'preview_url'?}]. QueryFiles when an API key
    is given (falls back to scraping if that fails), else scrapes the workshop browse page. Never raises."""
    try:
        query, page = str(query or "").strip(), max(1, int(page or 1))
    except (TypeError, ValueError):
        return []
    if api_key:
        try:
            return _query_files(query, page, api_key)
        except Exception as e:  # noqa: BLE001
            _log.warning("QueryFiles failed (%s); falling back to scraping", type(e).__name__)
    try:
        from . import config
        params = {"appid": RIMWORLD_APPID, "searchtext": query, "browsesort": "textsearch" if query else "trend",
                  "section": "readytouseitems", "p": page}
        if not query:
            params["days"] = -1
        raw = config.http_get(_BROWSE_URL + "?" + urllib.parse.urlencode(params), timeout=30,
                              headers={"Accept-Language": "en-US,en;q=0.8"})
        return _parse_browse_html(raw.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        _log.warning("workshop search failed: %s", e)
        return []


def _query_files(query: str, page: int, api_key: str) -> list[dict]:
    from . import config
    params = {"key": api_key, "appid": RIMWORLD_APPID, "search_text": query, "return_metadata": 1,
              "return_short_description": 1, "numperpage": 50, "page": page, "filetype": 0,
              "query_type": 12 if query else 9}    # 12 = RankedByTextSearch, 9 = RankedByTotalUniqueSubscriptions
    data = json.loads(config.http_get(_QUERY_URL + "?" + urllib.parse.urlencode(params), timeout=30))
    out = []
    for d in _resp_list(data, "publishedfiledetails"):
        item = _result_item(d)
        if item:
            out.append(item)
    return out


def _result_item(d) -> dict | None:
    """Normalize a Steam file-details-like dict (API or page state) to a search result."""
    if not isinstance(d, dict):
        return None
    wid = str(d.get("publishedfileid") or "")
    title = d.get("title")
    app = str(d.get("consumer_appid") or d.get("consumer_app_id") or RIMWORLD_APPID)
    if not wid.isdigit() or not isinstance(title, str) or not title.strip() or app != RIMWORLD_APPID:
        return None
    if d.get("result") not in (None, 1):
        return None
    item = {"workshop_id": wid, "title": _html.unescape(title).strip()}
    try:
        if d.get("subscriptions") is not None:
            item["subscriptions"] = int(d["subscriptions"])
    except (TypeError, ValueError):
        pass
    desc = d.get("short_description") or d.get("description")
    if isinstance(desc, str) and desc:
        item["description"] = desc[:500]
    if isinstance(d.get("preview_url"), str) and d["preview_url"]:
        item["preview_url"] = d["preview_url"]
    return item


# ------------------------------------------------------------------ Web API with a key: full item data
# Raw PublishedFileDetails (tags, required items as 'children', uploader as 'creator') for the AI generator.
_PFS_URL = _API + "/IPublishedFileService/{}/v1/"
_FULL = {"return_tags": 1, "return_children": 1, "return_short_description": 1}


def _pfs(method: str, key: str, params: dict) -> dict:
    """An IPublishedFileService call. The key is in the URL: callers log exception types only."""
    from . import config
    data = json.loads(config.http_get(_PFS_URL.format(method) + "?" + urllib.parse.urlencode({"key": key, **params}),
                                      timeout=30))
    r = data.get("response") if isinstance(data, dict) else None
    return r if isinstance(r, dict) else {}


def _rimworld_items(items) -> list[dict]:
    """RimWorld workshop items Steam serves (file_type 0: not collections, guides or artwork)."""
    return [d for d in items if isinstance(d, dict) and d.get("result", 1) == 1 and not d.get("file_type")
            and str(d.get("publishedfileid", "")).isdigit()
            and str(d.get("consumer_appid") or RIMWORLD_APPID) == RIMWORLD_APPID] if isinstance(items, list) else []


def query_items(text: str, key: str, version: str = "", n: int = 50) -> list[dict]:
    """Steam's own text search (titles and descriptions), best match first, only items tagged `version`."""
    p = {"appid": RIMWORLD_APPID, "search_text": text, "query_type": 12, "numperpage": n, "filetype": 0, **_FULL}
    if version:
        # RimWorld 1.7 supports 1.6 mods, and Workshop mods are tagged 1.6
        p["requiredtags[0]"] = "1.6" if version == "1.7" else version
    return _rimworld_items(_pfs("QueryFiles", key, p).get("publishedfiledetails"))


def user_items(steamid: str, key: str, max_pages: int = 5) -> list[dict]:
    """Every RimWorld workshop item one Steam user published (up to max_pages x 100)."""
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        r = _pfs("GetUserFiles", key, {"steamid": steamid, "appid": RIMWORLD_APPID, "numperpage": 100, "page": page,
                                       "type": "myfiles", **_FULL})
        got = r.get("publishedfiledetails")
        got = got if isinstance(got, list) else []
        out += _rimworld_items(got)
        total = r.get("total")
        if len(got) < 100 or (isinstance(total, int) and page * 100 >= total):
            break
    return out


def item_details(ids: list[str], key: str) -> dict[str, dict]:
    """GetDetails with tags, required items and uploader, 100 ids per call. Omits ids Steam doesn't serve."""
    out: dict[str, dict] = {}
    for chunk in _chunks(_clean_ids(ids), 100):
        p = {"includetags": 1, "includechildren": 1, "short_description": 1,
             **{f"publishedfileids[{n}]": i for n, i in enumerate(chunk)}}
        for d in _rimworld_items(_pfs("GetDetails", key, p).get("publishedfiledetails")):
            out[str(d["publishedfileid"])] = d
    return out


_RE_JSON_PARSE = re.compile(r'JSON\.parse\(\s*(?=")')
_RE_JS_ASSIGN = re.compile(r'(?:window\.SSR\.\w+|var\s+g_\w+)\s*=\s*(?=[\[{])')
_RE_HOVER = re.compile(r'SharedFileBindMouseHover\(\s*"sharedfile_(\d+)"\s*,\s*\w+\s*,\s*(?=\{)')
_RE_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
_RE_ANCHOR = re.compile(r'<a\b[^>]*?\bhref\s*=\s*["\']([^"\']*)["\'][^>]*>(.*?)</a>', re.S | re.I)
_RE_FILE_ID = re.compile(r"filedetails/?\?(?:[^\"'#]*?&(?:amp;)?)?id=(\d+)")
_RE_USER_LINK = re.compile(r"steamcommunity\.com/(?:id|profiles)/|^/(?:id|profiles)/")
_RE_TAG = re.compile(r"<[^>]+>")
_RE_IMG_ALT = re.compile(r'<img\b[^>]*\balt\s*=\s*"([^"]*)"', re.I)


def _walk_json(v, found: list, depth: int = 0) -> None:
    """Collect dicts that look like workshop items; decodes JSON nested inside strings."""
    if depth > 80:
        return
    if isinstance(v, str):
        s = v.lstrip()
        if len(s) > 20 and s[:1] in "{[":
            try:
                _walk_json(json.loads(s), found, depth + 1)
            except ValueError:
                pass
    elif isinstance(v, dict):
        if "publishedfileid" in v and "title" in v:
            found.append(v)
        for x in v.values():
            _walk_json(x, found, depth + 1)
    elif isinstance(v, list):
        for x in v:
            _walk_json(x, found, depth + 1)


def _text(fragment: str) -> str:
    return " ".join(_html.unescape(_RE_TAG.sub(" ", fragment)).split())


def _parse_browse_html(page: str) -> list[dict]:
    """Workshop browse/search page → result items. Layers: embedded JSON state (current React page, old hover JSON),
    then rendered <a> links (old and new markup) for titles/authors. Never raises."""
    items: dict[str, dict] = {}
    try:
        dec = json.JSONDecoder()
        found: list = []
        for rx in (_RE_JSON_PARSE, _RE_JS_ASSIGN):
            for m in rx.finditer(page):
                try:
                    _walk_json(dec.raw_decode(page, m.end())[0], found)
                except (ValueError, RecursionError):
                    pass
        for m in _RE_HOVER.finditer(page):
            try:
                d = dec.raw_decode(page, m.end())[0]
                if isinstance(d, dict):
                    found.append({**d, "publishedfileid": m.group(1)})
            except ValueError:
                pass
        for d in found:
            it = _result_item(d)
            if it and it["workshop_id"] not in items:
                items[it["workshop_id"]] = it
    except Exception as e:  # noqa: BLE001
        _log.debug("browse page JSON layer failed: %s", e)
    have_state = bool(items)
    try:
        anchors = [(m.group(1), m.group(2)) for m in _RE_ANCHOR.finditer(_RE_SCRIPT.sub(" ", page))]
        for n, (href, inner) in enumerate(anchors):
            m = _RE_FILE_ID.search(href)
            if not m:
                continue
            wid = m.group(1)
            if wid not in items:
                if have_state:
                    continue           # page state is authoritative; other links are page chrome/descriptions
                items[wid] = {"workshop_id": wid}
            it = items[wid]
            title = _text(inner)
            if not title:
                alt = _RE_IMG_ALT.search(inner)
                title = _html.unescape(alt.group(1)).strip() if alt else ""
            if title and not it.get("title"):
                it["title"] = title
            if "author" not in it:
                for href2, inner2 in anchors[n + 1:n + 4]:
                    other = _RE_FILE_ID.search(href2)
                    if other and other.group(1) != wid:
                        break
                    if _RE_USER_LINK.search(href2):
                        author = re.sub(r"^by\s+", "", _text(inner2), flags=re.I)
                        if author:
                            it["author"] = author
                        break
    except Exception as e:  # noqa: BLE001
        _log.debug("browse page link layer failed: %s", e)
    return [it for it in items.values() if it.get("title")]


# ------------------------------------------------------------------ openers
def workshop_url(wid) -> str:
    s = str(wid).strip()
    if s.lower().startswith("gh:"):
        return f"https://github.com/{s[3:]}"
    return WORKSHOP_URL.format(s)


def open_in_browser(ids: list[str]) -> None:
    seen: set[str] = set()
    for raw in ids or []:
        s = str(raw).strip()
        if (s.isdigit() or s.lower().startswith("gh:")) and s not in seen:
            seen.add(s)
            webbrowser.open_new_tab(workshop_url(s))


def open_in_steam(ids: list[str]) -> bool:
    """Open the first item's page in the Steam client and any others in the web browser: the client has a single
    browser view, so each steam:// page would replace the previous one. With no steam:// handler every page opens
    in the browser and this returns False."""
    ids = _clean_ids(ids)
    if not ids:
        return True
    try:
        (getattr(os, "startfile", None) or webbrowser.open)(f"steam://url/CommunityFilePage/{ids[0]}")
    except OSError:
        open_in_browser(ids)
        return False
    open_in_browser(ids[1:])
    return True


# ------------------------------------------------------------------ subprocess streaming
def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def kill_children() -> None:
    """Kill every steamcmd / Steamworks helper process this module started and that is still running.
    Also runs at interpreter exit: job threads are daemons, so their own cleanup never runs then."""
    for proc in list(_CHILDREN):
        proc.rmm_killed = True           # _run_stream then reports 'cancelled' (no retry, no result)
        _kill_tree(proc)


atexit.register(kill_children)


def _pump(stream, q: queue.Queue) -> None:
    """Reader thread: split raw output on \\r and \\n (steamcmd redraws lines with \\r), decode leniently."""
    buf = b""
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            *lines, buf = re.split(rb"\r\n|\r|\n", buf + chunk)
            for ln in lines:
                if ln.strip():
                    q.put(ln.decode("utf-8", "replace").strip())
    except (OSError, ValueError):
        pass
    finally:
        if buf.strip():
            q.put(buf.decode("utf-8", "replace").strip())
        q.put(None)


def _run_stream(cmd: list[str], cwd: str | None, on_line, cancel: threading.Event | None = None,
                timeout: float | None = None, idle_timeout: float | None = None,
                env: dict | None = None, on_tick=None) -> tuple[int | None, str]:
    """Run cmd hidden (no console window), calling on_line(text) per output line on the calling thread.
    on_tick() is polled about twice a second; returning True counts as activity for the idle watchdog.
    Returns (exit code, status) where status is '' | 'cancelled' | 'timeout' | 'idle'. Kills the process tree on stop."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=_NO_WINDOW)
    _CHILDREN.add(proc)
    q: queue.Queue = queue.Queue()
    reader = threading.Thread(target=_pump, args=(proc.stdout, q), daemon=True, name="proc-reader")
    reader.start()
    start = last = last_tick = time.monotonic()
    status, eof = "", False
    try:
        while True:
            try:
                line = q.get(timeout=0.2)
            except queue.Empty:
                line = ""
            if line is None:
                eof = True
                break
            now = time.monotonic()
            if line:
                last = now
                on_line(line)
            if on_tick is not None and now - last_tick >= 0.5:
                last_tick = now
                if on_tick():
                    last = now
            if cancel is not None and cancel.is_set():
                status = "cancelled"
            elif timeout and now - start > timeout:
                status = "timeout"
            elif idle_timeout and now - last > idle_timeout:
                status = "idle"
            if status:
                break
    finally:
        if not eof:                      # stopped early: cancel / timeout / error in on_line
            _kill_tree(proc)
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rc = None
        reader.join(timeout=2)
        try:
            proc.stdout.close()
        except OSError:
            pass
        _CHILDREN.discard(proc)
    return rc, status or ("cancelled" if getattr(proc, "rmm_killed", False) else "")


# ------------------------------------------------------------------ SteamCMD
_RE_SC_OK = re.compile(r'Success\. Downloaded item (\d+) to "([^"]*)"(?: \((\d+) bytes\))?')
_RE_SC_FAIL = re.compile(r"ERROR! Download item (\d+) failed \(([^)]*)\)")
_RE_SC_ITEM_ERR = re.compile(r"ERROR!.*?\bitem (\d+)", re.I)
_RE_SC_ERR = re.compile(r"^ERROR|FAILED (?:login|\()|\.\.\.FAILED|Login Failure|No Connection|Fatal Error", re.I)
_RE_PCT = re.compile(r"\[\s*(\d{1,3})%\]")
_RE_KB = re.compile(r"\(([\d,.]+) of ([\d,.]+) KB\)")
_RE_LOG_TS = re.compile(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\]\s*")


class _Tail:
    """Follow a growing log file from its size at creation (steamcmd block-buffers stdout when piped,
    but writes logs/console_log.txt and logs/bootstrap_log.txt live)."""

    def __init__(self, path: str):
        self.path, self.buf = path, b""
        try:
            self.pos = os.path.getsize(path)
        except OSError:
            self.pos = 0

    def read(self) -> list[str]:
        try:
            size = os.path.getsize(self.path)
            if size < self.pos:                  # rotated / truncated
                self.pos, self.buf = 0, b""
            if size == self.pos:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(min(size - self.pos, 1 << 20))
        except OSError:
            return []
        self.pos += len(data)
        *lines, self.buf = (self.buf + data).split(b"\n")
        out = (_RE_LOG_TS.sub("", ln.decode("utf-8", "replace")).strip() for ln in lines)
        return [ln for ln in out if ln]


def _parse_steamcmd_line(line: str) -> tuple[str, str, str, str] | None:
    """('ok', id, message, reported path) | ('fail', id, reason, '') | ('error', '', line, '') | None."""
    m = _RE_SC_OK.search(line)
    if m:
        size = f" ({int(m.group(3)):,} bytes)" if m.group(3) else ""
        return "ok", m.group(1), "downloaded" + size, m.group(2)
    m = _RE_SC_FAIL.search(line)
    if m:
        return "fail", m.group(1), m.group(2).strip() or line, ""
    m = _RE_SC_ITEM_ERR.search(line)
    if m:
        return "fail", m.group(1), line, ""
    if _RE_SC_ERR.search(line):
        return "error", "", line, ""
    return None


def _update_fraction(line: str) -> float | None:
    """Self-update progress from '[ 42%] …' or '(1,234 of 51,245 KB)'."""
    m = _RE_KB.search(line)
    if m:
        try:
            done, total = (float(g.replace(",", "")) for g in m.groups())
            return min(done / total, 1.0) if total else None
        except ValueError:
            return None
    m = _RE_PCT.search(line)
    return min(int(m.group(1)), 100) / 100 if m else None


_RE_FLAKY = re.compile(r"^(?:no connection|failure|busy|timeout|service unavailable|rate limit)", re.I)


def _retryable(message: str) -> bool:
    return not re.search(r"not found|access denied|invalid workshop id|cancelled|fatal error|non-ASCII|log in again",
                         message, re.I)


@contextlib.contextmanager
def _steamcmd_lock(progress: Progress, cancel, what: str):
    """Hold _STEAMCMD_LOCK, waiting cancellably (the login console keeps it until its window closes)."""
    while not _STEAMCMD_LOCK.acquire(timeout=0.5):
        progress(None, "Waiting for another SteamCMD task to finish (login window open?)…")
        if cancel is not None and cancel.is_set():
            raise InterruptedError(f"{what} cancelled")
    try:
        yield
    finally:
        _STEAMCMD_LOCK.release()


class SteamCMD:
    """Valve's command-line Steam client, used to download workshop items (anonymously, or as `username` once that
    account has logged in through login_console, which makes SteamCMD cache its credentials)."""

    def __init__(self, root_dir: str, username: str = ""):
        self.root = os.path.abspath(root_dir)
        self.username = re.sub(r"[^A-Za-z0-9_.@-]", "", username or "")   # one runscript token, nothing else
        self.exe = os.path.join(self.root, "steamcmd.exe")
        self.content_dir = os.path.join(self.root, "steamapps", "workshop", "content", RIMWORLD_APPID)

    def login_console(self, progress: Progress = noop_progress, cancel=None) -> None:
        """Open SteamCMD's own console to log in once (password and Steam Guard are typed there, never seen by us).
        Blocks until the window is closed and holds _STEAMCMD_LOCK meanwhile: a download finishing during the
        login would rewrite config.vdf and drop the cached credentials."""
        if not self.username:
            raise ValueError("Enter a Steam account name first")
        with _steamcmd_lock(progress, cancel, "SteamCMD login"):
            progress(None, "Log in in the SteamCMD window, then close it…")
            p = subprocess.Popen(f'cmd /c ""{self.exe}" +login {self.username} +quit & pause"', cwd=self.root,
                                 creationflags=subprocess.CREATE_NEW_CONSOLE)
            while p.poll() is None:
                if cancel is not None and cancel.wait(0.5):
                    _kill_tree(p)
                    raise InterruptedError("SteamCMD login cancelled")

    def is_installed(self) -> bool:
        return os.path.isfile(self.exe)

    def path_problem(self) -> str:
        """'' or why SteamCMD can't run from this folder (it refuses any path with non-ASCII characters)."""
        if self.root.isascii():
            return ""
        return (f"SteamCMD cannot run from a folder whose path has non-ASCII characters ({self.root}). "
                "Choose an ASCII-only 'SteamCMD folder' in Settings → Game & folders, "
                "for example C:\\RimModManager\\steamcmd.")

    def install(self, progress: Progress = noop_progress, cancel=None) -> None:
        """Download steamcmd.zip (mirrors), extract, then run it once so it self-updates. InterruptedError on cancel."""
        from . import config
        if self.path_problem():
            raise RuntimeError(self.path_problem())
        with _steamcmd_lock(progress, cancel, "SteamCMD installation"):
            os.makedirs(self.root, exist_ok=True)
            zpath = os.path.join(self.root, "steamcmd.zip")
            errors = []
            for url in STEAMCMD_URLS:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("SteamCMD installation cancelled")
                try:
                    config.download_file(url, zpath, timeout=60, cancel=cancel,
                                         progress=lambda f, t: progress(None if f is None else f * 0.2, t))
                    with zipfile.ZipFile(zpath) as z:
                        _safe_extract(z, self.root)
                    break
                except InterruptedError:
                    raise
                except Exception as e:  # noqa: BLE001 - try the next mirror
                    errors.append(f"{url}: {e}")
                    _log.warning("SteamCMD download from %s failed: %s", url, e)
                finally:
                    if os.path.exists(zpath):
                        try:
                            os.remove(zpath)
                        except OSError:
                            pass
            else:
                raise RuntimeError("Could not download SteamCMD:\n" + "\n".join(errors))
            self._self_update(progress, cancel)

    def _run(self, args: list[str], on_line, cancel, timeout: float | None = None,
             idle_timeout: float | None = None) -> tuple[int | None, str]:
        """Run steamcmd with live output. Its stdout arrives in bursts (block-buffered pipe), so the live log files
        are followed too; once they produce lines, stdout only contributes result lines. Duplicates are dropped."""
        tails = [_Tail(os.path.join(self.root, "logs", n)) for n in ("console_log.txt", "bootstrap_log.txt")]
        recent: deque = deque(maxlen=400)
        live = [False]
        dl = os.path.join(self.root, "steamapps", "workshop", "downloads")
        seen = [self._tree_sig(dl), time.monotonic()]

        def feed(line: str, from_log: bool = False) -> None:
            p = _parse_steamcmd_line(line)
            if live[0] and not from_log and p is None:
                return
            key = (p[0], p[1]) if p and p[1] else line
            if key not in recent:
                recent.append(key)
                on_line(line)

        def tick() -> bool:
            new = [ln for t in tails for ln in t.read()]
            live[0] = live[0] or bool(new)
            for ln in new:
                feed(ln, True)
            if new or time.monotonic() - seen[1] < self.DL_POLL_S:
                return bool(new)
            # steamcmd logs nothing while one item downloads: a growing download folder counts as activity too
            sig, seen[1] = self._tree_sig(dl), time.monotonic()
            busy, seen[0] = sig != seen[0], sig
            return busy

        rc, status = _run_stream([self.exe, *args], self.root, feed, cancel, timeout, idle_timeout, on_tick=tick)
        tick()
        return rc, status

    DL_POLL_S = 30.0     # how often the idle watchdog looks at the download folder (a tree walk)

    @staticmethod
    def _tree_sig(path: str) -> tuple[int, int, int]:
        """(files, total bytes, newest mtime_ns) of a folder tree; (0, 0, 0) when missing."""
        n = size = newest = 0
        for dirpath, _dirs, files in os.walk(path):
            for f in files:
                try:
                    st = os.stat(os.path.join(dirpath, f))
                except OSError:
                    continue
                n, size, newest = n + 1, size + st.st_size, max(newest, st.st_mtime_ns)
        return n, size, newest

    def _self_update(self, progress: Progress, cancel) -> None:
        tail: deque[str] = deque(maxlen=12)
        rc, status = None, ""

        def on_line(line: str) -> None:
            tail.append(line)
            f = _update_fraction(line)
            progress(None if f is None else 0.2 + 0.8 * f, "SteamCMD: " + line)

        for attempt in range(2):     # a fresh steamcmd sometimes needs a second run to finish updating
            progress(0.2, "Updating SteamCMD (first run can take a few minutes)…")
            rc, status = self._run(["+quit"], on_line, cancel, timeout=1800, idle_timeout=300)
            if status == "cancelled":
                raise InterruptedError("SteamCMD installation cancelled")
            if not status and self._updated():
                progress(1.0, "SteamCMD is ready")
                return
            _log.warning("SteamCMD self-update attempt %d: exit %s %s", attempt + 1, rc, status)
        raise RuntimeError(f"SteamCMD self-update failed (exit code {rc}{', ' + status if status else ''}):\n"
                           + "\n".join(tail))

    def _updated(self) -> bool:
        """steamcmd.exe alone is only the bootstrapper; a completed update leaves the Steam client DLLs next to it."""
        return any(os.path.isfile(os.path.join(self.root, n)) for n in ("steamclient64.dll", "steamclient.dll"))

    def download(self, ids: list[str], progress: Progress = noop_progress, cancel=None, retries: int = 3,
                 batch: int = 40) -> dict[str, DownloadResult]:
        """Download workshop items (one runscript per batch), retrying failures. Result path = content dir of the item."""
        results: dict[str, DownloadResult] = {}
        wanted = []
        for i in dict.fromkeys(str(x).strip() for x in ids or []):
            if i.isdigit():
                wanted.append(i)
            elif i:
                results[i] = DownloadResult(i, False, "invalid workshop id")
        if not wanted:
            return results
        if self.path_problem():
            return {**results, **{i: DownloadResult(i, False, self.path_problem()) for i in wanted}}
        try:
            if not self.is_installed():
                self.install(progress, cancel)
            elif not self._updated():
                with _steamcmd_lock(progress, cancel, "SteamCMD installation"):
                    self._self_update(progress, cancel)
        except Exception as e:  # noqa: BLE001 - report per item like any other failure
            msg = "cancelled" if isinstance(e, InterruptedError) else f"SteamCMD unavailable: {e}"
            (_log.info if isinstance(e, InterruptedError) else _log.error)("SteamCMD setup failed: %s", e)
            return {**results, **{i: DownloadResult(i, False, msg) for i in wanted}}
        attempts = 1 + max(0, int(retries))
        pending = wanted
        try:
            with _steamcmd_lock(progress, cancel, "SteamCMD download"):
                for attempt in range(attempts):
                    self._reset_workshop_state()
                    if attempt:
                        done = sum(1 for i in wanted if results.get(i) and results[i].ok)
                        progress(done / len(wanted), f"Retrying {len(pending)} failed item(s) (attempt {attempt + 1}/{attempts})…")
                    for chunk in _chunks(pending, max(1, int(batch))):
                        if cancel is not None and cancel.is_set():
                            break
                        try:
                            self._run_batch(chunk, wanted, results, progress, cancel)
                            # a dropped connection to the content servers fails the rest of the batch within
                            # seconds: retry those now, after a pause, instead of only in the next pass
                            flaky = [w for w in chunk if results.get(w) and _RE_FLAKY.search(results[w].message)]
                            if flaky and attempt + 1 < attempts:
                                progress(None, f"Steam connection dropped: retrying {len(flaky)} item(s) in "
                                               f"{self.FLAKY_PAUSE_S:.0f} s…")
                                if cancel is not None and cancel.wait(self.FLAKY_PAUSE_S):
                                    break
                                if cancel is None:
                                    time.sleep(self.FLAKY_PAUSE_S)
                                self._reset_workshop_state()
                                self._run_batch(flaky, wanted, results, progress, cancel)
                        except InterruptedError:
                            raise
                        except Exception as e:  # noqa: BLE001 - keep earlier batches' results (they still get installed)
                            _log.exception("SteamCMD batch failed")
                            for w in chunk:
                                if not (results.get(w) and results[w].ok):
                                    results[w] = DownloadResult(w, False, f"SteamCMD failed: {e}")
                    if cancel is not None and cancel.is_set():
                        break
                    pending = [i for i in pending if not results[i].ok and _retryable(results[i].message)]
                    if not pending:
                        break
        except InterruptedError:
            pass                        # unfinished items become 'cancelled' below
        for i in wanted:
            if i not in results:
                results[i] = DownloadResult(i, False, "cancelled")
        ok = sum(1 for i in wanted if results[i].ok)
        progress(1.0, f"SteamCMD: {ok}/{len(wanted)} item(s) downloaded")
        return results

    def _reset_workshop_state(self) -> None:
        """A cancelled/killed download leaves partial state that makes SteamCMD fail that item with 'Failure' on
        every later attempt, so after such a run, or one where an item failed with anything but a timeout (flag
        file left by _run_batch, also across app restarts), drop it (not the content folder). After SteamCMD's own
        timeouts the partial download is kept so the retry can resume it."""
        flag = os.path.join(self.root, self.DIRTY_FLAG)
        ws = os.path.join(self.root, "steamapps", "workshop")
        acf = os.path.join(ws, f"appworkshop_{RIMWORLD_APPID}.acf")
        if not os.path.exists(flag) and not self._acf_stale(acf):
            return
        for name in ("downloads", "temp"):
            shutil.rmtree(os.path.join(ws, name), ignore_errors=True)
        for p in (acf, flag):
            try:
                os.remove(p)
            except OSError:
                pass

    def _acf_stale(self, acf: str) -> bool:
        """The acf lists an installed item whose content folder is gone (moved into Mods/): SteamCMD would read
        chunks from it on the next delta update and fail with 'Missing game files'."""
        try:
            with open(acf, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return False
        block = text.partition('"WorkshopItemsInstalled"')[2].partition('"WorkshopItemDetails"')[0]
        ids = re.findall(r'^\s*"(\d+)"\s*$', block, re.M)
        return any(not os.path.isdir(os.path.join(self.content_dir, i)) for i in ids)

    FLAKY_PAUSE_S = 15.0     # wait before retrying items that failed because Steam's connection dropped
    DIRTY_FLAG = "rmm_download_running.flag"  # exists while a download runs and stays if it did not end cleanly

    def _run_batch(self, chunk: list[str], wanted: list[str], results: dict, progress: Progress, cancel) -> None:
        script = os.path.join(self.root, "rmm_download.txt")
        lines = ["@ShutdownOnFailedCommand 0", "@NoPromptForPassword 1", f'force_install_dir "{self.root}"',
                 f"login {self.username or 'anonymous'}", *(f"workshop_download_item {RIMWORLD_APPID} {i} validate" for i in chunk), "quit"]
        with open(script, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        pending = set(chunk)
        last_error = [""]
        login_failed = [False]
        total = len(wanted)

        def on_line(line: str) -> None:
            if self.username and _RE_LOGIN_FAIL.search(line):
                login_failed[0] = True
            p = _parse_steamcmd_line(line)
            if p:
                kind, wid, msg, reported = p
                if kind == "ok" and wid in chunk:
                    path = os.path.join(self.content_dir, wid)
                    if not os.path.isdir(path) and reported and os.path.isdir(reported):
                        path = os.path.abspath(reported)
                    ok = os.path.isdir(path)
                    results[wid] = DownloadResult(wid, ok, msg if ok else f"SteamCMD reported success but {path} is missing",
                                                  path if ok else "")
                    pending.discard(wid)
                elif kind == "fail" and wid in pending:
                    results[wid] = DownloadResult(wid, False, msg)
                    pending.discard(wid)
                elif kind == "error":
                    last_error[0] = msg
            done = sum(1 for i in wanted if results.get(i) and results[i].ok)
            progress(min(done / total, 1.0), line)

        progress(None, f"SteamCMD: downloading {len(chunk)} item(s)…")
        flag = os.path.join(self.root, self.DIRTY_FLAG)
        open(flag, "w").close()
        status = "error"
        try:
            # idle = no log lines and no growth of the download folder for 30 min
            rc, status = self._run(["+runscript", os.path.basename(script)], on_line, cancel, idle_timeout=1800)
        finally:
            # keep the flag (→ state reset before the next run) when we killed it, or when an item failed with
            # anything but a timeout: SteamCMD resumes its own timeouts, but e.g. a 'Failure' with partial state
            # left behind fails again on every retry
            failed = [results[w].message for w in chunk if w in results and not results[w].ok]
            dirty = status or any("timeout" not in m.lower() for m in failed)
            for p in (script, flag) if not dirty else (script,):
                try:
                    os.remove(p)
                except OSError:
                    pass
        reason = {"cancelled": "cancelled", "timeout": "SteamCMD timed out",
                  "idle": "SteamCMD stopped responding"}.get(status) or last_error[0] \
            or f"no result from SteamCMD (exit code {rc})"
        if login_failed[0] and status != "cancelled":
            # most workshop items download anonymously: don't fail the whole list over an expired login
            _log.warning("SteamCMD login as %s failed; retrying %d item(s) anonymously", self.username, len(chunk))
            progress(None, "SteamCMD login expired or missing: downloading anonymously instead "
                           "(log in again: Settings → Downloads → Log in…)")
            self.username = ""
            for w in chunk:
                if not (results.get(w) and results[w].ok):
                    results.pop(w, None)
            self._run_batch(chunk, wanted, results, progress, cancel)
            return
        if status == "cancelled":       # SteamCMD may print 'failed (Failure)' for the item it was killed on
            pending.update(w for w in chunk if not (results.get(w) and results[w].ok))
        for wid in pending:
            results[wid] = DownloadResult(wid, False, reason)


def _safe_extract(z: zipfile.ZipFile, root: str) -> None:
    real_root = os.path.realpath(root)
    for name in z.namelist():
        target = os.path.realpath(os.path.join(root, name))
        if target != real_root and not target.startswith(real_root + os.sep):
            raise RuntimeError(f"unsafe path in zip: {name}")
    z.extractall(root)


# ------------------------------------------------------------------ installing downloads
def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def _is_link(p: str) -> bool:
    return os.path.islink(p) or bool(getattr(os.path, "isjunction", lambda _p: False)(p))


def _rm_readonly(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove(path: str) -> None:
    """Delete a junction/symlink (never its target), a folder tree, or a file."""
    if _is_link(path):
        try:
            os.rmdir(path)
        except OSError:
            os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path, onexc=_rm_readonly)
    else:
        os.remove(path)


def _make_junction(src: str, dest: str) -> None:
    if os.name != "nt":
        os.symlink(src, dest, target_is_directory=True)
        return
    try:
        import _winapi
        _winapi.CreateJunction(src, dest)
    except (ImportError, AttributeError, OSError) as e:
        _log.info("CreateJunction failed (%s); using mklink", e)
        r = subprocess.run(["cmd", "/c", "mklink", "/J", dest, src], capture_output=True, timeout=30,
                           creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)
        if not os.path.isdir(dest):
            raise OSError(f"mklink /J failed: {r.stdout.decode('mbcs', 'replace').strip()}")


_RE_STALE = re.compile(r"^(.+)\.rmm_(old|new)_\d+$")      # our temporary names (staging folder; legacy: Mods/)


def _staging_root(mods_dir: str) -> str:
    """Where installs build new copies and park old ones: next to Mods (same volume, so the swap is a rename) but
    outside it, so RimWorld can never load a half-copied mod left by a killed install."""
    return os.path.join(os.path.dirname(os.path.realpath(mods_dir)), "_RimModManager_staging")


def _rename_retry(a: str, b: str, tries: int = 5) -> None:
    """os.rename that rides out brief sharing violations (antivirus scanning new files, an Explorer window)."""
    for attempt in range(tries):
        try:
            os.rename(a, b)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _install_one(src: str, dest: str, mods_dir: str, mode: str, any_name: bool = False) -> None:
    name = os.path.basename(dest)
    # Strict safety: the only things we ever replace are <mods_dir>/<numeric id>, a folder directly inside mods_dir
    # that the caller named explicitly (any_name), and our own temporary '<name>.rmm_new_/rmm_old_<ts>' copies.
    if not (name.isdigit() or (any_name and name and not _RE_STALE.match(name))) \
            or os.path.normcase(os.path.dirname(os.path.abspath(dest))) != os.path.normcase(os.path.abspath(mods_dir)):
        raise RuntimeError(f"refusing to touch {dest}")
    s, d = _norm(src), os.path.normcase(os.path.abspath(dest))
    if _is_link(dest):
        if mode == "link" and _norm(dest) == s:
            return                               # already linked to this copy
    elif os.path.lexists(dest) and _norm(dest) == s:
        return                                   # the download already lives at <mods_dir>/<id>
    if d.startswith(s + os.sep) or s.startswith(d + os.sep):
        raise RuntimeError(f"source {src} and destination {dest} overlap")
    # Crash-safe: build the new copy in the staging folder (the slow part), then swap with two quick renames.
    # Whatever a kill leaves behind is undone by cleanup_stale_installs.
    root = _staging_root(mods_dir)
    os.makedirs(root, exist_ok=True)
    ts = int(time.time() * 1000)
    stage, old = os.path.join(root, f"{name}.rmm_new_{ts}"), os.path.join(root, f"{name}.rmm_old_{ts}")
    moved = False
    try:
        if mode == "copy":
            shutil.copytree(src, stage)
        elif mode == "link":
            _make_junction(src, stage)
        else:
            try:
                os.rename(src, stage)            # same volume: instant
                moved = True
            except OSError:
                shutil.copytree(src, stage)      # cross-drive move: copy now, drop the source once installed
        try:
            if os.path.lexists(dest):
                _rename_retry(dest, old)
            try:
                _rename_retry(stage, dest)
            except BaseException:
                if os.path.lexists(old):
                    _rename_retry(old, dest)
                raise
        except PermissionError as e:
            raise PermissionError(f"{dest} is in use - close RimWorld/Explorer windows showing it and try again") from e
    except BaseException:
        if os.path.lexists(stage):
            try:
                os.rename(stage, src) if moved else _remove(stage)
            except OSError as e:
                _log.error("could not clean up partial %s: %s", stage, e)
        raise
    if mode == "move" and not moved:
        shutil.rmtree(src, ignore_errors=True)
    if os.path.lexists(old):
        try:
            _remove(old)
        except OSError as e:
            _log.warning("could not delete old copy %s (removed on next scan): %s", old, e)


def install_items(results: dict[str, DownloadResult], mods_dir: str, mode: str = "move",
                  targets: dict[str, str] | None = None) -> dict[str, DownloadResult]:
    """Move/copy/junction each successful download into <mods_dir>/<id>, replacing an existing <mods_dir>/<id>.
    targets maps a workshop id to an existing folder directly inside mods_dir to replace instead (updating a mod
    whose folder is not named by its id). Failed or unusable results pass through unchanged (or become failures
    with the install error)."""
    if mode not in ("move", "copy", "link"):
        raise ValueError(f"unknown install mode {mode!r}")
    mods_dir = os.path.abspath(mods_dir)
    os.makedirs(mods_dir, exist_ok=True)
    targets = targets or {}
    out: dict[str, DownloadResult] = {}
    with _INSTALL_LOCK:
        for key, r in results.items():
            wid = str(r.workshop_id or key).strip()
            if not r.ok:
                out[key] = r
                continue
            if not wid.isdigit() or not r.path or not os.path.isdir(r.path):
                out[key] = DownloadResult(wid, False, f"nothing to install ({r.path or 'no path'})", r.path)
                continue
            dest = os.path.abspath(targets[wid]) if targets.get(wid) else os.path.join(mods_dir, wid)
            try:
                _install_one(os.path.abspath(r.path), dest, mods_dir, mode, any_name=bool(targets.get(wid)))
                out[key] = DownloadResult(wid, True, f"installed ({mode})", dest)
            except Exception as e:  # noqa: BLE001
                _log.error("installing %s into %s failed: %s", wid, mods_dir, e)
                out[key] = DownloadResult(wid, False, f"install failed: {e}", r.path)
        try:
            os.rmdir(_staging_root(mods_dir))     # only when empty: no leftover folder in the game dir
        except OSError:
            pass
    return out


_GH_REPO_RE = re.compile(r"^(?:gh:|https?://github\.com/)?([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+?)(?:\.git)?(?:[/?#].*)?$", re.I)


def parse_github_repo(s: str) -> str:
    """Return 'owner/repo' (lowercased) from 'gh:owner/repo' or a github.com URL, or ''."""
    m = _GH_REPO_RE.match((s or "").strip())
    if not m:
        return ""
    repo = m.group(1).lower()
    owner, _, name = repo.partition("/")
    if not owner or not name or owner in ("topics", "search", "settings", "orgs", "users", "marketplace"):
        return ""
    return repo


def _find_mod_root_in_dir(root: str) -> str:
    """Find the folder inside an extracted archive that contains About/About.xml (case-insensitive)."""
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            if d.lower() == "about":
                about_dir = os.path.join(dirpath, d)
                try:
                    if any(f.lower() == "about.xml" for f in os.listdir(about_dir)):
                        return dirpath
                except OSError:
                    pass
    return ""


def download_github_mods(ids: list[str], mods_dir: str, progress: Progress = noop_progress, cancel=None,
                         targets: dict[str, str] | None = None) -> dict[str, DownloadResult]:
    """Download and install GitHub-hosted RimWorld mods ('gh:owner/repo') into <mods_dir>/<repo_name>."""
    from . import config
    mods_dir = os.path.abspath(mods_dir)
    os.makedirs(mods_dir, exist_ok=True)
    targets = targets or {}
    results: dict[str, DownloadResult] = {}
    wanted = list(dict.fromkeys(str(x).strip() for x in ids or [] if parse_github_repo(str(x))))
    if not wanted:
        return results
    stage_root = _staging_root(mods_dir)
    os.makedirs(stage_root, exist_ok=True)
    hdrs = {"Accept": "application/vnd.github+json", "User-Agent": "RimModManager"}
    for idx, raw_id in enumerate(wanted):
        repo = parse_github_repo(raw_id)
        wid = f"gh:{repo}"
        if cancel is not None and cancel.is_set():
            results[raw_id] = DownloadResult(wid, False, "cancelled")
            continue
        owner, _, repo_name = repo.partition("/")
        progress(idx / len(wanted), f"GitHub: downloading {repo} ({idx + 1}/{len(wanted)})…")
        zip_urls: list[str] = []
        # 1. Check latest GitHub release for a packaged .zip asset first (contains compiled Assemblies), then source zipball
        try:
            rel_raw = config.http_get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=15, headers=hdrs)
            rel = json.loads(rel_raw.decode("utf-8", "replace"))
            if isinstance(rel, dict):
                for asset in rel.get("assets") or []:
                    if isinstance(asset, dict):
                        dl_url = str(asset.get("browser_download_url") or "")
                        if dl_url.lower().endswith(".zip"):
                            zip_urls.append(dl_url)
                if rel.get("zipball_url"):
                    zip_urls.append(str(rel["zipball_url"]))
        except Exception as e:  # noqa: BLE001 - most RimWorld repos have no Releases and use default branch
            _log.debug("GitHub releases lookup for %s skipped: %s", repo, e)
        # 2. Fallback to default branch archive (HEAD, main, master)
        zip_urls.extend([
            f"https://codeload.github.com/{repo}/zip/HEAD",
            f"https://github.com/{repo}/archive/refs/heads/main.zip",
            f"https://github.com/{repo}/archive/refs/heads/master.zip",
        ])
        ts = int(time.time() * 1000)
        zpath = os.path.join(stage_root, f"gh_{repo_name}_{ts}.zip")
        ext_dir = os.path.join(stage_root, f"gh_ext_{repo_name}_{ts}")
        last_err = "download failed"
        downloaded = False
        try:
            for url in dict.fromkeys(zip_urls):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("cancelled")
                try:
                    config.download_file(url, zpath, timeout=90, cancel=cancel,
                                         progress=lambda f, t, _i=idx: progress(
                                             (_i + (f or 0.0)) / len(wanted), f"GitHub ({repo}): {t}"))
                    os.makedirs(ext_dir, exist_ok=True)
                    with zipfile.ZipFile(zpath) as zf:
                        _safe_extract(zf, ext_dir)
                    downloaded = True
                    break
                except InterruptedError:
                    raise
                except Exception as e:  # noqa: BLE001
                    last_err = str(e)
                    shutil.rmtree(ext_dir, ignore_errors=True)
                    continue
            if not downloaded:
                results[raw_id] = DownloadResult(wid, False, f"GitHub download failed for {repo}: {last_err}")
                continue
            mod_root = _find_mod_root_in_dir(ext_dir)
            if not mod_root:
                results[raw_id] = DownloadResult(wid, False, f"{repo} has no About/About.xml (not a ready-to-load RimWorld mod)")
                continue
            about_dir = os.path.join(mod_root, "About")
            try:
                with open(os.path.join(about_dir, "GitHubRepo.txt"), "w", encoding="utf-8") as f:
                    f.write(repo + "\n")
            except OSError:
                pass
            safe_folder = re.sub(r"[^A-Za-z0-9_.\-]", "_", repo_name).strip("._") or "github_mod"
            dest = os.path.abspath(targets[raw_id]) if targets.get(raw_id) else os.path.join(mods_dir, safe_folder)
            with _INSTALL_LOCK:
                _install_one(mod_root, dest, mods_dir, "move", any_name=True)
            results[raw_id] = DownloadResult(wid, True, f"installed from GitHub ({repo})", dest)
        except InterruptedError:
            results[raw_id] = DownloadResult(wid, False, "cancelled")
        except Exception as e:  # noqa: BLE001
            _log.error("GitHub install of %s failed: %s", repo, e)
            results[raw_id] = DownloadResult(wid, False, f"GitHub install failed: {e}")
        finally:
            for p in (zpath, ext_dir):
                if os.path.exists(p):
                    try:
                        _remove(p)
                    except OSError:
                        pass
    try:
        os.rmdir(stage_root)
    except OSError:
        pass
    ok = sum(1 for r in results.values() if r.ok)
    progress(1.0, f"GitHub: {ok}/{len(wanted)} mod(s) installed")
    return results


def cleanup_stale_installs(mods_dir: str) -> list[str]:
    """Undo what a killed install left in the staging folder next to mods_dir (and legacy copies inside mods_dir):
    '<name>.rmm_old_<ts>' is renamed back to mods_dir/<name> when that is missing; every other '*.rmm_old_*' /
    '*.rmm_new_*' is deleted (a junction: only the link). Returns messages."""
    msgs: list[str] = []
    if not _INSTALL_LOCK.acquire(blocking=False):
        return msgs                              # an install is running: its temporary folders are not stale
    try:
        root = _staging_root(mods_dir)
        for d in (root, mods_dir):
            try:
                names = sorted(os.listdir(d), reverse=True)     # per name: rmm_old before rmm_new, newest first
            except OSError:
                continue
            for n in names:
                m = _RE_STALE.match(n)
                if not m:
                    continue
                p, final = os.path.join(d, n), os.path.join(mods_dir, m.group(1))
                try:
                    if m.group(2) == "old" and not os.path.lexists(final):
                        os.rename(p, final)
                        msgs.append(f"Restored {m.group(1)} after an interrupted install")
                    else:
                        _remove(p)
                        msgs.append(f"Removed {n} left by an interrupted install")
                except OSError as e:
                    msgs.append(f"Could not clean up {n} (left by an interrupted install): {e}")
        try:
            os.rmdir(root)
        except OSError:
            pass
    finally:
        _INSTALL_LOCK.release()
    return msgs


# ------------------------------------------------------------------ Steamworks (subprocess + ctypes worker)
def steamworks_available(game_dir: str | None) -> bool:
    if os.name != "nt" or sys.maxsize <= 2 ** 32 or not game_dir:
        return False
    from . import paths
    return bool(paths.steam_api_dll(game_dir))


def _sw_fail(msg: str) -> dict:
    return {"ok": False, "error": msg, "items": {}, "subscribed": []}


def steamworks_call(action: str, ids: list[str], game_dir: str, progress: Progress = noop_progress,
                    timeout: float = 600, cancel: threading.Event | None = None) -> dict:
    """Run a Steamworks action in a helper process (Steam sees 'RimWorld running' only while it lives).
    Returns {'ok', 'error', 'items': {id: {...}}, 'subscribed': [...]}. Never raises. cancel.set() kills the helper
    (error 'cancelled'); Steam keeps downloading items that are already subscribed."""
    if action not in _SW_ACTIONS:
        return _sw_fail(f"unknown Steamworks action {action!r}")
    if os.name != "nt":
        return _sw_fail("Steamworks downloads are only supported on Windows")
    ids = _clean_ids(ids)
    if action != "list_subscribed" and not ids:
        return {"ok": True, "error": "", "items": {}, "subscribed": []}
    try:
        from . import paths
        dll = paths.steam_api_dll(game_dir) if game_dir else None
    except Exception as e:  # noqa: BLE001
        _log.warning("steam_api64.dll lookup failed: %s", e)
        dll = None
    if not dll:
        return _sw_fail("steam_api64.dll not found – set the RimWorld game folder in Settings")
    # a Steam copy lives in <library>/steamapps/common/RimWorld; Steam stages its workshop downloads in that library
    downloads = os.path.normpath(os.path.join(game_dir, os.pardir, os.pardir, "workshop", "downloads", RIMWORLD_APPID))
    payload = json.dumps({"action": action, "ids": ids, "dll": dll, "timeout": timeout, "downloads": downloads})
    if getattr(sys, "frozen", False):
        cmd, cwd = [sys.executable, "--steamworks", payload], None
    else:
        cmd, cwd = [sys.executable, "-c", _WORKER_CODE, payload], os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, SteamAppId=RIMWORLD_APPID, SteamGameId=RIMWORLD_APPID)
    result: dict = {}
    tail: deque[str] = deque(maxlen=8)

    def on_line(line: str) -> None:
        k = line.find("{")
        obj = None
        if k >= 0:
            try:
                obj = json.loads(line[k:])
            except ValueError:
                obj = None
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            result.update(obj["result"])
        elif isinstance(obj, dict) and ("progress" in obj or "text" in obj):
            f = obj.get("progress")
            progress(f if isinstance(f, (int, float)) else None, str(obj.get("text", "")))
        else:
            tail.append(line)
            _log.debug("steamworks helper: %s", line)

    try:
        # the helper reports at least every second while it works: 5 silent minutes = hung
        rc, status = _run_stream(cmd, cwd, on_line, cancel=cancel, timeout=float(timeout) + 60, idle_timeout=300, env=env)
    except OSError as e:
        return _sw_fail(f"could not start the Steamworks helper: {e}")
    if not result and status == "cancelled":
        return _sw_fail("cancelled")
    if not result:
        why = "timed out" if status else f"exited without a result (code {rc})"
        return _sw_fail(f"Steamworks helper {why}" + (": " + " | ".join(tail) if tail else ""))
    out = _sw_fail("")
    out.update(result)
    return out


_ITEM_FLAGS = {1: "subscribed", 2: "legacy", 4: "installed", 8: "needs_update", 16: "downloading",
               32: "download_pending", 64: "disabled_locally"}
_BUSY = 8 | 16 | 32
_SW_MISSING = ("Steam says it is installed but its folder is missing: unsubscribe and subscribe again, "
               "verify RimWorld's files in Steam, or download it with SteamCMD")
_SW_LATER = "Steam has not finished it yet; it keeps downloading in the background (Rescan later)"
_SW_RETRY = {0, 2, 3, 10, 16, 25, 84}   # call failed, Fail, No connection, Busy, Timeout, (Rate) limit exceeded


def _usable(d: dict) -> bool:
    """An info() result Steam calls installed and up to date, with the mod really in its folder."""
    return bool(d["state"] & 4 and not d["state"] & _BUSY and d.get("path")
                and os.path.isfile(os.path.join(d["path"], "About", "About.xml")))
_ERESULT = {1: "OK", 2: "Fail", 3: "No connection", 8: "Invalid parameter", 9: "File not found", 10: "Busy",
            15: "Access denied", 16: "Timeout", 25: "Limit exceeded", 84: "Rate limit exceeded"}


class _Steamworks:
    """Just enough of the Steamworks flat API (steam_api64.dll via ctypes) for workshop subscribe/download/state."""

    def __init__(self, dll: str):
        import ctypes
        self.C = ctypes
        if hasattr(os, "add_dll_directory"):
            self._dll_dir = os.add_dll_directory(os.path.dirname(dll))   # keep the handle alive
        self.lib = ctypes.CDLL(dll)
        self.inited = False
        self.ugc = self.utils = None

    def _fn(self, name: str, restype, *argtypes, required: bool = True):
        f = getattr(self.lib, name, None)
        if f is None:
            if required:
                raise RuntimeError(f"{name} is not exported by this steam_api64.dll")
            return None
        f.restype, f.argtypes = restype, list(argtypes)
        return f

    def _accessor(self, prefix: str):
        for nn in range(25, 9, -1):
            f = self._fn(f"{prefix}{nn:03d}", self.C.c_void_p, required=False)
            if f is not None:
                ptr = f()
                if ptr:
                    return ptr
        return None

    def init(self) -> str:
        """'' on success, else a user-facing error."""
        C = self.C
        running = self._fn("SteamAPI_IsSteamRunning", C.c_bool, required=False)
        if running is not None and not running():
            return "Steam is not running. Start Steam, log in, then try again."
        err = C.create_string_buffer(1024)
        if (f := self._fn("SteamAPI_InitFlat", C.c_int, C.c_void_p, required=False)) is not None:
            ok = f(err) == 0
        elif (f := self._fn("SteamAPI_Init", C.c_bool, required=False)) is not None:
            ok = bool(f())
        elif (f := self._fn("SteamAPI_InitSafe", C.c_bool, required=False)) is not None:
            ok = bool(f())
        elif (f := self._fn("SteamInternal_SteamAPI_Init", C.c_int, C.c_char_p, C.c_void_p, required=False)) is not None:
            ok = f(None, err) == 0
        else:
            return "unsupported steam_api64.dll (no SteamAPI_Init export)"
        if not ok:
            detail = err.value.decode("utf-8", "replace").strip()
            return ("SteamAPI_Init failed" + (f": {detail}" if detail else "")
                    + " – is Steam running and logged in with an account that owns RimWorld?")
        self.inited = True
        self.ugc = self._accessor("SteamAPI_SteamUGC_v")
        self.utils = self._accessor("SteamAPI_SteamUtils_v")
        if not self.ugc or not self.utils:
            return "ISteamUGC / ISteamUtils interface not available in this steam_api64.dll"
        V, U64, U32, B, P = C.c_void_p, C.c_uint64, C.c_uint32, C.c_bool, C.POINTER
        self.RunCallbacks = self._fn("SteamAPI_RunCallbacks", None)
        self.GetItemState = self._fn("SteamAPI_ISteamUGC_GetItemState", U32, V, U64)
        self.GetItemInstallInfo = self._fn("SteamAPI_ISteamUGC_GetItemInstallInfo", B, V, U64, P(U64), V, U32, P(U32))
        self.GetItemDownloadInfo = self._fn("SteamAPI_ISteamUGC_GetItemDownloadInfo", B, V, U64, P(U64), P(U64),
                                            required=False)
        self.DownloadItem = self._fn("SteamAPI_ISteamUGC_DownloadItem", B, V, U64, B, required=False)
        self.SubscribeItem = self._fn("SteamAPI_ISteamUGC_SubscribeItem", U64, V, U64, required=False)
        self.UnsubscribeItem = self._fn("SteamAPI_ISteamUGC_UnsubscribeItem", U64, V, U64, required=False)
        # newer SDKs add a trailing bool bIncludeLocallyDisabled; passing it to older ones is harmless on x64
        self.GetNumSubscribedItems = self._fn("SteamAPI_ISteamUGC_GetNumSubscribedItems", U32, V, B)
        self.GetSubscribedItems = self._fn("SteamAPI_ISteamUGC_GetSubscribedItems", U32, V, P(U64), U32, B)
        self.IsAPICallCompleted = self._fn("SteamAPI_ISteamUtils_IsAPICallCompleted", B, V, U64, P(B))
        self.GetAPICallResult = self._fn("SteamAPI_ISteamUtils_GetAPICallResult", B, V, U64, V, C.c_int, C.c_int, P(B),
                                         required=False)
        return ""

    def shutdown(self) -> None:
        if self.inited:
            self.inited = False
            self._fn("SteamAPI_Shutdown", None)()

    def info(self, wid: str) -> dict:
        C = self.C
        st = int(self.GetItemState(self.ugc, int(wid)))
        d = {"state": st, "flags": [n for bit, n in _ITEM_FLAGS.items() if st & bit], "subscribed": bool(st & 1),
             "installed": bool(st & 4), "needs_update": bool(st & 8), "path": "", "size": 0, "timestamp": 0}
        if st & 4:
            size, ts, buf = C.c_uint64(), C.c_uint32(), C.create_string_buffer(2048)
            if self.GetItemInstallInfo(self.ugc, int(wid), C.byref(size), buf, len(buf), C.byref(ts)):
                d.update(path=buf.value.decode("utf-8", "replace"), size=size.value, timestamp=ts.value)
        if st & (16 | 32) and self.GetItemDownloadInfo is not None:
            done, total = C.c_uint64(), C.c_uint64()
            if self.GetItemDownloadInfo(self.ugc, int(wid), C.byref(done), C.byref(total)):
                d.update(bytes_done=done.value, bytes_total=total.value)
        return d

    def subscribed(self) -> list[str]:
        C = self.C
        n = int(self.GetNumSubscribedItems(self.ugc, False))
        if n <= 0:
            return []
        arr = (C.c_uint64 * n)()
        got = int(self.GetSubscribedItems(self.ugc, arr, n, False))
        return [str(arr[i]) for i in range(min(got, n))]

    def _call_result(self, handle: int, callback_id: int, io_failed: bool) -> tuple[int, str]:
        """(EResult, text) of a completed SteamAPICall_t; EResult 0 means the call itself failed."""
        C = self.C
        if io_failed:
            return 0, "Steam API call failed"
        if self.GetAPICallResult is not None:
            # RemoteStorage(Un)SubscribePublishedFileResult_t { EResult; uint64 id; } — 16 bytes with pack 8
            buf, failed2 = (C.c_ubyte * 16)(), C.c_bool(False)
            if self.GetAPICallResult(self.utils, handle, buf, 16, callback_id, C.byref(failed2)):
                er = int.from_bytes(bytes(buf[0:4]), "little", signed=True)
                return er, _ERESULT.get(er, f"EResult {er}")
        return 1, "OK"

    def subscribe_all(self, wids: list[str], deadline: float, emit, unsubscribe: bool = False) -> dict[str, str]:
        """(Un)SubscribeItem for every id with SUB_INFLIGHT calls in flight (one at a time took minutes for 300 mods);
        busy / rate-limit / timeout answers are retried twice. Returns {id: error} for the ones that failed."""
        C = self.C
        fn, callback_id = (self.UnsubscribeItem, 1315) if unsubscribe else (self.SubscribeItem, 1313)
        if fn is None:
            return {w: "not supported by this steam_api64.dll" for w in wids}
        verb = "Unsubscribing" if unsubscribe else "Subscribing"
        todo = [(w, 0.0) for w in wids]                     # (id, not before)
        inflight: dict[int, str] = {}
        tries: dict[str, int] = {}
        failed: dict[str, str] = {}
        n, last_emit = len(wids), 0.0
        last_change = time.monotonic()
        while (todo or inflight) and time.monotonic() < deadline:
            now = time.monotonic()
            for item in [t for t in todo if t[1] <= now][:max(0, self.SUB_INFLIGHT - len(inflight))]:
                todo.remove(item)
                last_change = now
                handle = int(fn(self.ugc, int(item[0])))
                if handle:
                    inflight[handle] = item[0]
                else:
                    failed[item[0]] = "Steam refused the request"
            self.RunCallbacks()
            for handle, w in list(inflight.items()):
                io_failed = C.c_bool(False)
                if not self.IsAPICallCompleted(self.utils, handle, C.byref(io_failed)):
                    continue
                del inflight[handle]
                last_change = now
                er, msg = self._call_result(handle, callback_id, io_failed.value)
                if er != 1 and er in _SW_RETRY and tries.get(w, 0) < 2:
                    tries[w] = tries.get(w, 0) + 1
                    todo.append((w, now + self.RETRY_S * tries[w]))
                elif er != 1:
                    failed[w] = msg
                    emit({"text": f"{verb} {w} failed ({msg})"})
            if now - last_emit > 1.0:
                last_emit = now
                left = len(todo) + len(inflight)
                emit({"progress": (n - left) / max(1, n), "text": f"{verb}: {n - left}/{n} done"})
            if inflight and now - last_change > self.STALL_S:     # Steam never answered these calls
                break
            time.sleep(0.02)
        for w in [t[0] for t in todo] + list(inflight.values()):
            failed.setdefault(w, "timed out waiting for Steam")
        return failed

    def download(self, ids: list[str], deadline: float, emit, downloads: str = "") -> dict:
        """Subscribe, then wait while Steam downloads. No forced DownloadItem per mod: high-priority requests make
        Steam fetch mods one job at a time, while plain subscriptions arrive as one batch job (about 300 mods in
        5 minutes in this PC's Steam logs). Waits as long as anything changes, including Steam's staging folder
        `downloads` (workshop/downloads/294100); STALL_S without a change ends it."""
        items: dict[str, dict] = {}
        n = len(ids)
        new = [w for w in ids if not self.GetItemState(self.ugc, int(w)) & 1]
        if new:
            emit({"progress": 0.0, "text": f"Subscribing to {len(new)} mod(s) in Steam…"})
            for w, msg in self.subscribe_all(new, deadline, emit).items():
                items[w] = {**self.info(w), "ok": False, "message": "subscribe failed: " + msg}
        pending = [w for w in ids if w not in items]
        nudged: dict[str, float] = {}     # not queued by Steam after NUDGE_S: asked once
        revived: dict[str, float] = {}    # 'installed' with the folder gone: asked once, given up after a grace
        started = last_change = last_emit = last_note = time.monotonic()
        last_sig, tree, tree_at = None, None, 0.0
        while pending and time.monotonic() < deadline:
            self.RunCallbacks()
            now = time.monotonic()
            partial, states, busy = 0.0, [], 0
            for wid in list(pending):
                d = self.info(wid)
                st = d["state"]
                states.append(st)
                if st & 4 and not st & _BUSY:
                    if _usable(d):
                        items[wid] = {**d, "ok": True, "message": "installed"}
                        pending.remove(wid)
                        emit({"progress": min((n - len(pending)) / n, 1.0), "text": f"Success: Steam installed {wid}"})
                    elif now - started > self.MISSING_GRACE_S:
                        # Steam's record says installed but the folder is gone: ask for it once, then give up
                        if wid not in revived:
                            self._nudge(wid, revived, now)
                        elif now - revived[wid] > self.MISSING_GRACE_S:
                            items[wid] = {**d, "ok": False, "message": _SW_MISSING}
                            pending.remove(wid)
                elif st & (16 | 32):
                    busy += 1
                    if d.get("bytes_total"):
                        partial += d["bytes_done"] / d["bytes_total"]
                elif wid not in nudged and now - started > self.NUDGE_S:
                    self._nudge(wid, nudged, now)    # subscribed, yet Steam has not queued it (e.g. an update)
            if downloads and now - tree_at > 30:   # a batch stages here; per-mod numbers can sit still meanwhile
                tree, tree_at = SteamCMD._tree_sig(downloads), now
            sig = (len(pending), round(partial, 3), tuple(states), tree)
            if sig != last_sig:
                last_sig, last_change = sig, now
            elif now - last_change > self.STALL_S:
                break
            if now - last_emit > 1.0:
                last_emit = now
                note = ""
                if now - last_note > 60:
                    last_note = now
                    note = f"Steam: {n - len(pending)}/{n} installed, {busy} downloading"
                emit({"progress": min((n - len(pending) + partial) / n, 1.0), "text": note})
            if pending:
                time.sleep(0.5)
        for wid in pending:
            d = self.info(wid)
            if _usable(d):                                  # finished during the last sleep
                items[wid] = {**d, "ok": True, "message": "installed"}
            else:
                stale = d["state"] & 4 and not d["state"] & _BUSY
                items[wid] = {**d, "ok": False, "message": _SW_MISSING if stale else _SW_LATER}
        return items

    def _nudge(self, wid: str, nudged: dict, now: float) -> None:
        nudged[wid] = now
        if self.DownloadItem is not None:
            self.DownloadItem(self.ugc, int(wid), False)

    SUB_INFLIGHT = 32
    RETRY_S = 3.0
    NUDGE_S = 20.0
    MISSING_GRACE_S = 30.0
    STALL_S = 300.0


def _worker_stdout():
    """sys.stdout, or (windowed frozen exe) a text stream on the inherited stdout pipe handle."""
    if sys.stdout is not None:
        return sys.stdout
    try:
        import ctypes
        import msvcrt
        get_std = ctypes.windll.kernel32.GetStdHandle
        get_std.restype = ctypes.c_void_p
        handle = get_std(-11)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        return open(msvcrt.open_osfhandle(handle, os.O_WRONLY), "w", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001
        return None


def steamworks_worker_main(arg_json: str) -> int:
    """Worker body (runs in its own process). Prints JSON lines {"progress","text"} and a final {"result": {...}}."""
    out = _worker_stdout()
    gone = []

    def emit(obj: dict) -> None:
        if out is not None and not gone:
            try:
                out.write("\n" + json.dumps(obj) + "\n")
                out.flush()
            except (OSError, ValueError):
                gone.append(True)        # the app went away: stop now instead of keeping 'RimWorld running' in Steam
                raise ConnectionAbortedError("the app closed")

    result = _sw_fail("")
    sw = None
    try:
        args = json.loads(arg_json)
        action, ids = args.get("action"), _clean_ids(args.get("ids"))
        deadline = time.monotonic() + max(5.0, float(args.get("timeout") or 600))
        if action not in _SW_ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        if action in ("subscribe", "unsubscribe", "state", "download") and not ids:
            raise ValueError("no workshop ids given")
        os.environ["SteamAppId"] = os.environ["SteamGameId"] = RIMWORLD_APPID
        emit({"progress": None, "text": "Connecting to Steam…"})
        sw = _Steamworks(args["dll"])
        err = sw.init()
        if err:
            result["error"] = err
        else:
            items: dict[str, dict] = {}
            if action == "list_subscribed":
                result["subscribed"] = sw.subscribed()
                items = {w: sw.info(w) for w in result["subscribed"]}
            elif action == "state":
                items = {w: sw.info(w) for w in ids}
            elif action == "download":
                items = sw.download(ids, deadline, emit, downloads=str(args.get("downloads") or ""))
            else:
                bad = sw.subscribe_all(ids, deadline, emit, unsubscribe=action == "unsubscribe")
                items = {w: {**sw.info(w), "ok": w not in bad, "message": bad.get(w, "OK")} for w in ids}
            bad = [w for w, d in items.items() if d.get("ok") is False]
            result.update(items=items, ok=not bad,
                          error=f"{len(bad)} of {len(items)} item(s) failed" if bad else "")
    except Exception as e:  # noqa: BLE001 - always report back
        result["ok"], result["error"] = False, f"{type(e).__name__}: {e}"
    finally:
        if sw is not None:
            try:
                sw.shutdown()
            except Exception:  # noqa: BLE001
                pass
    try:
        emit({"result": result})
    except OSError:
        pass
    return 0 if result["ok"] else 1


# ------------------------------------------------------------------ tests
def _selftest() -> None:
    import contextlib
    import io
    import tempfile

    opened, real_tab, real_sf = [], webbrowser.open_new_tab, getattr(os, "startfile", None)
    webbrowser.open_new_tab = opened.append
    try:
        def no_handler(_url):
            raise OSError("no steam:// handler")
        os.startfile = no_handler                                       # no handler: every page goes to the browser
        assert open_in_steam(["1", "2"]) is False and opened == [workshop_url("1"), workshop_url("2")]
        os.startfile, opened[:] = opened.append, []
        assert open_in_steam(["1", "2"]) is True and opened == ["steam://url/CommunityFilePage/1", workshop_url("2")]
    finally:
        webbrowser.open_new_tab = real_tab
        if real_sf:
            os.startfile = real_sf
        else:
            del os.startfile

    # browse page parsing: current React page (JSON.parse state + rendered links)
    state = {"queries": [{"state": {"data": {"results": [
        {"publishedfileid": "2009463077", "consumer_appid": 294100, "title": "Harmony", "subscriptions": 3308431,
         "short_description": "patch lib"},
        {"publishedfileid": "111", "consumer_appid": 730, "title": "Wrong game"}]}}}]}
    inner = json.dumps({"queryData": json.dumps(state)})
    new_page = ('<html><script>window.SSR.renderContext=JSON.parse(' + json.dumps(inner) + ');</script>'
                '<a href="https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077"><img alt="Harmony"/></a>'
                '<div><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077">Harmony</a></div>'
                '<div><a href="https://steamcommunity.com/id/brrainz/myworkshopfiles/?appid=294100">By Brrainz</a></div>'
                '<a href="https://steamcommunity.com/sharedfiles/filedetails/?id=999">Footer link</a></html>')
    r = _parse_browse_html(new_page)
    assert [x["workshop_id"] for x in r] == ["2009463077"], r
    assert r[0]["title"] == "Harmony" and r[0]["author"] == "Brrainz" and r[0]["subscriptions"] == 3308431, r
    old_page = ('<div class="workshopItem"><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=818773962&amp;searchtext=hug" '
                'class="ugc" data-publishedfileid="818773962"><img class="workshopItemPreviewImage" src="x.jpg"></a>'
                '<a href="https://steamcommunity.com/sharedfiles/filedetails/?id=818773962&searchtext=hug" class="item_link">'
                '<div class="workshopItemTitle ellipsis">HugsLib &amp; Co</div></a>'
                '<div class="workshopItemAuthorName ellipsis">by&nbsp;<a class="workshop_author_link" '
                'href="https://steamcommunity.com/id/unlimitedhugs/myworkshopfiles/?appid=294100">UnlimitedHugs</a></div></div>')
    r = _parse_browse_html(old_page)
    assert r == [{"workshop_id": "818773962", "title": "HugsLib & Co", "author": "UnlimitedHugs"}], r
    assert _parse_browse_html("") == [] and _parse_browse_html("<a href='x'>" * 50 + "JSON.parse(\"{bad") == []

    # collections: nested + cycle + dedupe, no recursion
    tree = {"1": [("10", 0), ("2", 2), ("11", 0)], "2": [("20", 0), ("1", 2), ("10", 0)], "3": []}
    calls = []

    def fake_fetch(ids):
        calls.append(list(ids))
        return {i: tree[i] for i in ids if i in tree}

    kids = _collection_tree(["1", "5"], fetch=fake_fetch)
    assert calls == [["1", "5"], ["2"]], calls
    assert _flatten(["5", "1", "10"], kids) == ["5", "10", "20", "11"], _flatten(["5", "1", "10"], kids)
    deep = {str(i): [str(i + 1)] for i in range(5000)}
    assert _flatten(["0"], deep) == ["5000"]

    # get_details: cache first, batches of 100, result!=1 omitted, all-batches-failed raises
    global _post_json
    real_post = _post_json
    sent: list[int] = []

    def fake_post(url, form):
        sent.append(form["itemcount"])
        ids_ = [form[f"publishedfileids[{n}]"] for n in range(form["itemcount"])]
        return {"response": {"publishedfiledetails": [
            {"publishedfileid": i, "result": 9 if i == "7" else 1, "title": f"t{i}"} for i in ids_]}}

    class FakeDb:
        stored: dict = {}

        def cached_details(self, ids, max_age_s):
            return {"5": {"publishedfileid": "5", "result": 1, "title": "cached"}} if "5" in ids else {}

        def cache_details(self, d):
            self.stored.update(d)

    try:
        _post_json = fake_post
        db = FakeDb()
        got = get_details([str(i) for i in range(1, 206)] + ["x", "5"], db=db)
        assert sent == [100, 100, 4] and got["5"]["title"] == "cached" and "7" not in got and len(got) == 204, sent
        assert "5" not in db.stored and len(db.stored) == 203
        assert get_details([]) == {}

        def down(url, form):
            raise OSError("offline")
        _post_json = down
        try:
            get_details(["1", "2"])
            raise AssertionError("expected OSError")
        except OSError:
            pass
        assert get_details(["5"], db=db)["5"]["title"] == "cached"            # fully cached: no network needed
        try:
            _collection_tree(["123"])
            raise AssertionError("expected OSError")      # core.import_text keeps the ids offline
        except OSError:
            pass
        from . import config
        real_get = config.http_get
        config.http_get = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
        try:
            assert search_workshop("anything") == [] and search_workshop("x", api_key="k") == []   # never raises
            assert search_workshop("x", page="bad") == []
        finally:
            config.http_get = real_get
    finally:
        _post_json = real_post

    # keyed IPublishedFileService helpers: an author's files page by page; only RimWorld items Steam serves
    global _pfs
    real_pfs, calls = _pfs, []

    def fake_pfs(method, key, params):
        calls.append((method, params.get("page"), params.get("requiredtags[0]")))
        if method == "GetUserFiles":
            n = {1: 100, 2: 100, 3: 37}[params["page"]]
            return {"total": 237, "publishedfiledetails": [{"publishedfileid": str(params["page"] * 1000 + i), "result": 1,
                                                            "consumer_appid": 294100} for i in range(n)]}
        return {"publishedfiledetails": [{"publishedfileid": "1", "result": 1, "consumer_appid": 294100},
                                         {"publishedfileid": "2", "result": 9}, {"publishedfileid": "3", "consumer_appid": 4000},
                                         {"publishedfileid": "4", "result": 1, "consumer_appid": 294100, "file_type": 2},
                                         {"publishedfileid": "x"}, "junk"]}
    _pfs = fake_pfs
    try:
        assert len(user_items("7", "k")) == 237 and [c[1] for c in calls] == [1, 2, 3], calls
        assert [d["publishedfileid"] for d in query_items("nice", "k", "1.6")] == ["1"] and calls[-1][2] == "1.6"
        assert list(item_details(["1", "2", "1", "bad"], "k")) == ["1"]
    finally:
        _pfs = real_pfs

    # steamcmd output parsing
    assert _parse_steamcmd_line('Success. Downloaded item 2009463077 to "C:\\x y\\steamapps\\workshop\\content\\294100\\2009463077" (5729526 bytes)') \
        == ("ok", "2009463077", "downloaded (5,729,526 bytes)", "C:\\x y\\steamapps\\workshop\\content\\294100\\2009463077")
    assert _parse_steamcmd_line("ERROR! Download item 123456 failed (Timeout).") == ("fail", "123456", "Timeout", "")
    assert _parse_steamcmd_line("ERROR! Timeout downloading item 777")[:2] == ("fail", "777")
    assert _parse_steamcmd_line("FAILED login with result code No Connection")[0] == "error"
    assert _parse_steamcmd_line("Loading Steam API...OK") is None
    assert _update_fraction("[ 45%] Downloading update (1,329 of 2,658 KB)...") == 0.5
    assert _update_fraction("[ 30%] Checking") == 0.3 and _update_fraction("[----] Verifying") is None
    assert not _retryable("File Not Found") and _retryable("Timeout")
    assert _parse_steamcmd_line("Failed to load cached hosts file (File 'x' not found), using defaults") is None
    assert _parse_steamcmd_line("Connecting anonymously to Steam Public...FAILED (No Connection)")[0] == "error"
    fatal = "[  0%] !!! Fatal Error: Steamcmd cannot run from a folder path with non-ASCII characters on this version"
    assert _parse_steamcmd_line(fatal)[0] == "error" and not _retryable(fatal)

    # subprocess streaming: \r and \n splitting, odd bytes, cancel kills the process
    py = sys.executable
    frozen = getattr(sys, "frozen", False)         # frozen: sys.executable is the app itself, not a Python
    if not frozen:
        lines: list[str] = []
        rc, status = _run_stream([py, "-c", "import sys; sys.stdout.buffer.write(b'a\\rb\\r\\nc\\xff\\nlast'); sys.exit(3)"],
                                 None, lines.append, timeout=60)
        assert (rc, status) == (3, "") and lines == ["a", "b", "c\ufffd", "last"], (rc, status, lines)
        ev = threading.Event()
        got: list[str] = []

        def on_line(t):
            got.append(t)
            ev.set()

        t0 = time.monotonic()
        rc, status = _run_stream([py, "-u", "-c", "import time; print('hi'); time.sleep(60)"], None, on_line, cancel=ev)
        assert status == "cancelled" and got == ["hi"] and time.monotonic() - t0 < 30, (status, got)
        rc, status = _run_stream([py, "-c", "import time; time.sleep(60)"], None, lambda t: None, idle_timeout=1)
        assert status == "idle", status
        box: list = []                              # kill_children (app exit) stops a running child, no cancel event
        th = threading.Thread(target=lambda: box.append(_run_stream([py, "-c", "import time; time.sleep(60)"],
                                                                    None, lambda t: None)))
        th.start()
        while not _CHILDREN and th.is_alive():
            time.sleep(0.05)
        kill_children()
        th.join(30)
        assert box and box[0][1] == "cancelled" and not _CHILDREN, box

    # SteamCMD bookkeeping (no network): paths, invalid ids never start steamcmd, non-ASCII folders refused
    sc = SteamCMD(os.path.join(tempfile.gettempdir(), "rmm-nonexistent-steamcmd"))
    assert sc.content_dir.endswith(os.path.join("workshop", "content", "294100")) and not sc.is_installed()
    assert SteamCMD(sc.root, "my_user\n+quit \"x\"").username == "my_userquitx"   # can't inject runscript lines
    assert _retryable("FAILED login (log in again: Settings)") is False
    assert sc.download(["abc", ""]) == {"abc": DownloadResult("abc", False, "invalid workshop id")}
    bad_sc = SteamCMD(os.path.join(tempfile.gettempdir(), "rmm-Kovács Péter", "steamcmd"))
    r = bad_sc.download(["123"])["123"]
    assert not r.ok and "non-ASCII" in r.message and not os.path.exists(bad_sc.root), r
    try:
        bad_sc.install()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "Settings" in str(e)

    # install_items: move / copy / link, replacing existing, strict deletion rules
    tmp = tempfile.mkdtemp(prefix="rmm_steam_")
    try:
        log = os.path.join(tmp, "console_log.txt")
        with open(log, "wb") as f:
            f.write(b"[2026-01-01 00:00:00] old line\n")
        tail = _Tail(log)
        assert tail.read() == []
        with open(log, "ab") as f:
            f.write(b"[2026-01-01 00:00:01] Success. Downloaded item 5 to \"x\"\n[2026-01-01 00:00:02] part")
        assert tail.read() == ['Success. Downloaded item 5 to "x"']
        with open(log, "ab") as f:
            f.write(b"ial\r\n")
        assert tail.read() == ["partial"]
        with open(log, "wb") as f:
            f.write(b"new\n")                                                  # rotated
        assert tail.read() == ["new"] and _Tail(os.path.join(tmp, "missing.txt")).read() == []

        content, mods = os.path.join(tmp, "content"), os.path.join(tmp, "Mods")
        for wid in ("101", "102", "103"):
            os.makedirs(os.path.join(content, wid, "About"))
            with open(os.path.join(content, wid, "About", "About.xml"), "w") as f:
                f.write(f"<ModMetaData><packageId>t.{wid}</packageId></ModMetaData>")
        os.makedirs(os.path.join(mods, "101", "Old"))                      # stale copy gets replaced
        os.makedirs(os.path.join(mods, "MyLocalMod"))                      # must never be touched
        res = {w: DownloadResult(w, True, "downloaded", os.path.join(content, w)) for w in ("101", "102", "103")}
        res["104"] = DownloadResult("104", False, "Timeout")
        moved = install_items({"101": res["101"], "104": res["104"]}, mods, "move")
        assert moved["101"].ok and moved["101"].path == os.path.join(mods, "101") and moved["104"] is res["104"]
        assert os.path.isfile(os.path.join(mods, "101", "About", "About.xml")) and not os.path.exists(os.path.join(mods, "101", "Old"))
        assert not os.path.exists(os.path.join(content, "101"))
        copied = install_items({"102": res["102"]}, mods, "copy")
        assert copied["102"].ok and os.path.isdir(os.path.join(content, "102", "About")) and os.path.isdir(os.path.join(mods, "102", "About"))
        linked = install_items({"103": res["103"]}, mods, "link")
        assert linked["103"].ok and _is_link(os.path.join(mods, "103")), linked
        assert os.path.isfile(os.path.join(mods, "103", "About", "About.xml"))
        again = install_items({"103": res["103"]}, mods, "link")                # already linked → no-op
        assert again["103"].ok and _is_link(os.path.join(mods, "103"))
        # replacing a junction must delete only the link, never the target
        recopy = install_items({"103": res["103"]}, mods, "copy")
        assert recopy["103"].ok and not _is_link(os.path.join(mods, "103"))
        assert os.path.isfile(os.path.join(content, "103", "About", "About.xml"))
        bad = install_items({"x": DownloadResult("../evil", True, "", os.path.join(content, "102"))}, mods)
        assert not bad["x"].ok
        try:
            _install_one(os.path.join(content, "102"), os.path.join(tmp, "MyLocalMod"), mods, "copy")
            raise AssertionError("expected refusal")
        except RuntimeError:
            pass
        assert os.path.isdir(os.path.join(mods, "MyLocalMod"))
        assert sorted(os.listdir(mods)) == ["101", "102", "103", "MyLocalMod"], os.listdir(mods)
        try:
            install_items({}, mods, "teleport")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
        # targets: updating a mod whose folder is not named by its id replaces that folder (no duplicate)
        os.makedirs(os.path.join(content, "105", "About"))
        with open(os.path.join(content, "105", "About", "About.xml"), "w") as f:
            f.write("<ModMetaData><packageId>t.105</packageId></ModMetaData>")
        r105 = DownloadResult("105", True, "downloaded", os.path.join(content, "105"))
        up = install_items({"105": r105}, mods, "copy", targets={"105": os.path.join(mods, "MyLocalMod")})
        assert up["105"].ok and up["105"].path == os.path.join(mods, "MyLocalMod"), up
        with open(os.path.join(mods, "MyLocalMod", "About", "About.xml")) as f:
            assert "t.105" in f.read()
        outside = install_items({"105": r105}, mods, "copy", targets={"105": os.path.join(content, "101x")})
        assert not outside["105"].ok and "refusing" in outside["105"].message, outside
        # a failed copy leaves the installed version alone and no temporary folder behind
        real_copytree = shutil.copytree

        def boom(s, d, *a, **k):
            assert os.path.dirname(d) == stg, d                                 # staged outside Mods (game never sees it)
            os.makedirs(os.path.join(d, "partial"))
            raise OSError("disk full")
        stg = _staging_root(mods)
        assert _norm(os.path.dirname(stg)) == _norm(tmp) and not os.path.exists(stg)   # removed after each install
        shutil.copytree = boom
        try:
            failed = install_items({"102": res["102"]}, mods, "copy")
        finally:
            shutil.copytree = real_copytree
        assert not failed["102"].ok and os.path.isfile(os.path.join(mods, "102", "About", "About.xml"))
        assert sorted(os.listdir(mods)) == ["101", "102", "103", "MyLocalMod"] and not os.path.exists(stg), os.listdir(mods)
        # leftovers of a killed install (staging folder, and legacy ones inside Mods): a lone old copy is restored,
        # the rest removed (junction: only the link)
        os.makedirs(stg)
        os.rename(os.path.join(mods, "101"), os.path.join(stg, "101.rmm_old_5"))
        os.makedirs(os.path.join(stg, "101.rmm_new_6", "Textures"))            # half-copied new version
        os.makedirs(os.path.join(mods, "102.rmm_old_7"))                        # legacy: swapped, old copy not deleted yet
        _make_junction(os.path.join(content, "103"), os.path.join(stg, "103.rmm_new_8"))
        msgs = cleanup_stale_installs(mods)
        assert len(msgs) == 4 and sorted(os.listdir(mods)) == ["101", "102", "103", "MyLocalMod"], (msgs, os.listdir(mods))
        assert not os.path.exists(stg)
        assert os.path.isfile(os.path.join(mods, "101", "About", "About.xml"))
        assert os.path.isfile(os.path.join(content, "103", "About", "About.xml"))
        assert cleanup_stale_installs(mods) == [] and cleanup_stale_installs(os.path.join(tmp, "nope")) == []

        # SteamCMD workshop state is reset only after a run that did not end cleanly (killed / app closed)
        sc2 = SteamCMD(os.path.join(tmp, "sc"))
        dl, flag = os.path.join(sc2.root, "steamapps", "workshop", "downloads"), os.path.join(sc2.root, sc2.DIRTY_FLAG)
        os.makedirs(os.path.join(dl, "294100"))
        sc2._reset_workshop_state()
        assert os.path.isdir(dl)                                                # partial downloads kept for resume
        open(flag, "w").close()
        sc2._reset_workshop_state()
        assert not os.path.exists(dl) and not os.path.exists(flag)
        # an acf listing an installed item whose content folder was moved out is dropped (else 'Missing game files')
        acf = os.path.join(sc2.root, "steamapps", "workshop", "appworkshop_294100.acf")
        body = '"AppWorkshop"\n{\n\t"appid"\t\t"294100"\n\t"WorkshopItemsInstalled"\n\t{\n\t\t"1"\n\t\t{\n\t\t}\n\t}\n}\n'
        with open(acf, "w") as f:
            f.write(body)
        sc2._reset_workshop_state()
        assert not os.path.exists(acf)
        os.makedirs(os.path.join(sc2.content_dir, "1"))
        with open(acf, "w") as f:
            f.write(body)
        sc2._reset_workshop_state()
        assert os.path.exists(acf)
        # a transient sharing violation on the swap rename is retried
        real_rename, calls = os.rename, []

        def flaky(a, b):
            calls.append(a)
            if len(calls) == 1:
                raise PermissionError(32, "in use")
            real_rename(a, b)
        os.rename = flaky
        try:
            _rename_retry(os.path.join(sc2.content_dir, "1"), os.path.join(sc2.content_dir, "2"))
        finally:
            os.rename = real_rename
        assert len(calls) == 2 and os.path.isdir(os.path.join(sc2.content_dir, "2"))
        # a batch that raises keeps the earlier batches' results; a lock held elsewhere doesn't block cancel
        sc2.path_problem, sc2.is_installed, sc2._updated = lambda: "", lambda: True, lambda: True

        def one_batch(chunk, wanted, results, progress, cancel):
            if chunk == ["8"]:
                raise OSError("steamcmd.exe is locked")
            results[chunk[0]] = DownloadResult(chunk[0], True, "ok", "x")
        sc2._run_batch = one_batch
        got = sc2.download(["7", "8"], retries=0, batch=1)
        assert got["7"].ok and not got["8"].ok and "locked" in got["8"].message, got
        # a dropped connection is retried right after its batch (after a pause), before the next batch
        runs = []

        def flaky_batch(chunk, wanted, results, progress, cancel):
            runs.append(list(chunk))
            for w in chunk:
                results[w] = DownloadResult(w, len(runs) > 1 or w == "1", "ok" if len(runs) > 1 or w == "1" else "No Connection")
        sc2._run_batch, sc2.FLAKY_PAUSE_S = flaky_batch, 0.01
        got = sc2.download(["1", "2", "3", "4"], batch=3)
        assert runs == [["1", "2", "3"], ["2", "3"], ["4"]] and all(r.ok for r in got.values()), (runs, got)
        del sc2.FLAKY_PAUSE_S
        sc2._run_batch = one_batch
        stop = threading.Event()
        with _STEAMCMD_LOCK:
            threading.Timer(0.3, stop.set).start()
            got = sc2.download(["7"], cancel=stop)
        assert got["7"].message == "cancelled", got
        for a in ("path_problem", "is_installed", "_updated", "_run_batch"):
            delattr(sc2, a)
        if not frozen:
            sc2.exe, got = py, {}
            sc2._run_batch(["5"], ["5"], got, noop_progress, None)             # 'python +runscript …' just exits
            assert not got["5"].ok and not os.path.exists(flag), got
            stop = threading.Event()
            stop.set()
            sc2._run_batch(["5"], ["5"], got, noop_progress, stop)
            assert got["5"].message == "cancelled" and os.path.exists(flag), got
            # killed mid-item: SteamCMD's dying 'failed (Failure)' line must not replace 'cancelled'
            sc2._run = lambda a, on_line, c, idle_timeout: (on_line("ERROR! Download item 5 failed (Failure)."),
                                                            (1, "cancelled"))[1]
            sc2._run_batch(["5"], ["5"], got, noop_progress, stop)
            assert got["5"].message == "cancelled", got
            # a clean run: a 'Failure' leaves the flag (its partial state is wiped before the retry), a timeout not
            for line, kept in (("ERROR! Download item 5 failed (Failure).", True),
                               ("ERROR! Download item 5 failed (Timeout).", False)):
                if os.path.exists(flag):
                    os.remove(flag)
                sc2._run = lambda a, on_line, c, idle_timeout, ln=line: (on_line(ln), (0, ""))[1]
                sc2._run_batch(["5"], ["5"], {}, noop_progress, None)
                assert os.path.exists(flag) == kept, line
            # a missing/expired cached login falls back to an anonymous run of the same batch
            logins = []

            def fake_login(a, on_line, c, idle_timeout):
                with open(os.path.join(sc2.root, a[1]), encoding="utf-8") as f:
                    login = next(ln for ln in f.read().splitlines() if ln.startswith("login "))
                logins.append(login)
                if login != "login anonymous":
                    on_line("FAILED (No cached credentials and @NoPromptForPassword is set)")
                    on_line("ERROR! Not logged on.")
                else:
                    os.makedirs(os.path.join(sc2.content_dir, "5"), exist_ok=True)
                    on_line(f'Success. Downloaded item 5 to "{os.path.join(sc2.content_dir, "5")}" (1 bytes)')
                return 0, ""
            sc2._run, sc2.username, got = fake_login, "someone", {}
            sc2._run_batch(["5"], ["5"], got, noop_progress, None)
            assert logins == ["login someone", "login anonymous"] and got["5"].ok, (logins, got)
            # a good cached login must not count as a failure (it would download everything twice)
            assert not _RE_LOGIN_FAIL.search("Logging in using cached credentials.")
            assert _RE_LOGIN_FAIL.search("Logging in user 'x' [U:1:0] to Steam Public...FAILED (Invalid Password)")
            del sc2._run
            # a download that logs nothing but grows its folder is not 'idle'
            sc2.DL_POLL_S = 0.1
            writer = (f"import os, time\nos.makedirs({dl!r}, exist_ok=True)\n"
                      "for i in range(8):\n    open(os.path.join(" + repr(dl) + ", 'x'), 'a').write('x' * 999); time.sleep(0.2)\n")
            rc, status = sc2._run(["-c", writer], lambda t: None, None, idle_timeout=1.2)
            assert (rc, status) == (0, ""), (rc, status)

        # Steamworks: 'installed' only counts when the folder is really there
        fake = object.__new__(_Steamworks)
        fake.ugc, fake.DownloadItem, fake.RunCallbacks, fake.GetItemState = None, None, lambda: None, lambda u, w: 5
        where = {"102": os.path.join(mods, "102"), "9": os.path.join(tmp, "gone")}
        fake.info = lambda w: {"state": 5, "path": where[w]}
        fake.MISSING_GRACE_S = 0.0
        got = fake.download(["102", "9"], time.monotonic() + 10, lambda o: None)
        assert got["102"]["ok"] and not got["9"]["ok"] and "folder is missing" in got["9"]["message"], got
        fake.MISSING_GRACE_S = 60.0
        assert "folder is missing" in fake.download(["9"], time.monotonic() + 0.3, lambda o: None)["9"]["message"]

        # Steamworks bulk download: no forced high-priority DownloadItem; a mod Steam never starts is nudged once
        # at normal priority, and STALL_S without any change ends the wait instead of a fixed deadline
        calls, polls, lines = [], [0], []
        sw2 = object.__new__(_Steamworks)
        sw2.ugc, sw2.RunCallbacks, sw2.GetItemState = None, lambda: None, lambda u, w: 1   # subscribed already
        sw2.DownloadItem = lambda u, w, high: calls.append((w, high)) or True

        def info2(w):
            polls[0] += 1
            return {"state": 5, "path": where["102"]} if w == "102" and polls[0] > 4 else {"state": 1, "path": ""}
        sw2.info, sw2.NUDGE_S, sw2.STALL_S = info2, 0.0, 0.6
        got = sw2.download(["102", "7"], time.monotonic() + 30, lines.append)
        assert got["102"]["ok"] and not got["7"]["ok"] and "keeps downloading" in got["7"]["message"], got
        assert calls and all(high is False for _, high in calls) and (7, False) in calls, calls
        assert any(o.get("text") == "Success: Steam installed 102" for o in lines), lines

        # subscribe_all: at most SUB_INFLIGHT calls in flight, a rate-limited one is retried, a denied one reported
        import ctypes
        sw3 = object.__new__(_Steamworks)
        sw3.C, sw3.ugc, sw3.utils, sw3.RunCallbacks = ctypes, None, None, lambda: None
        sw3.SUB_INFLIGHT, sw3.RETRY_S = 2, 0.0
        handles, live, peak, answers = {}, set(), [0], {"3": [84, 1], "4": [15]}

        def sub3(u, wid):
            h = len(handles) + 1
            handles[h] = str(wid)
            live.add(h)
            peak[0] = max(peak[0], len(live))
            return h

        def result3(h, cb, io_failed):
            live.discard(h)
            er = (answers.get(handles[h]) or [1]).pop(0)
            return er, _ERESULT.get(er, "?")
        sw3.SubscribeItem, sw3.IsAPICallCompleted, sw3._call_result = sub3, lambda u, h, f: True, result3
        bad = sw3.subscribe_all(["1", "2", "3", "4", "5"], time.monotonic() + 10, lambda o: None)
        assert bad == {"4": "Access denied"} and peak[0] <= 2 and list(handles.values()).count("3") == 2, \
            (bad, peak, handles)
        # ...and calls Steam never answers end after STALL_S, not at the (up to 100 min) download deadline
        sw3.IsAPICallCompleted, sw3.STALL_S, t0 = (lambda u, h, f: False), 0.3, time.monotonic()
        assert sw3.subscribe_all(["8"], t0 + 60, lambda o: None) == {"8": "timed out waiting for Steam"}
        assert time.monotonic() - t0 < 5
        # a mod that Steam finishes during the last sleep counts as installed, not as missing / unfinished
        late = [0]

        def info_late(w):
            late[0] += 1
            return {"state": 5, "path": where["102"]} if late[0] > 1 else {"state": 1, "path": ""}
        sw2.info = info_late
        assert sw2.download(["102"], time.monotonic() + 0.2, lambda o: None)["102"]["ok"]

        # Steamworks: graceful failures without touching Steam
        assert steamworks_available(None) is False and steamworks_available(tmp) is False
        r = steamworks_call("state", ["2009463077"], tmp)
        assert r["ok"] is False and "steam_api64.dll" in r["error"] and r["items"] == {} and r["subscribed"] == []
        assert steamworks_call("explode", [], tmp)["ok"] is False
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = steamworks_worker_main(json.dumps({"action": "state", "ids": ["1"], "dll": os.path.join(tmp, "nope.dll")}))
        res_lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
        assert code == 1 and res_lines[-1]["result"]["ok"] is False and res_lines[-1]["result"]["error"], res_lines
        if not frozen and os.name == "nt":          # cancel kills the helper (a stand-in that never calls Steam)
            global _WORKER_CODE
            from . import paths
            real_code, real_dll = _WORKER_CODE, paths.steam_api_dll
            _WORKER_CODE = "import time; print('{\"text\": \"fake helper\"}', flush=True); time.sleep(60)"
            paths.steam_api_dll = lambda gd: os.path.join(tmp, "steam_api64.dll")
            stop = threading.Event()
            threading.Timer(0.5, stop.set).start()
            t0 = time.monotonic()
            try:
                r = steamworks_call("download", ["123"], tmp, cancel=stop)
            finally:
                _WORKER_CODE, paths.steam_api_dll = real_code, real_dll
            assert r["ok"] is False and r["error"] == "cancelled" and time.monotonic() - t0 < 20, r
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_online() -> None:
    d = get_details(["2009463077", "1", "2009463077"])
    assert set(d) == {"2009463077"} and d["2009463077"]["title"] == "Harmony", d.keys()
    assert d["2009463077"]["consumer_app_id"] == 294100
    hits = search_workshop("harmony")
    assert any(h["workshop_id"] == "2009463077" for h in hits), hits[:5]
    assert "2009463077" not in _collection_tree(["2009463077"])           # not a collection
    from . import config
    raw = config.http_get(_BROWSE_URL + "?appid=294100&section=collections&browsesort=trend&days=-1",
                          timeout=30).decode("utf-8", "replace")
    colls = [c["workshop_id"] for c in _parse_browse_html(raw)]
    assert colls, "no collections found on the browse page"
    tree = _collection_tree(colls[:5])
    coll = next((c for c in colls[:5] if tree.get(c)), "")
    children = _flatten([coll], tree)
    assert children and all(c.isdigit() for c in children), (coll, children)
    expanded = _flatten([coll, "2009463077"], tree)
    assert "2009463077" in expanded and coll not in expanded and set(children) <= set(expanded)


if __name__ == "__main__":
    _selftest()
    print("steam selftest ok")
