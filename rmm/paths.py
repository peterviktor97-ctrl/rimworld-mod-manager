"""Steam / RimWorld / GOG detection, Valve KeyValues (VDF) parser, game version helpers."""
from __future__ import annotations

import logging
import os
import re
import string
import sys

from .models import RIMWORLD_APPID, short_version  # noqa: F401 - paths.short_version is public API

_log = logging.getLogger("rmm")

GOG_GAME_ID = "1094900386"

# ---------------------------------------------------------------- VDF
_VDF_TOKEN = re.compile(r'\s+|//[^\n]*|"((?:\\.|[^"\\])*)"?|([{}])|([^\s{}"]+)', re.S)
_VDF_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
_OPEN, _CLOSE = object(), object()


def _vdf_unescape(s: str) -> str:
    if "\\" not in s:
        return s
    # Unknown escapes (e.g. unescaped Windows paths like "C:\Games") keep their backslash.
    return re.sub(r"\\(.)", lambda m: _VDF_ESC.get(m.group(1), m.group(0)), s, flags=re.S)


def _vdf_tokens(text: str):
    pos, n = 0, len(text)
    while pos < n:
        m = _VDF_TOKEN.match(text, pos)
        if not m:          # cannot happen with the catch-all alternatives, but never loop forever
            pos += 1
            continue
        pos = m.end()
        quoted, brace, bare = m.group(1), m.group(2), m.group(3)
        if quoted is not None:
            yield _vdf_unescape(quoted)
        elif brace:
            yield _OPEN if brace == "{" else _CLOSE
        elif bare is not None:
            if bare.startswith("[") and bare.endswith("]"):   # conditional like [$WIN32] -> ignored
                continue
            yield bare


def parse_vdf(text: str) -> dict:
    """Valve KeyValues text -> nested dicts. Tolerates comments, bare tokens, escapes and unbalanced braces."""
    if text.startswith("\ufeff"):
        text = text[1:]
    root: dict = {}
    stack = [root]
    key = None
    for tok in _vdf_tokens(text):
        if tok is _OPEN:
            cur = stack[-1]
            k = "" if key is None else key
            child = cur.get(k)
            if not isinstance(child, dict):
                child = {}
                cur[k] = child
            stack.append(child)
            key = None
        elif tok is _CLOSE:
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = tok
        else:
            stack[-1][key] = tok
            key = None
    return root


def _ci(d, key: str):
    """Case-insensitive dict lookup."""
    if not isinstance(d, dict):
        return None
    if key in d:
        return d[key]
    kl = key.lower()
    for k, v in d.items():
        if k.lower() == kl:
            return v
    return None


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ---------------------------------------------------------------- registry / drives
def _norm(p: str) -> str:
    p = os.path.normpath(os.path.abspath(os.path.expandvars(p.strip().strip('"'))))
    try:   # fix the casing ('c:/program files (x86)/steam' from the registry) without following links elsewhere
        r = os.path.realpath(p)
        if os.path.normcase(r) == os.path.normcase(p):
            return r
    except (OSError, ValueError):
        pass
    return p


