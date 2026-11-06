"""App dir, settings, logging, DPAPI secrets, HTTP helpers, small OS helpers."""
from __future__ import annotations

import base64
import copy
import functools
import json
import logging
import logging.handlers
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from .models import APP_ID, APP_VERSION, USER_AGENT, Progress, noop_progress

NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- app dir
def _base_dir() -> str:
    """Folder of the exe when frozen, else the project root (where main.py lives)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_app_dir() -> str:
    base = _base_dir()
    ov = os.environ.get("RMM_APP_DIR")   # set by main.run_selftests so tests never touch the real app dir
    candidates = [ov] if ov else []
    if os.path.isfile(os.path.join(base, "portable.txt")):
        candidates.append(os.path.join(base, "data"))
    candidates += [os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_ID),
                   os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_ID),
                   os.path.join(tempfile.gettempdir(), APP_ID)]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return d
        except OSError:
            continue
    return candidates[-1]


APP_DIR: str = _resolve_app_dir()


def app_path(*parts: str) -> str:
    """Path under APP_DIR; creates the parent directories of the result."""
    p = os.path.join(APP_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _writable_dir(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, f".rmm_probe_{os.getpid()}")
        with open(probe, "wb"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


@functools.cache
def default_steamcmd_dir() -> str:
    """app_path('steamcmd'), unless that path has non-ASCII characters (e.g. C:\\Users\\Kovács): SteamCMD refuses
    to run there, so use the first writable ASCII folder of %PUBLIC%, %SystemDrive%\\ and %ProgramData%."""
    d = app_path("steamcmd")
    if d.isascii():
        return d
    for root in (os.environ.get("PUBLIC"), (os.environ.get("SystemDrive") or "C:") + os.sep, os.environ.get("ProgramData")):
        c = os.path.join(root, APP_ID, "steamcmd") if root else ""
        if c.isascii() and _writable_dir(c):
            return c
    return d


def atomic_write(path: str, data: bytes) -> None:
    """Write bytes via tmp file + os.replace (retries briefly when AV/indexers hold the target)."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if os.path.exists(path) and not os.access(path, os.W_OK):
                    # Read-only target (users lock ModsConfig.xml so the game cannot reset it): Windows never
                    # replaces it, so clear the flag, replace, and put it back on the new file.
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                    try:
                        os.replace(tmp, path)
                    finally:
                        os.chmod(path, stat.S_IREAD)
                    return
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------- logging
_logger_lock = threading.Lock()


def get_logger() -> logging.Logger:
    log = logging.getLogger("rmm")
    with _logger_lock:
        if getattr(log, "_rmm_configured", False):
            return log
        log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
        try:
            fh = logging.handlers.RotatingFileHandler(app_path("logs", "app.log"), maxBytes=1_000_000,
                                                      backupCount=3, encoding="utf-8", delay=True)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass
        if not getattr(sys, "frozen", False) and sys.stderr is not None:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            sh.setLevel(logging.WARNING)
            log.addHandler(sh)
        log.propagate = False
        log._rmm_configured = True  # type: ignore[attr-defined]
    return log


# ---------------------------------------------------------------- settings
DEFAULTS: dict = {
    "game_dir": "",
    "config_dir": "",
    "workshop_dir": "",
    "extra_mod_dirs": [],
    "steamcmd_dir": "",                # '' -> default_steamcmd_dir() (resolved by Settings.get)
    "download_method": "steamcmd",     # steamcmd | steamworks | steam_client | browser
    "install_mode": "move",            # move | copy | link
    "steamcmd_retries": 3,
    "steamcmd_fallback_steamworks": True,   # SteamCMD failures are subscribed through Steam when it runs
    "steamcmd_user": "",               # '' = anonymous; else an account that logged in once via SteamCMD's console
    "ai_provider": "antigravity",      # antigravity | anthropic | openai_compat | claude_code | offline
    "ai_preset": "Antigravity (your subscription)",
    "ai_model": "gemini-4-argon-high",
    "ai_base_url": "",
    "ai_effort": "high",
    "ai_live_verify": True,
    "ai_auto_count": True,             # let the AI decide how many mods the request needs
    "ai_no_lazy": True,                # instruct the AI not to be lazy: try its best and deliver fully
    "ai_chat_persona": "efficient",    # efficient (free default) | chaotic | warcrime | cassandra | phoebe | archotech | brainrot | soviet | eighties | iron_commander
    "ai_tts_enabled": True,            # read Randy's Idea Lab replies and AI modlist summaries out loud via Windows TTS
    "sort_tiebreak": "current",        # current | alphabetical
    "theme": "dark",
    "db_auto_update_days": 3,
    "check_updates_on_start": False,   # quiet Steam update check after the startup scan
    "launch_via_steam": True,
    "launch_args": "",
    "backup_keep": 30,
    "window_geometry": "",
    "last_tab": 0,
    "browser_batch": 10,
}


def _compatible(default, value) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


class Settings:
    def __init__(self, path: str | None = None):
        self.path = path or app_path("settings.json")
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()   # serialise+write as one step so saves cannot land out of order
        self.data: dict = copy.deepcopy(DEFAULTS)
        self._load_failed = False   # file exists but could not be read: never overwrite it with defaults
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        for attempt in range(5):   # AV/OneDrive/a second instance may hold the file briefly
            try:
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    text = f.read()
                break
            except FileNotFoundError:
                return
            except OSError as e:
                if isinstance(e, PermissionError) and attempt < 4:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                self._load_failed = True
                get_logger().warning("Settings file could not be read (%s); using defaults and not saving over it", e)
                return
        try:
            loaded = json.loads(text)
            if not isinstance(loaded, dict):
                raise ValueError("settings root is not an object")
        except ValueError as e:
            bak = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(self.path, bak)
            except OSError:
                bak = "(could not back up)"
            get_logger().warning("Settings file unreadable (%s); using defaults, old file -> %s", e, bak)
            return
        for k, v in loaded.items():
            if k in DEFAULTS and not _compatible(DEFAULTS[k], v):
                get_logger().warning("Ignoring setting %s=%r (wrong type)", k, v)
                continue
            self.data[k] = v
        if self.data.get("ai_provider") == "claude_code" or self.data.get("ai_preset") == "Claude Code (your subscription)":
            self.data["ai_provider"] = "antigravity"
            self.data["ai_preset"] = "Antigravity (your subscription)"
        if self.data.get("ai_provider") == "antigravity" and self.data.get("ai_model") in (
            "", "pro", "fable", "opus", "sonnet", "haiku"
        ):
            self.data["ai_model"] = "gemini-4-argon-high"

    def get(self, key, default=None):
        with self._lock:
            v = self.data.get(key, default)
        if key == "steamcmd_dir" and not v:
            return default_steamcmd_dir()
        return copy.copy(v) if isinstance(v, (list, dict)) else v

    def set(self, key, value) -> None:
        with self._lock:
            self.data[key] = value

    def save(self) -> None:
        # Not under self._lock: get() on the Tk thread must not wait for atomic_write's retries.
        if self._load_failed:
            raise OSError(f"{self.path} could not be read at startup; not overwriting it (restart the app)")
        with self._save_lock:
            with self._lock:
                payload = json.dumps(self.data, indent=2, ensure_ascii=False)
            atomic_write(self.path, payload.encode("utf-8"))

    def get_secret(self, key) -> str:
        with self._lock:
            v = self.data.get(key)
        if not v or not isinstance(v, str):
            return ""
        if v.startswith("plain:"):
            return v[6:]
        try:
            return _dpapi(base64.b64decode(v), protect=False).decode("utf-8")
        except Exception as e:  # noqa: BLE001 - wrong user/machine, corrupt data, no DPAPI
            get_logger().warning("Could not decrypt secret %s: %s", key, e)
            return ""

    def set_secret(self, key, value: str) -> None:
        if not value:
            with self._lock:
                self.data.pop(key, None)
            return
        try:
            stored = base64.b64encode(_dpapi(value.encode("utf-8"), protect=True)).decode("ascii")
        except Exception as e:  # noqa: BLE001
            get_logger().warning("DPAPI unavailable (%s); storing %s unencrypted", e, key)
            stored = "plain:" + value
        self.set(key, stored)