def _reg_value(root_name: str, subkey: str, value: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg
    root = getattr(winreg, root_name)
    for view in (0, winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as k:
                v, _ = winreg.QueryValueEx(k, value)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        except OSError:
            continue
    return None


def _drives() -> list[str]:
    """Existing local drive roots ('C:\\'...), skipping CD-ROM/network drives (slow or prompting)."""
    if sys.platform != "win32":
        return ["/"]
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        out = []
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                if k32.GetDriveTypeW(root) in (2, 3, 6):   # removable, fixed, ramdisk
                    out.append(root)
        return out
    except (OSError, AttributeError):
        return [f"{c}:\\" for c in "CDEFG" if os.path.exists(f"{c}:\\")]


def _dedupe_existing(paths) -> list[str]:
    seen, out = set(), []
    for p in paths:
        if not p:
            continue
        p = _norm(p)
        key = os.path.normcase(p)
        if key not in seen and os.path.isdir(p):
            seen.add(key)
            out.append(p)
    return out


# ---------------------------------------------------------------- Steam
def _is_steam_dir(p: str) -> bool:
    return os.path.isfile(os.path.join(p, "steam.exe")) or os.path.isdir(os.path.join(p, "steamapps"))


def find_steam() -> str | None:
    cands = [_reg_value("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
             _reg_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
             _reg_value("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath")]
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "Steam"))
    for d in _drives():
        cands += [os.path.join(d, "Program Files (x86)", "Steam"), os.path.join(d, "Program Files", "Steam"),
                  os.path.join(d, "Steam"), os.path.join(d, "Games", "Steam")]
    for c in cands:
        if c:
            p = _norm(c)
            if _is_steam_dir(p):
                return p
    return None


def steam_libraries(steam_path: str | None = None) -> list[str]:
    steam_path = steam_path or find_steam()
    if not steam_path:
        return []
    libs = [steam_path]
    for vdf in (os.path.join(steam_path, "steamapps", "libraryfolders.vdf"),
                os.path.join(steam_path, "config", "libraryfolders.vdf")):
        data = _ci(parse_vdf(_read_text(vdf)), "libraryfolders")
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            if isinstance(v, dict):
                libs.append(_ci(v, "path") or "")
            elif isinstance(v, str) and k.isdigit():   # pre-2021 format: "1" "D:\\SteamLibrary"
                libs.append(v)
    return _dedupe_existing(libs)


def _manifest_game_dir(lib: str) -> str | None:
    acf = os.path.join(lib, "steamapps", f"appmanifest_{RIMWORLD_APPID}.acf")
    state = _ci(parse_vdf(_read_text(acf)), "AppState")
    installdir = _ci(state, "installdir") if isinstance(state, dict) else None
    for name in ([installdir] if isinstance(installdir, str) and installdir else []) + ["RimWorld"]:
        p = os.path.join(lib, "steamapps", "common", name)
        if is_game_dir(p):
            return _norm(p)
    return None


def is_game_dir(path: str) -> bool:
    if not path:
        return False
    return (os.path.isfile(os.path.join(path, "Data", "Core", "About", "About.xml"))
            or os.path.isfile(os.path.join(path, "RimWorldWin64.exe")) or os.path.isfile(os.path.join(path, "RimWorld.exe"))
            or os.path.isfile(os.path.join(path, "RimWorldWin.exe")))


def _missing(key: str, path: str) -> None:
    # a configured-but-missing folder (unplugged drive, -savedatafolder) must not silently become another install
    _log.warning("Configured %s %s not found; not auto-detecting", key, path)


def _setting(settings, key: str) -> str:
    try:
        v = settings.get(key) if settings is not None else ""
    except Exception:  # noqa: BLE001 - dict-like or Settings; anything odd means "not set"
        return ""
    return v if isinstance(v, str) else ""


def find_game_dir(settings=None) -> str | None:
    s = _setting(settings, "game_dir")
    if s:
        if is_game_dir(s):
            return _norm(s)
        return _missing("game_dir", s)
    for lib in steam_libraries():
        p = _manifest_game_dir(lib)
        if p:
            return p
    for root in ("HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER"):
        for sub in (rf"SOFTWARE\WOW6432Node\GOG.com\Games\{GOG_GAME_ID}", rf"SOFTWARE\GOG.com\Games\{GOG_GAME_ID}"):
            for val in ("path", "PATH", "exe"):
                v = _reg_value(root, sub, val)
                if v:
                    p = os.path.dirname(v) if v.lower().endswith(".exe") else v
                    if is_game_dir(p):
                        return _norm(p)
    rels = [r"Program Files (x86)\Steam\steamapps\common\RimWorld", r"Program Files\Steam\steamapps\common\RimWorld",
            r"Steam\steamapps\common\RimWorld", r"SteamLibrary\steamapps\common\RimWorld",
            r"Games\Steam\steamapps\common\RimWorld", r"Games\SteamLibrary\steamapps\common\RimWorld",
            r"GOG Games\RimWorld", r"Program Files (x86)\GOG Galaxy\Games\RimWorld", r"Games\GOG Games\RimWorld",
            r"Games\RimWorld", r"RimWorld", r"Program Files\RimWorld", r"Program Files (x86)\RimWorld"]
    for d in _drives():
        for rel in rels:
            p = os.path.join(d, rel)
            if is_game_dir(p):
                return _norm(p)
    return None


def find_workshop_dir(game_dir: str | None, settings=None) -> str | None:
    s = _setting(settings, "workshop_dir")
    if s:
        if os.path.isdir(s):
            return _norm(s)
        return _missing("workshop_dir", s)
    cands = []
    if game_dir:
        common = os.path.dirname(os.path.normpath(game_dir))
        if os.path.basename(common).lower() == "common":
            cands.append(os.path.join(os.path.dirname(common), "workshop", "content", RIMWORLD_APPID))
    cands += [os.path.join(lib, "steamapps", "workshop", "content", RIMWORLD_APPID) for lib in steam_libraries()]
    for c in cands:
        if os.path.isdir(c):
            return _norm(c)
    return None


def _user_data_candidates() -> list[str]:
    tail = os.path.join("Ludeon Studios", "RimWorld by Ludeon Studios")
    out = []
    if os.environ.get("USERPROFILE"):
        out.append(os.path.join(os.environ["USERPROFILE"], "AppData", "LocalLow", tail))
    if os.environ.get("LOCALAPPDATA"):
        out.append(os.path.join(os.path.dirname(os.environ["LOCALAPPDATA"]), "LocalLow", tail))
    out.append(os.path.join(os.path.expanduser("~"), "AppData", "LocalLow", tail))
    return out


def find_config_dir(settings=None) -> str | None:
    s = _setting(settings, "config_dir")
    if s and not os.path.isdir(s):
        return _missing("config_dir", s)
    if s:
        s = _norm(s)
        # accept the user-data folder itself and return its Config child
        if os.path.basename(s).lower() != "config" and os.path.isdir(os.path.join(s, "Config")):
            return os.path.join(s, "Config")
        return s
    for ud in _user_data_candidates():
        cfg = os.path.join(ud, "Config")
        if os.path.isdir(cfg) or os.path.isdir(ud):
            return _norm(cfg)
    return None


def user_data_dir(config_dir: str) -> str:
    return os.path.dirname(os.path.normpath(config_dir))


def game_version(game_dir: str | None) -> str:
    if not game_dir:
        return ""
    for line in _read_text(os.path.join(game_dir, "Version.txt")).splitlines():
        if line.strip():
            return line.strip()
    return ""


def game_exe(game_dir: str) -> str | None:
    for name in ("RimWorldWin64.exe", "RimWorld.exe", "RimWorldWin.exe"):
        p = os.path.join(game_dir or "", name)
        if game_dir and os.path.isfile(p):
            return p
    return None


def steam_api_dll(game_dir: str) -> str | None:
    if not game_dir:
        return None
    for rel in ("steam_api64.dll", r"RimWorldWin64_Data\Plugins\x86_64\steam_api64.dll", r"RimWorldWin64_Data\Plugins\steam_api64.dll",
                r"RimWorldWin_Data\Plugins\x86_64\steam_api64.dll"):
        p = os.path.join(game_dir, rel)
        if os.path.isfile(p):
            return p
    return None


def rimsort_dbs_dir() -> str | None:
    cands = []
    for env in ("LOCALAPPDATA", "APPDATA"):
        if os.environ.get(env):
            cands += [os.path.join(os.environ[env], "RimSort", "dbs"), os.path.join(os.environ[env], "RimSort", "RimSort", "dbs")]
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


# ---------------------------------------------------------------- tests
def _selftest() -> None:
    vdf = r'''
    // comment
    "libraryfolders"
    {
        "0" { "path" "C:\\Program Files (x86)\\Steam"  "apps" { "294100" "123" } }
        "1" { path "D:\Games\SteamLib" [$WIN32] "label" "a \"q\" b" }
        "contentstatsid" "-1"
    '''
    d = parse_vdf(vdf)["libraryfolders"]
    assert d["0"]["path"] == r"C:\Program Files (x86)\Steam", d
    assert d["0"]["apps"] == {"294100": "123"}
    assert d["1"]["path"] == r"D:\Games\SteamLib" and d["1"]["label"] == 'a "q" b', d["1"]
    assert d["contentstatsid"] == "-1"
    assert parse_vdf("}}{") == {"": {}} and parse_vdf("") == {} and parse_vdf('"a" "b" "a" "c"') == {"a": "c"}
    assert parse_vdf('"k"\n{\n"x" "1"\n}\n"k" { "y" "2" }') == {"k": {"x": "1", "y": "2"}}
    assert short_version("1.6.4871 rev590") == "1.6" and short_version("") == "" and short_version("v1.10") == "1.10"
    assert short_version("junk") == ""
    assert user_data_dir(r"C:\a\RimWorld by Ludeon Studios\Config") == r"C:\a\RimWorld by Ludeon Studios"
    assert not is_game_dir("") and not is_game_dir(r"C:\definitely\not\here")
    assert game_version(None) == "" and game_exe("") is None and steam_api_dll("") is None
    quiet = _log.disabled
    _log.disabled = True                              # expected warnings below must not reach the app log
    try:
        nope = r"C:\definitely\not\here"
        assert find_game_dir({"game_dir": nope}) is None
        assert find_config_dir({"config_dir": nope}) is None
        assert find_workshop_dir(None, {"workshop_dir": nope}) is None
    finally:
        _log.disabled = quiet


if __name__ == "__main__":
    _selftest()
    steam = find_steam()
    g = find_game_dir()
    print("steam", steam, "libs", steam_libraries(steam))
    print("game", g, repr(game_version(g)), "exe", game_exe(g or ""), "dll", steam_api_dll(g or ""))
    print("workshop", find_workshop_dir(g), "config", find_config_dir(), "rimsort", rimsort_dbs_dir())