# ---------------------------------------------------------------- DPAPI (Windows, ctypes)
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not fn(ctypes.byref(blob_in), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
        raise OSError(ctypes.get_last_error(), "DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


# ---------------------------------------------------------------- HTTP
# The network stack (urllib.request/http.client/ssl, ~30 ms and the SSL DLLs) is imported on first use, not at startup.
_ssl_ctx = None


def _context():
    import ssl
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()   # uses the Windows cert store; certifi not needed
    return _ssl_ctx


def _request(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None):
    import urllib.request
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _open(req, timeout: float, retries: int = 1):
    """urlopen with one retry on transient failures (5xx/429/connection reset). Timeouts are not retried."""
    import http.client
    import ssl
    import urllib.error
    import urllib.request
    for attempt in range(retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=_context())
        except urllib.error.HTTPError as e:
            if attempt >= retries or not (e.code == 429 or e.code >= 500):
                raise
            e.close()
        except (TimeoutError, ssl.SSLCertVerificationError):
            raise
        except urllib.error.URLError as e:
            if attempt >= retries or isinstance(e.reason, (TimeoutError, ssl.SSLCertVerificationError)):
                raise
        except OSError:
            if attempt >= retries:
                raise
        except http.client.HTTPException as e:       # e.g. BadStatusLine: not an OSError, normalise it
            if attempt >= retries:
                raise ConnectionError(f"HTTP protocol error: {e!r}") from e
        time.sleep(1.0 + attempt)
    raise OSError("unreachable")


def _fetch(req, timeout: float) -> bytes:
    import http.client
    try:
        with _open(req, timeout) as r:
            return r.read()
    except http.client.HTTPException as e:           # IncompleteRead etc. -> OSError, as promised to callers
        raise ConnectionError(f"HTTP protocol error: {e!r}") from e


def http_get(url, timeout=30, headers=None) -> bytes:
    return _fetch(_request(url, headers=headers), timeout)


def http_post_form(url, data: dict, timeout=30) -> bytes:
    body = urllib.parse.urlencode(data).encode("utf-8")
    return _fetch(_request(url, body, {"Content-Type": "application/x-www-form-urlencoded"}, "POST"), timeout)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _fmt_mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def download_file(url, dest, progress: Progress = noop_progress, timeout=60,
                  cancel: threading.Event | None = None) -> str:
    """Stream url to dest+'.part', then os.replace to dest. Raises InterruptedError when cancelled."""
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    label = os.path.basename(dest)
    import http.client
    try:
        with _open(_request(url), timeout) as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, 0.0
            progress(0.0 if total else None, f"Downloading {label}")
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("Download cancelled")
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last >= 0.15:
                    last = now
                    if total:
                        progress(min(done / total, 1.0), f"Downloading {label}: {_fmt_mb(done)} / {_fmt_mb(total)}")
                    else:
                        progress(None, f"Downloading {label}: {_fmt_mb(done)}")
        if total and done < total:
            raise OSError(f"Download truncated: got {done} of {total} bytes")
        os.replace(part, dest)
    except http.client.HTTPException as e:
        _remove_quietly(part)
        raise ConnectionError(f"HTTP protocol error: {e!r}") from e
    except BaseException:
        _remove_quietly(part)
        raise
    progress(1.0, f"Downloaded {label} ({_fmt_mb(done)})")
    return dest


# ---------------------------------------------------------------- OS helpers
def open_path(path) -> None:
    """Open a file, folder or URL (http, steam://) with the default handler."""
    path = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, path])
    except OSError:
        if "://" in path:
            import webbrowser
            webbrowser.open(path)
        else:
            raise


def is_process_running(exe_name: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH", "/FO", "CSV"],
                             capture_output=True, timeout=15, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f'"{exe_name.lower()}"' in out.decode("utf-8", "replace").lower()


# ---------------------------------------------------------------- tests
def _selftest() -> None:
    global APP_DIR
    import shutil
    tmp = tempfile.mkdtemp(prefix="rmm_cfg_")
    logging.disable(logging.WARNING)   # the corrupt-file cases below warn on purpose
    try:
        p = os.path.join(tmp, "settings.json")
        s = Settings(p)
        assert s.get("download_method") == "steamcmd" and s.get("extra_mod_dirs") == []
        assert s.get("steamcmd_dir").endswith("steamcmd") and s.get("nope", 5) == 5
        s.set("theme", "light")
        s.set_secret("ai_api_key", "sk-test-ü")
        assert s.data["ai_api_key"] != "sk-test-ü"
        s.save()
        s2 = Settings(p)
        assert s2.get("theme") == "light" and s2.get_secret("ai_api_key") == "sk-test-ü"
        assert s2.get_secret("missing") == ""
        s2.set("ai_api_key", "garbage!!")
        assert s2.get_secret("ai_api_key") == ""
        s2.set_secret("x", "")
        assert "x" not in s2.data
        # wrong types are ignored, corrupt files are backed up
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"backup_keep": "lots", "theme": "light", "ai_live_verify": 1}, f)
        s3 = Settings(p)
        assert s3.get("backup_keep") == 30 and s3.get("theme") == "light" and s3.get("ai_live_verify") is True
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        s4 = Settings(p)
        assert s4.get("theme") == "dark" and not os.path.exists(p)
        assert any(n.startswith("settings.json.corrupt-") for n in os.listdir(tmp))
        # a file that cannot be opened (AV lock) is neither renamed nor overwritten by save()
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"theme": "light"}, f)
        import builtins                  # no unittest.mock: the exe build excludes unittest
        real_open, real_sleep = builtins.open, time.sleep

        def locked(f, *a, **k):
            if f == p:
                raise PermissionError(13, "locked")
            return real_open(f, *a, **k)
        builtins.open, time.sleep = locked, lambda s: None
        try:
            s5 = Settings(p)
        finally:
            builtins.open, time.sleep = real_open, real_sleep
        assert s5.get("theme") == "dark" and s5._load_failed
        try:
            s5.save()
            raise AssertionError("save over an unread file")
        except OSError:
            pass
        assert Settings(p).get("theme") == "light"
        assert sum(n.startswith("settings.json.corrupt-") for n in os.listdir(tmp)) == 1
        # a read-only target (locked ModsConfig.xml) is written and stays read-only
        ro = os.path.join(tmp, "ModsConfig.xml")
        atomic_write(ro, b"old")
        os.chmod(ro, stat.S_IREAD)
        atomic_write(ro, b"new")
        with open(ro, "rb") as f:
            assert f.read() == b"new"
        assert not os.access(ro, os.W_OK)
        os.chmod(ro, stat.S_IWRITE | stat.S_IREAD)
        assert app_path("x", "y.txt").startswith(APP_DIR) and os.path.isdir(os.path.join(APP_DIR, "x"))
        os.rmdir(os.path.join(APP_DIR, "x"))
        # SteamCMD cannot run from a non-ASCII folder: the default moves to an ASCII one
        if tmp.isascii():
            old_dir, old_pub = APP_DIR, os.environ.get("PUBLIC")
            APP_DIR, os.environ["PUBLIC"] = os.path.join(tmp, "Kovács Péter"), os.path.join(tmp, "pub")
            try:
                d = default_steamcmd_dir.__wrapped__()
                assert d == os.path.join(tmp, "pub", APP_ID, "steamcmd") and os.path.isdir(d), d
            finally:
                APP_DIR = old_dir
                if old_pub is None:
                    os.environ.pop("PUBLIC", None)
                else:
                    os.environ["PUBLIC"] = old_pub
        assert default_steamcmd_dir().isascii() or not app_path("steamcmd").isascii()
        assert is_process_running("definitely-not-running-rmm.exe") is False
        if sys.platform == "win32":
            assert is_process_running("explorer.exe") or is_process_running("svchost.exe")
        get_logger().info("config selftest ok")
    finally:
        logging.disable(logging.NOTSET)
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_online() -> None:
    import shutil
    body = http_post_form("https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
                          {"itemcount": 1, "publishedfileids[0]": "2009463077"})
    d = json.loads(body)["response"]["publishedfiledetails"][0]
    assert d["result"] == 1 and d["consumer_app_id"] == 294100, d
    tmp = tempfile.mkdtemp(prefix="rmm_dl_")
    try:
        seen = []
        out = download_file("https://raw.githubusercontent.com/emipa606/NoVersionWarning/main/1.5/ModIdsToFix.xml",
                            os.path.join(tmp, "a", "ids.xml"), progress=lambda f, t: seen.append(f))
        assert os.path.getsize(out) > 100 and not os.path.exists(out + ".part") and seen
        ev = threading.Event()
        ev.set()
        try:
            download_file("https://raw.githubusercontent.com/emipa606/NoVersionWarning/main/1.5/ModIdsToFix.xml",
                          os.path.join(tmp, "b.xml"), cancel=ev)
            raise AssertionError("expected cancel")
        except InterruptedError:
            assert not os.path.exists(os.path.join(tmp, "b.xml.part"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert http_get("https://steamcommunity.com/", timeout=30)[:1]


if __name__ == "__main__":
    _selftest()
    print("config ok", APP_DIR, APP_VERSION)
