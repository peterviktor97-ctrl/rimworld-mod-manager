"""About.xml parsing, mod scanning, ModsConfig/.rml/.rws/text/html/json import-export, backups, share codes."""
from __future__ import annotations

import base64
import gzip
import html
import json
import os
import re
import shutil
import time
import urllib.parse
import zlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from .models import (APP_NAME, APP_VERSION, CORE_ID, DLC_BY_APPID, DLCS, WORKSHOP_URL, Mod, ModDependency, ModList,
                     ModRef, Progress, noop_progress, short_version)

SHARE_PREFIX = "RMM1:"
_I = re.I | re.S

# ============================================================== tolerant XML
_XML_DECL = re.compile(r"<\?xml[^>]*\?>", re.I)
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")   # characters XML 1.0 forbids
_BAD_PID = re.compile(r'[\s<>&"\x00-\x1f\ud800-\udfff\ufffe\uffff]')                # never valid in a package id
_NAMED_ENT = re.compile(r"&([A-Za-z_][\w.-]*);")
_BAD_AMP = re.compile(r"&(?!(?:#\d+|#x[0-9a-fA-F]+|[A-Za-z_][\w.-]*);)")
_XML5 = {"amp", "lt", "gt", "quot", "apos"}


def _decode(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def _read_text(path: str, limit: int = -1) -> str:
    with open(path, "rb") as f:
        return _decode(f.read(limit))


def _xesc(s: str) -> str:
    """XML-escape for every file we write; a forbidden character would make RimWorld reset ModsConfig.xml."""
    return html.escape(_CTRL.sub("", s), quote=False)


def _fix_entities(text: str) -> str:
    def named(m: re.Match) -> str:
        if m.group(1) in _XML5:
            return m.group(0)
        u = html.unescape(m.group(0))
        return _xesc(u) if u != m.group(0) else "&amp;" + m.group(0)[1:]
    return _BAD_AMP.sub("&amp;", _NAMED_ENT.sub(named, _CTRL.sub("", text)))


def _trim(text: str, root_tag: str | None) -> str:
    """Drop XML declarations, junk before the root element and after its end."""
    text = _XML_DECL.sub("", text.lstrip("\ufeff"))
    if root_tag:
        m = re.search(rf"<{root_tag}[\s>/]", text, re.I)
        if m:
            text = text[m.start():]
            e = re.search(rf"</{root_tag}\s*>", text, re.I)
            if e:
                text = text[:e.end()]
            return text
    i = text.find("<")
    return text[i:] if i > 0 else text


def _lower_tags(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str):
            el.tag = el.tag.rsplit("}", 1)[-1].lower()
    return root


_LEX = re.compile(r"<!\[CDATA\[(.*?)(?:\]\]>|\Z)|<!--.*?(?:-->|\Z)|<[?!][^<>]*>?"
                  r"|<(/?)([A-Za-z_][\w.:\-]*)[^<>]*?(/?)>", re.S)


def _lenient_tree(text: str) -> ET.Element:
    """Never-failing, linear-time tag-soup parser (HTML-parser style): an end tag closes up to its matching open
    ancestor, stray end tags are ignored, an open tag equal to the current one starts a sibling (<li>a<li>b).
    Entities are decoded leniently. Returns a synthetic '#document' root; tags are lowercased."""
    doc = ET.Element("#document")
    stack = [doc]

    def add_text(s: str) -> None:
        cur = stack[-1]
        if len(cur):
            cur[-1].tail = (cur[-1].tail or "") + s
        else:
            cur.text = (cur.text or "") + s
    pos = 0
    for m in _LEX.finditer(text):
        if m.start() > pos:
            add_text(html.unescape(text[pos:m.start()]))
        pos = m.end()
        cdata, close, tag, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        if cdata is not None:
            add_text(cdata)
        elif tag is None:
            continue                                  # comment, declaration, processing instruction
        elif close:
            tag = tag.lower()
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].tag == tag:
                    del stack[i:]
                    break
        else:
            tag = tag.rsplit(":", 1)[-1].lower()
            if len(stack) > 1 and stack[-1].tag == tag:
                stack.pop()
            el = ET.SubElement(stack[-1], tag)
            if not selfclose and len(stack) < 256:
                stack.append(el)
    if pos < len(text):
        add_text(html.unescape(text[pos:]))
    return doc


def _parse_tolerant(text: str, root_tag: str | None) -> tuple[ET.Element, bool]:
    """(root, strict_ok). Strict XML first (as-is, then with entity/control-char repairs); if that fails, the
    lenient tag-soup parser. Never raises. Tags are lowercased; root is the root_tag element when present."""
    t = _trim(text, root_tag)
    for candidate in (t, None):
        try:
            return _lower_tags(ET.fromstring(candidate if candidate is not None else _fix_entities(t))), True
        except ET.ParseError:
            continue
    doc = _lenient_tree(t)
    el = next(doc.iter(root_tag.lower()), None) if root_tag else None
    return (el if el is not None else doc), False


def _find(el, tag: str):
    """First element named tag below el, breadth-first (direct children win), never looking inside <li> items.
    Breadth-first search lets a lenient tree with an unclosed tag still yield its fields."""
    if el is None:
        return None
    level = list(el)
    while level:
        for c in level:
            if c.tag == tag:
                return c
        level = [g for c in level if c.tag != "li" for g in c]
    return None


def _txt(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _get(el, tag: str) -> str:
    return _txt(_find(el, tag))


def _lis(el, keep_empty: bool = False) -> list[str]:
    if el is None:
        return []
    if len(el) == 0:   # '<supportedVersions>1.6</supportedVersions>' style mistakes
        t = (el.text or "").strip()
        return [t] if t else []
    out = [_txt(c) for c in el]
    return out if keep_empty else [x for x in out if x]


def _uniq(items, lower: bool = False) -> list[str]:
    seen, out = set(), []
    for x in items:
        x = x.strip()
        if lower:
            x = x.lower()
        k = x.lower()
        if x and k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ============================================================== About.xml
_WID_IN_URL = re.compile(r"(?:[?&]id=|/filedetails/|CommunityFilePage/)(\d{3,})", re.I)


def _wid_from_url(url: str) -> str:
    m = _WID_IN_URL.search(url or "")
    return m.group(1) if m else ""


# RimWorld's ModMetaData.PackageIdFormatRegex; ValidateDependencies drops dependencies that fail it
_GAME_PID_RX = re.compile(r"(?=.{1,60}$)^(?!\.)(?=.*?[.])(?!.*([.])\1+)[a-zA-Z0-9.]{1,}[a-zA-Z0-9]{1}$")


def _deps(el) -> list[ModDependency]:
    out = []
    for li in (el if el is not None else []):
        pid = _get(li, "packageid").lower()
        if pid:
            url, name = _get(li, "steamworkshopurl"), _get(li, "displayname")
            # the game ignores (with a warning) a dependency it could not tell the player how to get
            enforced = bool(_GAME_PID_RX.match(pid) and name and (url or _get(li, "downloadurl") or "ludeon" in pid))
            out.append(ModDependency(pid, name, url, _wid_from_url(url),
                                     _uniq(_lis(_find(li, "alternativepackageids")), lower=True), enforced))
    return out


def _norm_versions(items: list[str]) -> list[str]:
    return _uniq(map(short_version, items))


def _extract(root: ET.Element, short: str) -> dict:
    vtag = "v" + short if short else None

    def pick(tag: str):
        """RimWorld 1.6 InitVersionedData: a <tagByVersion><vX.Y> entry (even an empty one) REPLACES the generic list."""
        bv = _find(_find(root, tag + "byversion"), vtag) if vtag else None
        return bv if bv is not None else _find(root, tag)

    authors = _lis(_find(root, "authors"))
    a = _get(root, "author")
    if a:
        authors += a.split(",")
    deps: dict[str, ModDependency] = {}
    for d in _deps(pick("moddependencies")):
        deps.setdefault(d.package_id, d)
    return {
        "package_id": _get(root, "packageid").lower().replace(" ", ""),
        "name": _get(root, "name"),
        "authors": _uniq(authors),
        "supported_versions": _norm_versions(_lis(_find(root, "supportedversions"))),
        "description": _get(root, "description"),
        "mod_version": _get(root, "modversion"),
        "url": _get(root, "url"),
        "dependencies": list(deps.values()),
        "load_after": _uniq(_lis(pick("loadafter")) + _lis(_find(root, "forceloadafter")), lower=True),
        "load_before": _uniq(_lis(pick("loadbefore")) + _lis(_find(root, "forceloadbefore")), lower=True),
        "incompatible_with": _uniq(_lis(pick("incompatiblewith")), lower=True),
    }


def _empty_about() -> dict:
    return {"package_id": "", "name": "", "authors": [], "supported_versions": [], "description": "",
            "mod_version": "", "url": "", "dependencies": [], "load_after": [], "load_before": [],
            "incompatible_with": [], "workshop_id": "", "preview_path": "", "parse_error": ""}


def parse_about(mod_dir: str, game_version_short: str = "") -> dict:
    """Parse <mod>/About/About.xml (+ PublishedFileId.txt, Preview.png). Never raises."""
    info = _empty_about()
    about_dir = os.path.join(mod_dir, "About")
    try:
        pf = os.path.join(about_dir, "PublishedFileId.txt")
        if os.path.isfile(pf):
            m = re.search(r"\d{3,}", _read_text(pf, 4096))
            info["workshop_id"] = m.group(0) if m else ""
        if not info["workshop_id"]:
            gh = os.path.join(about_dir, "GitHubRepo.txt")
            if os.path.isfile(gh):
                m_gh = re.search(r"([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", _read_text(gh, 4096))
                if m_gh:
                    info["workshop_id"] = "gh:" + m_gh.group(1).lower()
        pv = os.path.join(about_dir, "Preview.png")
        if os.path.isfile(pv):
            info["preview_path"] = pv
        path = os.path.join(about_dir, "About.xml")
        if not os.path.isfile(path):
            info["parse_error"] = "About/About.xml not found"
            return info
        text = _read_text(path, 4 << 20)
        root, strict = _parse_tolerant(text, "ModMetaData")
        if root.tag != "modmetadata":
            root = next(root.iter("modmetadata"), root)
        if not strict:
            info["parse_error"] = "About.xml is malformed XML; values recovered with a lenient parser"
        info.update(_extract(root, game_version_short))
        mf = os.path.join(about_dir, "Manifest.xml")        # Mod Manager manifest: where most mods keep their version
        if not info["mod_version"] and os.path.isfile(mf):
            m = re.search(r"<version>\s*([^<\s][^<]*?)\s*</version>", _read_text(mf, 65536), re.I)
            info["mod_version"] = m.group(1) if m else ""
    except Exception as e:  # noqa: BLE001 - parse_about must never raise
        info["parse_error"] = f"About.xml could not be read: {e}"
    return info


# ============================================================== scanning
_NO_ASM_DIRS = {"about", "defs", "textures", "languages", "sounds", "patches", "source", "news", ".git", ".vs",
                ".github", ".vscode"}


def _official_pid(folder: str) -> str:
    f = folder.lower()
    if f == "core":
        return CORE_ID
    for pid, (_appid, short) in DLCS.items():
        if short.lower() == f:
            return pid
    return ""


def _official_name(pid: str) -> str:
    return "Core" if pid == CORE_ID else DLCS.get(pid, ("", ""))[1]


def _dlls(folder: str, stems: list[str], seen: set[str]) -> None:
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.lower().endswith(".dll"):
                    stem = e.name[:-4]
                    if stem.lower() not in seen:
                        seen.add(stem.lower())
                        stems.append(stem)
    except OSError:
        pass


def _scan_files(path: str, about: str) -> tuple[list[str], float]:
    """Assembly stems (Assemblies/*.dll and <sub>/Assemblies/*.dll) and newest mtime of the mod root entries."""
    stems: list[str] = []
    seen: set[str] = set()
    mtime = 0.0
    for p in (path, about):
        try:
            mtime = max(mtime, os.stat(p).st_mtime)
        except OSError:
            pass
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return stems, mtime
    for e in entries:
        try:
            mtime = max(mtime, e.stat().st_mtime)   # free on Windows (cached from the directory listing)
            is_dir = e.is_dir()
        except OSError:
            continue
        if not is_dir:
            continue
        low = e.name.lower()
        if low == "assemblies":
            _dlls(e.path, stems, seen)
        elif low not in _NO_ASM_DIRS:
            _dlls(os.path.join(e.path, "Assemblies"), stems, seen)
    return stems, mtime


def _load_folder_mods(mod_dir: str) -> list[str]:
    """packageIds a LoadFolders.xml folder with Defs is conditional on (IfModActive / IfModActiveAll): those defs
    usually inherit from that mod (CE ammo bases, ...), which RimWorld only resolves when it loads first.
    Patch-only folders (CE's compat folders for other mods) don't care about load order."""
    p = os.path.join(mod_dir, "LoadFolders.xml")
    try:
        text = _read_text(p, 1 << 20) if os.path.isfile(p) else ""
    except OSError:
        return []
    out = []
    for ids, folder in re.findall(r'<li\b[^>]*?IfModActive(?:All)?\s*=\s*"([^"]*)"[^>]*>([^<]*)</li>', text, re.I):
        if os.path.isdir(os.path.join(mod_dir, folder.strip().strip("/\\"), "Defs")):
            out += [x.strip() for x in ids.split(",") if x.strip()]
    return _uniq(out, lower=True)


def _join_err(a: str, b: str) -> str:
    return f"{a}; {b}" if a else b


def scan_mod_dir(path: str, source: str, game_version_short: str = "") -> Mod | None:
    folder = os.path.basename(os.path.normpath(path))
    about = os.path.join(path, "About", "About.xml")
    official = source in ("core", "dlc")
    if not os.path.isfile(about) and not (official and _official_pid(folder)):
        return None
    info = parse_about(path, game_version_short)
    err = "" if official and info["parse_error"] == "About/About.xml not found" else info["parse_error"]
    pid = info["package_id"] or (_official_pid(folder) if official else "")
    if not pid:
        pid = folder.lower().replace(" ", "")
        err = _join_err(err, f"no packageId in About.xml; using folder name '{pid}'")
    name = info["name"] or (_official_name(pid) if official else "") or folder
    wid = info["workshop_id"]
    if official:
        wid = ""
    elif not wid and re.fullmatch(r"\d{5,}", folder):
        wid = folder
    versions = info["supported_versions"] or ([game_version_short] if official and game_version_short else [])
    assemblies, mtime = _scan_files(path, about)
    return Mod(uid=pid, package_id=pid, name=name, path=os.path.abspath(path), source=source, workshop_id=wid,
               authors=info["authors"] or (["Ludeon Studios"] if official else []), supported_versions=versions,
               description=info["description"], mod_version=info["mod_version"], url=info["url"],
               preview_path=info["preview_path"], dependencies=info["dependencies"], load_after=info["load_after"],
               load_before=info["load_before"], incompatible_with=info["incompatible_with"],
               folder_after=[] if official else _load_folder_mods(path), assemblies=assemblies,
               mtime=mtime, parse_error=err)


def _is_link(e: os.DirEntry) -> bool:
    try:
        return e.is_symlink() or bool(getattr(e, "is_junction", lambda: False)())
    except OSError:
        return False


def _safe_scan(job: tuple[str, str], short: str) -> Mod | None:
    path, source = job
    try:
        return scan_mod_dir(path, source, short)
    except Exception as e:  # noqa: BLE001 - one weird folder must not kill the scan
        folder = os.path.basename(path).lower()
        return Mod(uid=folder, package_id=folder, name=os.path.basename(path), path=path, source=source,
                   parse_error=f"scan failed: {e}")


def scan_all(game_dir: str | None, workshop_dir: str | None, extra_dirs: list[str], steamcmd_content_dir: str | None,
             game_version_short: str = "", progress: Progress = noop_progress) -> dict[str, Mod]:
    """Scan every mod source (see SPEC) in parallel and assign RimWorld-style uids."""
    jobs: list[tuple[str, str]] = []
    seen_roots: set[str] = set()
    seen_dirs: set[str] = set()

    def key(p: str) -> str:
        return os.path.normcase(os.path.abspath(p))

    def add_root(root: str | None, source: str | None) -> None:
        if not root or not os.path.isdir(root):
            return
        rk = os.path.normcase(os.path.realpath(root))
        if rk in seen_roots:
            return
        seen_roots.add(rk)
        try:
            with os.scandir(root) as it:
                # skip steam.install_items' temporary '<name>.rmm_new_/rmm_old_<ts>' folders (a scan during an install)
                entries = [e for e in it if e.is_dir() and not re.search(r"\.rmm_(old|new)_\d+$", e.name)]
        except OSError:
            return
        if source is None:   # game Data: Core, DLCs in canonical order, then anything else
            order = {"core": -1, **{s.lower(): i for i, (_a, s) in enumerate(DLCS.values())}}
            entries.sort(key=lambda e: (order.get(e.name.lower(), 99), e.name.lower()))
        # other roots keep os.scandir order: it is the order RimWorld's GetDirectories() sees (NTFS: upcase-ordinal),
        # so the copy that gets the plain uid among same-packageId folders is the one the game loads
        for e in entries:
            k = os.path.normcase(os.path.realpath(e.path)) if _is_link(e) else key(e.path)
            if k in seen_dirs:      # e.g. a junction in Mods/ pointing at the SteamCMD content folder
                continue
            seen_dirs.add(k)
            src = source or ("core" if e.name.lower() == "core" else "dlc")
            jobs.append((e.path, src))

    progress(None, "Listing mod folders…")
    if game_dir:
        add_root(os.path.join(game_dir, "Data"), None)
        add_root(os.path.join(game_dir, "Mods"), "local")
    add_root(workshop_dir, "workshop")
    add_root(steamcmd_content_dir, "steamcmd")
    for d in extra_dirs or []:
        add_root(d, "extra")

    n = len(jobs)
    parsed: list[Mod | None] = []
    if n:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="scan") as ex:
            for i, m in enumerate(ex.map(lambda j: _safe_scan(j, game_version_short), jobs), 1):
                parsed.append(m)
                if i % 25 == 0 or i == n:
                    progress(i / n, f"Scanning mods {i}/{n}")

    out: dict[str, Mod] = {}
    first: dict[str, Mod] = {}
    for m in parsed:
        if m is None:
            continue
        pid = m.package_id
        if pid not in first:
            first[pid] = m
            uid = pid
            if uid in out:   # a literal '<x>_steam' / '<x>_dupN' package id already taken by a suffixed uid
                uid = _dup_uid(out, pid)
        else:
            orig = first[pid]
            uid = pid + "_steam"
            if not (m.source == "workshop" and orig.source != "workshop") or uid in out:
                uid = _dup_uid(out, pid)
                m.parse_error = _join_err(m.parse_error, f"duplicate packageId '{pid}' (also in {orig.path})")
        m.uid = uid
        out[uid] = m
    progress(1.0, f"Found {len(out)} mods")
    return out


def _dup_uid(out: dict, pid: str) -> str:
    n = 1
    while f"{pid}_dup{n}" in out:
        n += 1
    return f"{pid}_dup{n}"


def mod_to_ref(m: Mod) -> ModRef:
    return ModRef(package_id=m.package_id, name=m.name, workshop_id=m.workshop_id)


# ============================================================== ModsConfig.xml + backups
def _atomic_write(path: str, data: bytes) -> None:
    from .config import atomic_write
    atomic_write(path, data)


def _xml_lines_to_bytes(lines: list[str]) -> bytes:
    return ("\ufeff" + "\r\n".join(lines)).encode("utf-8")


def _modsconfig_parts(text: str) -> tuple[list[str], list[str], str, bool]:
    """(active, known, version, found_activeMods) from ModsConfig-like XML text."""
    root, _strict = _parse_tolerant(text, None)
    am = next(root.iter("activemods"), None)
    return (_lis(am), _lis(next(root.iter("knownexpansions"), None)), _txt(next(root.iter("version"), None)),
            am is not None)


def read_mods_config(config_dir: str) -> tuple[list[str], list[str], str]:
    if not config_dir:
        return [], [], ""
    path = os.path.join(config_dir, "ModsConfig.xml")
    try:
        text = _read_text(path)
    except OSError:
        return [], [], ""
    active, known, ver, _found = _modsconfig_parts(text)
    return _uniq(active, lower=True), _uniq(known, lower=True), ver


def _modsconfig_bytes(active: list[str], known: list[str], version: str) -> bytes:
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<ModsConfigData>", f"  <version>{_xesc(version)}</version>",
             "  <activeMods>"]
    lines += [f"    <li>{_xesc(x)}</li>" for x in active]
    lines += ["  </activeMods>", "  <knownExpansions>"]
    lines += [f"    <li>{_xesc(x)}</li>" for x in known]
    lines += ["  </knownExpansions>", "</ModsConfigData>"]
    return _xml_lines_to_bytes(lines)


def _backup_file(src: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # same-second backups get -2, -3 … above any existing one (pruning may have removed lower numbers)
    taken = [int(m.group(2) or 1) for m in map(_BACKUP_RX.match, os.listdir(backup_dir)) if m and m.group(1) == stamp]
    n = max(taken, default=0) + 1
    dst = os.path.join(backup_dir, f"ModsConfig-{stamp}.xml" if n == 1 else f"ModsConfig-{stamp}-{n}.xml")
    shutil.copyfile(src, dst)
    return dst


def _prune(backup_dir: str, keep: int) -> None:
    if keep <= 0:
        return
    for old in list_backups(backup_dir)[keep:]:
        try:
            os.remove(old)
        except OSError:
            pass


def _ensure_writable(path: str) -> None:
    """Fail fast (before any backup) on a read-only ModsConfig.xml instead of os.replace's retried WinError 5.
    Users set the flag on purpose so RimWorld can't reset the file, so it is never cleared silently."""
    if os.path.isfile(path) and not os.access(path, os.W_OK):
        raise PermissionError(f"{os.path.basename(path)} is read-only (you or another tool set this so RimWorld cannot "
                              "change it). Untick Read-only in its Properties, then save again.")


def write_mods_config(config_dir: str, active: list[str] | None, known_expansions: list[str] | None, version: str,
                      backup_dir: str | None = None, keep: int = 30) -> str | None:
    """Write ModsConfig.xml (UTF-8 BOM, atomic). Returns the backup path of the previous file, or None.

    active=None keeps the current active list (only an explicit [] writes an empty list);
    known_expansions=None keeps the existing ones. Nothing is written when the content is unchanged."""
    path = os.path.join(config_dir, "ModsConfig.xml")
    old_active, old_known, old_ver = read_mods_config(config_dir)
    if active is None:
        active = old_active
    if known_expansions is None:
        known_expansions = old_known if os.path.isfile(path) else list(DLCS)
    data = _modsconfig_bytes(_uniq(active, lower=True), _uniq(known_expansions, lower=True), version or old_ver)
    backup = None
    if os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                if f.read() == data:
                    return None
        except OSError:
            pass
        _ensure_writable(path)
        if backup_dir:
            backup = _backup_file(path, backup_dir)
            _prune(backup_dir, keep)
    _atomic_write(path, data)
    return backup


_BACKUP_RX = re.compile(r"ModsConfig-(\d{8}-\d{6})(?:-(\d+))?\.xml$", re.I)


def list_backups(backup_dir: str) -> list[str]:
    """ModsConfig backups, newest first."""
    try:
        names = [n for n in os.listdir(backup_dir) if _BACKUP_RX.match(n)]
    except OSError:
        return []

    def k(n: str):
        m = _BACKUP_RX.match(n)
        return m.group(1), int(m.group(2) or 1)
    return [os.path.join(backup_dir, n) for n in sorted(names, key=k, reverse=True)]


def restore_backup(backup_path: str, config_dir: str, backup_dir: str) -> None:
    """Replace ModsConfig.xml with a backup (the current file is backed up first)."""
    with open(backup_path, "rb") as f:
        data = f.read()
    if not _modsconfig_parts(_decode(data))[3]:
        raise ValueError(f"{os.path.basename(backup_path)} is not a ModsConfig file")
    cur = os.path.join(config_dir, "ModsConfig.xml")
    if os.path.isfile(cur):
        _ensure_writable(cur)
        _backup_file(cur, backup_dir)
    _atomic_write(cur, data)


def read_modsconfig_file(path) -> ModList:
    """Any ModsConfig-format XML (RimWorld, RimPy and RimSort exports use <activeMods>)."""
    text = _read_text(path, 32 << 20)
    active, _known, ver, found = _modsconfig_parts(text)
    if not found:   # maybe a saved list or save meta disguised as .xml
        if re.search(r"<savedModList", text[:4096], re.I):
            return read_rml(path)
        if re.search(r"<savegame", text[:4096], re.I):
            return read_save_meta(path)
    refs = [ModRef(package_id=p) for p in _uniq(active, lower=True)]
    return _modlist(_stem(path), refs, ver, "import:modsconfig", path)


# ============================================================== .rml / .rws
def _stem(path) -> str:
    base = os.path.basename(str(path))
    for ext in (".rws.gz", ".rml", ".rws", ".xml", ".json", ".txt", ".html", ".htm", ".md"):
        if base.lower().endswith(ext):
            return base[: -len(ext)]
    return os.path.splitext(base)[0]


def _modlist(name: str, refs: list[ModRef], version: str, source: str, path=None) -> ModList:
    try:
        mt = os.path.getmtime(path) if path else time.time()
    except OSError:
        mt = time.time()
    return ModList(name=name or "Imported list", mods=_dedupe_refs(refs), game_version=version, source=source,
                   created=mt, updated=mt)


def _clean_sid(sid: str, pid: str) -> str:
    sid = (sid or "").strip()
    if not sid.isdigit() or sid == "0" or sid in DLC_BY_APPID or pid == CORE_ID or pid in DLCS:
        return ""
    return sid


def _refs_from_columns(ids: list[str], sids: list[str], names: list[str]) -> list[ModRef]:
    refs = []
    for i, pid in enumerate(ids):
        pid = pid.strip().lower()
        if not pid:
            continue
        sid = sids[i] if i < len(sids) else ""
        name = names[i].strip() if i < len(names) else ""
        refs.append(ModRef(package_id=pid, name=name, workshop_id=_clean_sid(sid, pid)))
    return refs


def read_rml(path) -> ModList:
    """RimWorld's saved mod list (ModLists/*.rml)."""
    text = _read_text(path, 32 << 20)
    root, _strict = _parse_tolerant(text, "savedModList")
    meta, lst = _find(root, "meta"), _find(root, "modlist")
    m_ids, m_sids, m_names = (_lis(_find(meta, t), True) for t in ("modids", "modsteamids", "modnames"))
    ids, names = _lis(_find(lst, "ids"), True), _lis(_find(lst, "names"), True)
    version = _get(meta, "gameversion")
    meta_refs = _refs_from_columns(m_ids, m_sids, m_names)
    if ids:   # modList is authoritative for order; steam ids only exist in meta
        sid_by_pid = {r.package_id: r.workshop_id for r in meta_refs}
        refs = _refs_from_columns(ids, [], names)
        for r in refs:
            r.workshop_id = sid_by_pid.get(r.package_id, "")
    else:
        refs = meta_refs
    return _modlist(_stem(path), refs, version, "import:rml", path)


def _rml_rows(ml: ModList, installed: dict[str, Mod] | None) -> list[tuple[str, str, str]]:
    installed = installed or {}
    by_pid: dict[str, Mod] = {}
    by_wid: dict[str, Mod] = {}
    for m in installed.values():
        by_pid.setdefault(m.package_id, m)
        if m.workshop_id:
            by_wid.setdefault(m.workshop_id, m)
    rows, seen = [], set()
    for ref in ml.mods:
        pid = (ref.package_id or "").strip().lower()
        base = pid[:-6] if pid.endswith("_steam") else pid
        m = installed.get(pid) or by_pid.get(base) or (by_wid.get(ref.workshop_id) if ref.workshop_id else None)
        # RimWorld's "Load mod list" matches ids exactly: write the id the game uses right now ('_steam' only while
        # a local duplicate exists)
        if m is None:
            pid = base
        elif pid not in installed:
            pid = m.uid if m.uid in (m.package_id, m.package_id + "_steam") else m.package_id
        if not pid or pid in seen:
            continue            # a workshop-only ref cannot be expressed in RimWorld's format
        seen.add(pid)
        if pid == CORE_ID:
            sid = "0"
        elif pid in DLCS:
            sid = DLCS[pid][0]
        else:
            sid = ref.workshop_id or (m.workshop_id if m else "") or "0"
        rows.append((pid, sid, ref.name or (m.name if m else "") or pid))
    return rows


def write_rml(ml: ModList, path, game_version: str, installed: dict[str, Mod] | None = None) -> None:
    """Write RimWorld's .rml format (tab-indented, CRLF, UTF-8 BOM), atomically."""
    rows = _rml_rows(ml, installed)

    def block(tag: str, values, depth: int) -> list[str]:
        t = "\t" * depth
        return [f"{t}<{tag}>"] + [f"{t}\t<li>{_xesc(v)}</li>" for v in values] + [f"{t}</{tag}>"]
    ids, sids, names = [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<savedModList>", "\t<meta>",
             f"\t\t<gameVersion>{_xesc(game_version or ml.game_version)}</gameVersion>"]
    lines += block("modIds", ids, 2) + block("modSteamIds", sids, 2) + block("modNames", names, 2)
    lines += ["\t</meta>", "\t<modList>"] + block("ids", ids, 2) + block("names", names, 2)
    lines += ["\t</modList>", "</savedModList>"]
    _atomic_write(str(path), _xml_lines_to_bytes(lines))


def read_save_meta(path) -> ModList:
    """Mod list stored in a save (.rws). Streams with iterparse and stops after </meta> (saves can be 100 MB+)."""
    with open(path, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    cols = {"modids": [], "modsteamids": [], "modnames": []}
    version = ""
    try:
        with (gzip.open(path, "rb") if gz else open(path, "rb")) as f:
            stack: list[str] = []
            for ev, el in ET.iterparse(f, events=("start", "end")):
                tag = el.tag.lower() if isinstance(el.tag, str) else ""
                if ev == "start":
                    stack.append(tag)
                    if len(stack) == 1 and tag != "savegame":
                        break
                    continue
                stack.pop()
                parent = stack[-1] if stack else ""
                if tag == "li" and parent in cols and len(stack) >= 2 and stack[-2] == "meta":
                    cols[parent].append((el.text or "").strip())
                elif tag == "gameversion" and parent == "meta":
                    version = (el.text or "").strip()
                elif len(stack) == 1:    # end of a direct child of <savegame>: meta is always first
                    break
    except (ET.ParseError, OSError, EOFError, zlib.error):
        head = ""
        try:
            with (gzip.open(path, "rb") if gz else open(path, "rb")) as f:
                head = _decode(f.read(4 << 20))
        except (OSError, EOFError, zlib.error):
            pass
        end = re.search(r"</meta\s*>", head, re.I)
        meta = next(_lenient_tree(head[:end.end()] if end else head).iter("meta"), None)
        cols = {t: _lis(_find(meta, t), True) for t in cols}
        version = _get(meta, "gameversion")
    refs = _refs_from_columns(cols["modids"], cols["modsteamids"], cols["modnames"])
    return _modlist(_stem(path), refs, version, "import:rws", path)


# ============================================================== text lists
_PID_RX = re.compile(r"^[a-z0-9_\-]*[a-z][a-z0-9_\-]*(?:\.[a-z0-9_\-]+)+$", re.I)
_FILE_EXT = {"xml", "txt", "dll", "png", "jpg", "json", "html", "htm", "exe", "zip", "md", "com", "net", "org",
             "io", "gg"}
_STRIP = "\"'()[]{}<>*`"


def _as_pid(tok: str, strict: bool = True) -> str:
    """Package id in tok, or ''. strict (free text) also rejects 'v1.2', 'e.g', 'About.xml', 'site.com' lookalikes."""
    t = tok.strip(_STRIP).rstrip(".,;:!?").strip(_STRIP)
    if not _PID_RX.match(t):
        return ""
    parts = t.split(".")
    if strict and (len(parts[0]) < 2 or parts[-1].lower() in _FILE_EXT
                   or not any(re.search(r"[a-z]", p, re.I) for p in parts[1:])):
        return ""
    return t.lower()


def _as_wid(tok: str) -> str:
    t = tok.strip(_STRIP).rstrip(".,;:!?")
    low = t.lower()
    if "steamcommunity." in low or "steam://" in low or "steampowered." in low:
        m = re.search(r"[?&]id=(\d{3,})", t) or re.search(r"(\d{5,})(?!.*\d{5,})", t)
        return m.group(1) if m else ""
    if "github.com/" in low or low.startswith("gh:"):
        m_gh = re.search(r"(?:github\.com/|^gh:)([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", t, re.I)
        if m_gh:
            repo = re.sub(r"\.git$", "", m_gh.group(1).lower())
            return f"gh:{repo}"
    return t if re.fullmatch(r"\d{5,}", t) else ""


def _dedupe_refs(refs: list[ModRef]) -> list[ModRef]:
    """Merge refs that share a package id or workshop id (keeping the first position)."""
    out: list[ModRef] = []
    by_p: dict[str, ModRef] = {}
    by_w: dict[str, ModRef] = {}
    for r in refs:
        r.package_id = (r.package_id or "").strip().lower()
        r.workshop_id = (r.workshop_id or "").strip()
        r.name = (r.name or "").strip()
        if not r.package_id and not r.workshop_id:
            continue
        ex = by_p.get(r.package_id) if r.package_id else None
        if ex is None and r.workshop_id:
            ex = by_w.get(r.workshop_id)
            if ex is not None and ex.package_id and r.package_id and ex.package_id != r.package_id:
                ex = None     # same workshop item listed under two ids (e.g. '_steam'): keep both
        if ex is None:
            out.append(r)
            ex = r
        else:
            ex.package_id = ex.package_id or r.package_id
            ex.workshop_id = ex.workshop_id or r.workshop_id
            ex.name = ex.name or r.name
        if ex.package_id:
            by_p.setdefault(ex.package_id, ex)
        if ex.workshop_id:
            by_w.setdefault(ex.workshop_id, ex)
    return out


_BULLET = re.compile(r"^\s*(?:[-*+•>]+|\d+[.)])\s*")


def parse_text_list(text: str) -> list[ModRef]:
    """Workshop URLs, steam:// links, bare ids (>=5 digits) and package ids; one or more per line.

    A line holding exactly one package id and one workshop id (e.g. our 'name | pid | url' export or
    RimSort's markdown) becomes a single ref; its remaining text becomes the name."""
    refs: list[ModRef] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "//", ";", "<!--")):
            continue
        name = ""
        scan = line
        strict = True
        if "|" in line:     # 'name | package_id | url' columns: trust the id column
            fields = [f.strip() for f in line.split("|")]
            # old exports of nameless refs repeat the id as the name: 'brrainz.harmony | brrainz.harmony | url'
            dup = fields[0] != "" and fields[0] in fields[1:3]
            if fields and (dup or not (_as_pid(fields[0]) or _as_wid(fields[0]))):
                name, scan, strict = "" if dup else fields[0], " ".join(fields[1:]), False
        md = re.search(r"\[([^\]]+)\]\((?:https?|steam)://", line)
        if md:
            name = md.group(1)
        found: list[tuple[str, str]] = []
        before: list[str] = []      # words before the first id: 'Some Mod - https://…' -> name
        for tok in re.split(r"[\s,;|]+", scan):
            if not tok:
                continue
            w = _as_wid(tok)
            p = "" if w else _as_pid(tok, strict)
            if w:
                found.append(("w", w))
            elif p:
                found.append(("p", p))
            elif not found:
                before.append(tok)
        if not found:
            continue
        pids = [v for k, v in found if k == "p"]
        wids = [v for k, v in found if k == "w"]
        if len(pids) <= 1 and len(wids) <= 1:
            if not name and "://" not in " ".join(before):
                name = _BULLET.sub("", " ".join(before)).strip(" -–—:|\t")
                name = name if len(name) <= 120 and re.search(r"[^\W\d_]", name) else ""
            refs.append(ModRef(package_id=pids[0] if pids else "", name=name, workshop_id=wids[0] if wids else ""))
        else:
            for k, v in found:
                refs.append(ModRef(package_id=v) if k == "p" else ModRef(workshop_id=v))
    return _dedupe_refs(refs)


def write_text_list(ml: ModList, path) -> None:
    lines = [f"# {ml.name}",
             f"# Exported by {APP_NAME} {APP_VERSION} on {time.strftime('%Y-%m-%d %H:%M')} - {len(ml.mods)} mods"
             + (f" - game version {ml.game_version}" if ml.game_version else ""),
             "# Format: name | package_id | workshop URL  (re-import this file, or paste it into the Import text dialog)"]
    for r in ml.mods:
        url = WORKSHOP_URL.format(r.workshop_id) if r.workshop_id else ""
        lines.append(f"{r.name.replace('|', '/')} | {r.package_id} | {url}")   # '' name -> column mode on re-import
    _atomic_write(str(path), ("\r\n".join(lines) + "\r\n").encode("utf-8"))


# ============================================================== HTML (the "browser method")
_HTML_CSS = """
:root{--bg:#1e1f22;--panel:#2b2d31;--text:#e6e6e6;--muted:#9aa0a6;--accent:#5b8def;--border:#3a3c42;--ok:#3ba55d;--row:#25262a}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--panel:#fff;--text:#1f2328;--muted:#5f6670;--accent:#2f6fde;--border:#d7dbe0;--ok:#1f8a4c;--row:#f0f2f5}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px}h1{font-size:22px;margin:0 0 4px}.muted{color:var(--muted)}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--panel);border:1px solid var(--border);
border-radius:8px;padding:10px 12px;margin:14px 0;position:sticky;top:0}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 12px;font:inherit;cursor:pointer}
button.alt{background:transparent;color:var(--text);border:1px solid var(--border)}button:disabled{opacity:.5;cursor:default}
input[type=number]{width:64px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{font-weight:600;color:var(--muted)}tr:nth-child(even) td{background:var(--row)}tr.done td{opacity:.55}
td.n{color:var(--muted);width:42px}code{font:12px Consolas,monospace;color:var(--muted);word-break:break-all}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}a:visited{color:var(--ok)}
details{margin-top:14px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 12px}
textarea{width:100%;height:160px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;font:12px Consolas,monospace}
"""

_HTML_JS = """
const rows=[...document.querySelectorAll('tr[data-id]')];let pos=0;
function url(id){return 'https://steamcommunity.com/sharedfiles/filedetails/?id='+id}
function upd(){const b=document.getElementById('next');const n=Math.max(1,+document.getElementById('batch').value||10);
 if(pos>=rows.length){b.textContent='All '+rows.length+' opened';b.disabled=true;return}
 b.disabled=false;b.textContent='Open next '+Math.min(n,rows.length-pos)+' ('+(pos+1)+'\\u2013'+Math.min(pos+n,rows.length)+' of '+rows.length+')'}
function openBatch(){const n=Math.max(1,+document.getElementById('batch').value||10);let blocked=false;
 rows.slice(pos,pos+n).forEach(r=>{const w=window.open(url(r.dataset.id),'_blank');if(!w)blocked=true;r.classList.add('done')});
 pos=Math.min(rows.length,pos+n);upd();
 if(blocked)alert('Your browser blocked some tabs. Allow pop-ups for this page, then press Reset and try again.')}
function reset(){pos=0;rows.forEach(r=>r.classList.remove('done'));upd()}
function copyIds(){const t=document.getElementById('ids');t.style.display='block';t.select();
 try{navigator.clipboard.writeText(t.value)}catch(e){document.execCommand('copy')}}
document.getElementById('batch').addEventListener('input',upd);upd();
"""


def write_html_list(ml: ModList, path) -> None:
    """Standalone page: every mod with its workshop page + steam:// link and an 'open next batch' button."""
    e = html.escape
    body_rows, wids = [], []
    for i, r in enumerate(ml.mods, 1):
        name = e(r.name or r.package_id or r.workshop_id)
        if r.package_id == CORE_ID or r.package_id in DLCS:
            link = "Base game" if r.package_id == CORE_ID else \
                f'<a href="https://store.steampowered.com/app/{DLCS[r.package_id][0]}" target="_blank">Store page (DLC)</a>'
            body_rows.append(f"<tr><td class=n>{i}</td><td>{name}</td><td><code>{e(r.package_id)}</code></td>"
                             f"<td>{link}</td><td></td></tr>")
        elif r.workshop_id:
            wid = e(r.workshop_id)
            wids.append(r.workshop_id)
            body_rows.append(f'<tr data-id="{wid}"><td class=n>{i}</td><td>{name}</td><td><code>{e(r.package_id)}</code></td>'
                             f'<td><a href="{WORKSHOP_URL.format(wid)}" target="_blank">Workshop page</a></td>'
                             f'<td><a href="steam://url/CommunityFilePage/{wid}">Open in Steam</a></td></tr>')
        else:
            search = ("https://steamcommunity.com/workshop/browse/?appid=294100&searchtext="
                      + urllib.parse.quote(r.name or r.package_id))
            body_rows.append(f"<tr><td class=n>{i}</td><td>{name}</td><td><code>{e(r.package_id)}</code></td>"
                             f'<td><a href="{e(search)}" target="_blank">Search workshop</a></td><td></td></tr>')
    data = json.dumps(ml.to_dict(), ensure_ascii=False).replace("</", "<\\/")
    steamcmd = "\n".join(["@ShutdownOnFailedCommand 0", "@NoPromptForPassword 1", "login anonymous"]
                         + [f"workshop_download_item 294100 {w} validate" for w in wids] + ["quit"])
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(ml.name)} - RimWorld mod list</title><style>{_HTML_CSS}</style></head>
<body><div class="wrap">
<h1>{e(ml.name)}</h1>
<div class="muted">{len(ml.mods)} mods{(' &middot; game version ' + e(ml.game_version)) if ml.game_version else ''}
 &middot; exported by {e(APP_NAME)} {e(APP_VERSION)} on {time.strftime('%Y-%m-%d %H:%M')}</div>
{('<p>' + e(ml.notes) + '</p>') if ml.notes else ''}
<div class="bar"><button id="next" onclick="openBatch()">Open next batch</button>
<label class="muted">batch size <input id="batch" type="number" min="1" max="50" value="10"></label>
<button class="alt" onclick="reset()">Reset</button><button class="alt" onclick="copyIds()">Copy workshop IDs</button>
<span class="muted">Opens workshop pages in tabs so you can Subscribe to each. Visited links turn green.</span></div>
<textarea id="ids" style="display:none" readonly>{e(chr(10).join(wids))}</textarea>
<table><thead><tr><th>#</th><th>Name</th><th>Package ID</th><th>Workshop</th><th>Steam client</th></tr></thead>
<tbody>
{chr(10).join(body_rows)}
</tbody></table>
<details><summary>SteamCMD script ({len(wids)} items)</summary>
<p class="muted">Save as <code>mods.txt</code> and run <code>steamcmd +runscript mods.txt</code>; items land in
<code>steamapps/workshop/content/294100/&lt;id&gt;</code> &mdash; move those folders into RimWorld's <code>Mods</code> folder.</p>
<textarea readonly>{e(steamcmd)}</textarea></details>
</div>
<script type="application/json" id="rmm-modlist">{data}</script>
<script>{_HTML_JS}</script>
</body></html>
"""
    _atomic_write(str(path), page.encode("utf-8"))


# ============================================================== share codes
def encode_share_code(ml: ModList) -> str:
    # first row ['', '', list name]: without ids, so older decoders drop it (_dedupe_refs) and still read the list
    rows = [["", "", ml.name]] + [[r.package_id, r.workshop_id, r.name] for r in ml.mods]
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return SHARE_PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii").rstrip("=")


def _share_rows(b64: str) -> tuple[str, list[ModRef]]:
    """(list name, refs); codes from before the name row have name ''."""
    try:
        packed = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        d = zlib.decompressobj()
        raw = d.decompress(packed, 16 << 20)
        if d.unconsumed_tail or not d.eof or d.unused_data:
            raise ValueError("truncated or oversized data")
        rows = json.loads(raw.decode("utf-8"))
    except (ValueError, zlib.error, UnicodeDecodeError, RecursionError) as e:
        raise ValueError(str(e)) from None
    if not isinstance(rows, list):
        raise ValueError("unexpected content")
    list_name, refs = "", []
    for i, row in enumerate(rows):
        if not isinstance(row, list) or not row or not all(isinstance(x, str) for x in row):
            raise ValueError("unexpected entry")
        pid, wid, name = (row + ["", "", ""])[:3]
        if i == 0 and not pid and not wid:
            list_name = " ".join(_CTRL.sub("", name).split())[:120]
            continue
        pid = "" if _BAD_PID.search(pid) else pid.lower()     # crafted codes: never let markup/controls into XML
        refs.append(ModRef(package_id=pid, name=name, workshop_id=wid if wid.isdigit() else ""))
    return list_name, refs


def decode_share_code(code: str) -> ModList:
    """Decode an 'RMM1:' code, even when embedded in other text or wrapped over lines. Raises ValueError."""
    m = re.search(r"RMM1:", code or "", re.I)
    if not m:
        raise ValueError("Not a mod list share code (expected text starting with RMM1:)")
    chunks = re.match(r"[A-Za-z0-9_\-\s]*", code[m.end():]).group(0).split()[:200]
    err = "empty code"
    for k in range(len(chunks), 0, -1):     # longest run first; trailing words after the code get dropped
        try:
            name, refs = _share_rows("".join(chunks[:k]))
        except ValueError as e:
            err = str(e)
            continue
        now = time.time()
        return ModList(name=name or "Shared list", mods=_dedupe_refs(refs), source="import:share", created=now, updated=now)
    raise ValueError(f"Corrupt share code: {err}")


# ============================================================== JSON (ours + RimSort/RimPy-ish)
def _ref_from_obj(item, strict: bool = True) -> ModRef | None:
    """strict=False (our own saved lists): trust the stored package id as-is (1-char parts, '.com' endings, no-dot
    folder-name ids are all real), rejecting only whitespace/markup."""
    if isinstance(item, str):
        w = _as_wid(item)
        p = "" if w else _as_pid(item)
        return ModRef(package_id=p, workshop_id=w) if (p or w) else None
    if not isinstance(item, dict):
        return None
    low = {str(k).lower(): v for k, v in item.items()}

    def pick(*keys) -> str:
        for k in keys:
            v = low.get(k)
            if isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
                return str(v).strip()
        return ""
    pid = pick("packageid", "package_id", "packageidlowercase", "modid", "mod_id", "id")
    if pid and (_BAD_PID.search(pid) if not strict else not _as_pid(pid)):
        pid = ""
    wid = pick("publishedfileid", "published_file_id", "workshop_id", "workshopid", "steamid", "steam_id",
               "publishedid", "fileid")
    if not wid.isdigit():
        wid = _as_wid(pick("url", "steamworkshopurl", "workshop_url", "link"))
    if not wid and pick("id").isdigit() and len(pick("id")) >= 5:
        wid = pick("id")
    name = pick("name", "displayname", "display_name", "title", "steamname", "modname")
    if not pid and not wid:
        return None
    return ModRef(package_id=pid.lower(), name=name, workshop_id=wid if wid != "0" else "")


def _refs_from_json(obj) -> tuple[list[ModRef], str]:
    """Find the most plausible active mod list anywhere in a JSON document. Returns (refs, game version)."""
    best: tuple[tuple[int, int], list[ModRef]] = ((-1, -1), [])
    version = ""

    def walk(o, key: str, depth: int) -> None:
        nonlocal best, version
        if depth > 5:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if kl in ("version", "gameversion", "game_version") and isinstance(v, str) and not version:
                    version = v
                walk(v, kl, depth + 1)
        elif isinstance(o, list):
            if any(s in key for s in ("inactive", "known", "expansion", "depend", "incompat", "rules")) or \
                    key.startswith(("loadafter", "loadbefore", "load_after", "load_before", "forceload", "force_load")):
                return
            refs = [r for r in (_ref_from_obj(x) for x in o) if r]
            if not refs:
                for x in o:
                    walk(x, key, depth + 1)
                return
            prio = 2 if "active" in key else 1 if (key == "" or any(s in key for s in ("mod", "list", "ids", "order"))) else 0
            score = (prio, sum(1 + bool(r.name) + bool(r.workshop_id) for r in refs))
            if score > best[0]:
                best = (score, refs)

    walk(obj, "", 0)
    return best[1], version


def _modlist_from_json(obj, path) -> ModList:
    if isinstance(obj, dict) and isinstance(obj.get("mods"), list) and \
            (obj.get("format") == "rmm-modlist" or all(isinstance(m, dict) for m in obj["mods"])):
        # ours: the format tag (saved lists) or ModList.to_dict rows (HTML exports embed those without the tag)
        ours = obj.get("format") == "rmm-modlist" or all(
            isinstance(m, dict) and set(m) == {"package_id", "name", "workshop_id"} for m in obj["mods"])
        refs = [r for r in (_ref_from_obj(m, strict=not ours) for m in obj["mods"]) if r]
        if refs or not obj["mods"]:
            def num(k):
                v = obj.get(k)
                return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
            name = obj.get("name") if isinstance(obj.get("name"), str) and obj.get("name").strip() else _stem(path)
            return ModList(name=name, mods=_dedupe_refs(refs), game_version=str(obj.get("game_version") or ""),
                           notes=str(obj.get("notes") or ""), source=str(obj.get("source") or "manual"),
                           created=num("created"), updated=num("updated"))
    refs, version = _refs_from_json(obj)
    if not refs:
        raise ValueError(f"No mod list found in {os.path.basename(str(path))}")
    return _modlist(_stem(path), refs, version, "import:json", path)


def load_modlist_json(path) -> ModList:
    try:
        obj = json.loads(_read_text(path))     # UTF-8/16 BOMs and ANSI (Notepad, PowerShell Out-File) too
    except RecursionError:
        raise ValueError("mod list is nested too deeply") from None
    return _modlist_from_json(obj, path)


def save_modlist_json(ml: ModList, path) -> None:
    d = {"format": "rmm-modlist", **ml.to_dict()}
    _atomic_write(str(path), json.dumps(d, indent=1, ensure_ascii=False).encode("utf-8"))


# ============================================================== import dispatch
def _from_html(text: str, path) -> ModList:
    m = re.search(r'<script[^>]*id="rmm-modlist"[^>]*>(.*?)</script>', text, _I)
    if m:
        try:
            return _modlist_from_json(json.loads(m.group(1)), path)
        except (ValueError, RecursionError):
            pass
    urls = re.findall(r"""href\s*=\s*["']([^"']+)["']""", text, re.I)
    plain = re.sub(r"<[^>]+>", "\n", re.sub(r"<(script|style)\b.*?</\1\s*>", "", text, flags=_I))
    refs = parse_text_list("\n".join(urls) + "\n" + html.unescape(plain))
    return _modlist(_stem(path), refs, "", "import:html", path)


def import_any(path: str) -> ModList:
    """Import a mod list from .rml, .rws, .json (ours / RimSort / RimPy-ish), .xml (ModsConfig-like), .html, .txt …

    Raises ValueError when the file holds no recognisable mod list, OSError when it cannot be read."""
    low = str(path).lower()
    if low.endswith((".rws", ".rws.gz")):
        ml = read_save_meta(path)
    elif low.endswith(".rml"):
        ml = read_rml(path)
    else:
        with open(path, "rb") as f:
            head = _decode(f.read(65536)).lstrip("\ufeff \t\r\n")
        if low.endswith(".json") or head[:1] in ("{", "["):
            try:
                ml = load_modlist_json(path)
            except ValueError:
                if low.endswith(".json"):
                    raise
                ml = _modlist(_stem(path), parse_text_list(_read_text(path, 16 << 20)), "", "import:txt", path)
        elif low.endswith((".html", ".htm")) or re.match(r"<!doctype html|<html", head, re.I):
            ml = _from_html(_read_text(path, 16 << 20), path)
        elif head.startswith("<"):
            if re.search(r"<savedModList", head, re.I):
                ml = read_rml(path)
            elif re.search(r"<savegame", head, re.I):
                ml = read_save_meta(path)
            else:
                ml = read_modsconfig_file(path)
        else:
            text = _read_text(path, 16 << 20)
            if SHARE_PREFIX.lower() in text[:200].lower():
                ml = decode_share_code(text)
                ml.name = _stem(path)
            else:
                ml = _modlist(_stem(path), parse_text_list(text), "", "import:txt", path)
    if not ml.mods:
        raise ValueError(f"No mods found in {os.path.basename(str(path))}")
    return ml


# ============================================================== tests
def _selftest() -> None:
    import tempfile
    tmp = tempfile.mkdtemp(prefix="rmm_mods_")
    try:
        def mk(rel: str, content, binary=False):
            p = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(content if binary else content.encode("utf-8"))
            return p

        # --- About.xml torture test: BOM, junk before decl, unescaped &, &nbsp;, mixed case, byVersion
        mk("game/Data/Core/About/About.xml", '\ufeff<?xml version="1.0"?><ModMetaData><packageId>Ludeon.RimWorld</packageId>'
           "<author>Ludeon Studios</author></ModMetaData>")
        mk("game/Data/Royalty/About/About.xml", "<ModMetaData><packageId>Ludeon.RimWorld.Royalty</packageId></ModMetaData>")
        mk("game/Version.txt", "1.6.4871 rev590\n")
        mk("game/Mods/A/About/Manifest.xml", "<Manifest>\n <version> 2.3.1 </version>\n</Manifest>")
        mk("game/Mods/A/About/About.xml", 'garbage\ufeff<?xml version="1.0" encoding="utf-8"?>\n<ModMetaData>'
           "<PackageID>Foo.Bar</PackageID><name>Foo & Bar&nbsp;Mod</name><authors><li>Ann</li><li>Bob</li></authors>"
           "<author>Bob, Cy</author><supportedVersions><li>1.5</li><li>v1.6</li></supportedVersions>"
           "<modDependencies><li><packageId>generic.only</packageId><displayName>G</displayName></li></modDependencies>"
           "<modDependenciesByVersion><v1.6><li><packageId>OskarPotocki.VanillaFactionsExpanded.Core</packageId>"
           "<alternativePackageIds><li>Fork.VFECore</li></alternativePackageIds></li><li><packageId>brrainz.harmony"
           "</packageId><displayName>Harmony</displayName><steamWorkshopUrl>steam://url/CommunityFilePage/2009463077"
           "</steamWorkshopUrl></li></v1.6><v1.5><li><packageId>old.dep</packageId></li>"
           "</v1.5></modDependenciesByVersion><loadAfter><li>X.Y</li></loadAfter><forceLoadAfter><li>Z.W</li>"
           "</forceLoadAfter><loadAfterByVersion><v1.6/></loadAfterByVersion><loadBeforeByVersion><v1.6><li>Q.R</li>"
           "</v1.6></loadBeforeByVersion><incompatibleWith><li>Bad.Mod</li></incompatibleWith><incompatibleWithByVersion>"
           "<v1.5><li>x.five</li></v1.5></incompatibleWithByVersion></ModMetaData>trailing junk")
        mk("game/Mods/A/About/PublishedFileId.txt", "  123456789\r\n")
        mk("game/Mods/A/About/Preview.png", b"\x89PNG", True)
        mk("game/Mods/A/Assemblies/FooBar.dll", b"", True)
        mk("game/Mods/A/1.6/Assemblies/FooBar.dll", b"", True)
        mk("game/Mods/A/1.6/Assemblies/Extra.DLL", b"", True)
        mk("game/Mods/A/Textures/Assemblies/Nope.dll", b"", True)
        # broken beyond XML repair -> regex fallback; dependency listed before the mod's own packageId
        mk("game/Mods/B/About/About.xml", "<ModMetaData><modDependencies><li><packageId>dep.one</packageId></li>"
           "</modDependencies><name>Broken <b>mod</name><packageId>Broken.Mod</packageId><loadAfter><li>foo.bar</li>"
           "</loadAfter><supportedVersions><li>1.6</li></supportedVersions>")
        mk("game/Mods/NoPid/About/About.xml", "<ModMetaData><name>No Pid</name></ModMetaData>")
        mk("game/Mods/NotAMod/readme.txt", "x")
        mk("ws/555555/About/About.xml", "<ModMetaData><packageId>foo.bar</packageId><name>Foo WS</name></ModMetaData>")
        mk("scmd/777777/About/About.xml", "<ModMetaData><packageId>foo.bar</packageId><name>Foo 3</name></ModMetaData>")

        a = parse_about(os.path.join(tmp, "game/Mods/A"), "1.6")
        assert a["package_id"] == "foo.bar" and a["name"] == "Foo & Bar\xa0Mod" and not a["parse_error"], a
        assert a["authors"] == ["Ann", "Bob", "Cy"] and a["supported_versions"] == ["1.5", "1.6"], a
        # a v1.6 byVersion list (even empty) REPLACES the generic one, like RimWorld 1.6; force* lists are kept
        vfe, har = a["dependencies"]
        assert (vfe.package_id, har.package_id) == ("oskarpotocki.vanillafactionsexpanded.core", "brrainz.harmony")
        assert har.workshop_id == "2009463077" and har.display_name == "Harmony" and har.enforced and not har.alternatives
        assert vfe.alternatives == ["fork.vfecore"] and not vfe.enforced     # no displayName/URL: the game drops it
        assert a["load_after"] == ["z.w"] and a["load_before"] == ["q.r"] and a["incompatible_with"] == ["bad.mod"], a
        assert a["workshop_id"] == "123456789" and a["preview_path"].endswith("Preview.png")
        b = parse_about(os.path.join(tmp, "game/Mods/B"), "1.6")
        assert b["package_id"] == "broken.mod" and b["name"] == "Broken mod" and b["parse_error"], b
        assert [d.package_id for d in b["dependencies"]] == ["dep.one"] and b["load_after"] == ["foo.bar"], b
        assert parse_about(os.path.join(tmp, "nope"))["parse_error"] == "About/About.xml not found"

        prog = []
        mods = scan_all(os.path.join(tmp, "game"), os.path.join(tmp, "ws"), [os.path.join(tmp, "game/Mods")],
                        os.path.join(tmp, "scmd"), "1.6", progress=lambda f, t: prog.append(f))
        assert list(mods)[:2] == ["ludeon.rimworld", "ludeon.rimworld.royalty"], list(mods)
        core = mods["ludeon.rimworld"]
        assert core.source == "core" and core.name == "Core" and core.supported_versions == ["1.6"] and not core.parse_error
        assert mods["ludeon.rimworld.royalty"].name == "Royalty" and mods["ludeon.rimworld.royalty"].source == "dlc"
        fb = mods["foo.bar"]
        assert fb.mod_version == "2.3.1", fb.mod_version      # from About/Manifest.xml
        assert fb.source == "local" and sorted(fb.assemblies) == ["Extra", "FooBar"] and fb.mtime > 0, fb.assemblies
        assert mods["foo.bar_steam"].source == "workshop" and mods["foo.bar_steam"].workshop_id == "555555"
        assert mods["foo.bar_dup1"].source == "steamcmd" and "duplicate" in mods["foo.bar_dup1"].parse_error
        assert mods["nopid"].parse_error and mods["nopid"].name == "No Pid"
        assert "notamod" not in mods and len(mods) == 7, list(mods)   # extra dir == Mods -> not scanned twice
        assert prog and prog[-1] == 1.0
        for f in ("Dup_1", "DupA"):   # same package id: the plain uid goes to the folder the game's GetDirectories lists first
            mk(f"order/Mods/{f}/About/About.xml", "<ModMetaData><packageId>t.dup</packageId></ModMetaData>")
        mk("order/Mods/DupA.rmm_new_17/About/About.xml", "<ModMetaData><packageId>t.dup</packageId></ModMetaData>")
        od = scan_all(os.path.join(tmp, "order"), None, [], None)    # an install's staging folder is not a mod
        assert os.path.basename(od["t.dup"].path) == next(os.scandir(os.path.join(tmp, "order/Mods"))).name
        assert "t.dup_dup1" in od and "t.dup_dup2" not in od

        # --- ModsConfig + backups
        cfg, bk = os.path.join(tmp, "Config"), os.path.join(tmp, "backups")
        assert read_mods_config(cfg) == ([], [], "")
        assert write_mods_config(cfg, ["Ludeon.RimWorld", "foo.bar", "foo.bar"], None, "1.6.1 rev1", bk) is None
        act, known, ver = read_mods_config(cfg)
        assert act == ["ludeon.rimworld", "foo.bar"] and known == list(DLCS) and ver == "1.6.1 rev1"
        with open(os.path.join(cfg, "ModsConfig.xml"), "rb") as f:
            assert f.read(3) == b"\xef\xbb\xbf"
        assert write_mods_config(cfg, ["ludeon.rimworld", "foo.bar"], None, "1.6.1 rev1", bk) is None  # unchanged
        b1 = write_mods_config(cfg, None, ["ludeon.rimworld.royalty"], "", bk, keep=2)
        assert b1 and os.path.isfile(b1) and read_mods_config(cfg) == (["ludeon.rimworld", "foo.bar"],
                                                                        ["ludeon.rimworld.royalty"], "1.6.1 rev1")
        for i in range(3):
            write_mods_config(cfg, ["ludeon.rimworld"] + [f"m.{j}" for j in range(i)], None, "v", bk, keep=2)
        bl = list_backups(bk)
        assert len(bl) == 2, bl
        assert read_modsconfig_file(bl[0]).mods[-1].package_id == "m.0", bl   # newest backup first
        assert write_mods_config(cfg, [], None, "v", bk, keep=2) and read_mods_config(cfg)[0] == []
        restore_backup(list_backups(bk)[0], cfg, bk)
        assert read_mods_config(cfg)[0] == ["ludeon.rimworld", "m.0", "m.1"], read_mods_config(cfg)
        try:
            restore_backup(mk("junk.xml", "<x/>"), cfg, bk)
            raise AssertionError("restore of junk must fail")
        except ValueError:
            pass
        import stat   # read-only ModsConfig: clear error before any backup is taken, flag left alone
        cfg_path, nbk = os.path.join(cfg, "ModsConfig.xml"), len(list_backups(bk))
        os.chmod(cfg_path, stat.S_IREAD)
        try:
            for fn in (lambda: write_mods_config(cfg, ["ro.x"], None, "v", bk), lambda: restore_backup(list_backups(bk)[0], cfg, bk)):
                try:
                    fn()
                    raise AssertionError("read-only ModsConfig must fail")
                except PermissionError as e:
                    assert "read-only" in str(e), e
            assert len(list_backups(bk)) == nbk and not os.access(cfg_path, os.W_OK)
        finally:
            os.chmod(cfg_path, stat.S_IREAD | stat.S_IWRITE)

        # --- rml / json / txt / html / share code round trips
        ml = ModList("Test list", [ModRef("brrainz.harmony", "Harmony", "2009463077"), ModRef(CORE_ID, "Core"),
                                   ModRef("ludeon.rimworld.royalty", "Royalty"), ModRef("foo.bar", "Foo & <Bar>"),
                                   ModRef("", "Workshop only", "3333333")], game_version="1.6.1 rev1", notes="n")
        rml = os.path.join(tmp, "out", "x.rml")
        write_rml(ml, rml, "1.6.2 rev2", mods)
        raw = open(rml, "rb").read()
        assert raw.startswith(b"\xef\xbb\xbf<?xml") and b"\r\n\t<meta>\r\n\t\t<gameVersion>1.6.2 rev2" in raw
        r = read_rml(rml)
        assert [x.package_id for x in r.mods] == ["brrainz.harmony", CORE_ID, "ludeon.rimworld.royalty", "foo.bar"]
        assert r.mods[0].workshop_id == "2009463077" and r.mods[2].workshop_id == "" and r.mods[3].name == "Foo & <Bar>"
        assert r.game_version == "1.6.2 rev2" and r.name == "x"
        assert b"<li>1149640</li>" in raw and b"<li>123456789</li>" in raw   # DLC appid, installed mod's PublishedFileId
        import dataclasses   # stale '_steam' ids: RimWorld's "Load mod list" matches ids exactly
        ws_only = {"foo.bar": dataclasses.replace(mods["foo.bar_steam"], uid="foo.bar")}
        write_rml(ModList("s", [ModRef("foo.bar_steam", "F", "555555"), ModRef("gone.x_steam", "G")]), rml, "", ws_only)
        assert [x.package_id for x in read_rml(rml).mods] == ["foo.bar", "gone.x"]
        write_rml(ModList("s", [ModRef("foo.bar_steam", "F", "555555")]), rml, "", mods)   # both copies: keep '_steam'
        assert [x.package_id for x in read_rml(rml).mods] == ["foo.bar_steam"]
        jp = os.path.join(tmp, "out", "x.json")
        save_modlist_json(ml, jp)
        j = load_modlist_json(jp)
        assert j.to_dict() == ml.to_dict(), (j, ml)
        odd = ModList("Odd ids", [ModRef("k.psitech", "PsiTech"), ModRef("bfs.programmerlily.com", "BFS", "2877990593"),
                                  ModRef("nopidfolder", "Folder-name id")])   # real ids the free-text parser rejects
        save_modlist_json(odd, os.path.join(tmp, "out", "odd.json"))
        assert load_modlist_json(os.path.join(tmp, "out", "odd.json")).to_dict() == odd.to_dict()
        write_html_list(odd, os.path.join(tmp, "out", "odd.html"))
        assert import_any(os.path.join(tmp, "out", "odd.html")).mods == odd.mods
        assert import_any(jp).to_dict() == ml.to_dict()
        tp = os.path.join(tmp, "out", "x.txt")
        write_text_list(ml, tp)
        t = import_any(tp)
        assert [(x.package_id, x.workshop_id) for x in t.mods] == [(x.package_id, x.workshop_id) for x in ml.mods]
        assert t.mods[0].name == "Harmony" and t.mods[3].name == "Foo & <Bar>"
        write_text_list(ModList("n", [ModRef("brrainz.harmony", "", "2009463077")]), tp)   # nameless ref: one entry
        assert import_any(tp).mods == [ModRef("brrainz.harmony", "", "2009463077")], import_any(tp).mods
        old_txt = "brrainz.harmony | brrainz.harmony | https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077"
        assert parse_text_list(old_txt) == [ModRef("brrainz.harmony", "", "2009463077")]   # pre-fix exports
        hp = os.path.join(tmp, "out", "x.html")
        write_html_list(ml, hp)
        assert import_any(hp).to_dict() == ml.to_dict()
        page = open(hp, encoding="utf-8").read()
        assert "steam://url/CommunityFilePage/2009463077" in page and "openBatch" in page and "Foo &amp; &lt;Bar&gt;" in page
        code = encode_share_code(ml)
        assert code.startswith("RMM1:") and "=" not in code
        d = decode_share_code("here you go:\n" + code[:20] + "\n" + code[20:] + "\nenjoy the list")
        assert [(x.package_id, x.workshop_id, x.name) for x in d.mods] == [(x.package_id, x.workshop_id, x.name) for x in ml.mods]
        assert d.name == "Test list" and _dedupe_refs([ModRef(name=d.name)]) == []   # old decoders drop the name row
        old = lambda rows: "RMM1:" + base64.urlsafe_b64encode(zlib.compress(json.dumps(rows).encode())).decode()
        assert decode_share_code(old([["a.b", "", "A"]])).name == "Shared list"         # codes without a name row
        crafted = decode_share_code(old([["", "", "Evil\x01\n list"], ["some.mod\x01", "1234567", "Nice \x02 Mod"]]))
        assert crafted.name == "Evil list" and [(x.package_id, x.workshop_id) for x in crafted.mods] == [("", "1234567")]
        ET.fromstring(_modsconfig_bytes(["a.b\x01"], ["c\ud800"], "1.6\x02"))           # writers drop XML-forbidden chars
        write_rml(ModList("c", [ModRef("x.y", "Nice \x02 Mod￾")]), rml, "1.6", {})
        assert [x.name for x in read_rml(rml).mods] == ["Nice  Mod"] and ET.fromstring(open(rml, "rb").read()) is not None
        for bad in ("hello", "RMM1:!!!", "RMM1:" + base64.urlsafe_b64encode(zlib.compress(b'{"a":1}')).decode()):
            try:
                decode_share_code(bad)
                raise AssertionError(bad)
            except ValueError:
                pass

        # --- text parsing
        refs = parse_text_list("""# comment
            https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077&searchtext=
            steam://url/CommunityFilePage/818773962, 1234 v1.2 About.xml
            - Vanilla Expanded Framework https://steamcommunity.com/workshop/filedetails/?id=2023507013
            1. [RimHUD](https://steamcommunity.com/sharedfiles/filedetails/?id=1508850027) packageid: jaxe.rimhud
            brrainz.harmony 2009463077
            Ludeon.RimWorld ludeon.rimworld.royalty 99999
            steam://openurl/https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077
        """)
        got = [(x.package_id, x.workshop_id, x.name) for x in refs]
        assert got == [("brrainz.harmony", "2009463077", ""), ("", "818773962", ""),
                       ("", "2023507013", "Vanilla Expanded Framework"), ("jaxe.rimhud", "1508850027", "RimHUD"),
                       (CORE_ID, "", ""), ("ludeon.rimworld.royalty", "", ""), ("", "99999", "")], got
        assert _as_pid("e.g") == "" and _as_pid("1.6") == "" and _as_pid("Mlie.Foo_Bar-2") == "mlie.foo_bar-2"
        assert _as_pid("About.xml") == "" and _as_pid("cgf1.gunsgalore.x.rws") == "cgf1.gunsgalore.x.rws"
        assert parse_text_list("Guns | cgf1.gunsgalore.x.json | ")[0].package_id == "cgf1.gunsgalore.x.json"

        # --- saves, ModsConfig-like xml, RimSort JSON
        sv = mk("s.rws", '\ufeff<?xml version="1.0" encoding="utf-8"?>\n<savegame><meta><gameVersion>1.6.1 rev1</gameVersion>'
            "<modIds><li>ludeon.rimworld</li><li>brrainz.harmony</li></modIds><modSteamIds><li>0</li><li>2009463077</li>"
            "</modSteamIds><modNames><li>Core</li><li>Harmony</li></modNames></meta><game>" + "<x>1</x>" * 1000 + "</game>")
        s = import_any(sv)
        assert [(x.package_id, x.workshop_id, x.name) for x in s.mods] == [(CORE_ID, "", "Core"),
                                                                            ("brrainz.harmony", "2009463077", "Harmony")]
        assert s.game_version == "1.6.1 rev1"
        with gzip.open(os.path.join(tmp, "g.rws"), "wb") as f:
            f.write(open(sv, "rb").read())
        assert len(read_save_meta(os.path.join(tmp, "g.rws")).mods) == 2
        broken_save = mk("b.rws", "<savegame><meta><gameVersion>1</gameVersion><modIds><li>a.b</li></modIds>"
                         "<modNames><li>A & B</li></modNames></meta><game>&&&")
        assert [(x.package_id, x.name) for x in read_save_meta(broken_save).mods] == [("a.b", "A & B")]
        mc = import_any(mk("rimpy.xml", "<?xml version='1.0'?><ModsConfigData><version>1.6</version><activeMods>"
                                        "<li>Brrainz.Harmony</li><li>ludeon.rimworld</li></activeMods></ModsConfigData>"))
        assert [x.package_id for x in mc.mods] == ["brrainz.harmony", CORE_ID] and mc.game_version == "1.6"
        rs = import_any(mk("rimsort.json", json.dumps({
            "version": "1.6", "activeMods": [CORE_ID, "brrainz.harmony"], "knownExpansions": [],
            "rimsortSnapshot": {"activeMods": [{"packageId": "ludeon.rimworld", "name": "RimWorld", "publishedFileId": None},
                                               {"packageId": "brrainz.harmony", "name": "Harmony", "publishedFileId": "2009463077"}],
                                "inactiveMods": [{"packageId": f"x.m{i}", "name": "x"} for i in range(9)]}})))
        assert [(x.package_id, x.workshop_id, x.name) for x in rs.mods] == [(CORE_ID, "", "RimWorld"),
                                                                             ("brrainz.harmony", "2009463077", "Harmony")]
        assert import_any(mk("ids.json", '["2009463077", "jaxe.rimhud"]')).mods[1].package_id == "jaxe.rimhud"
        assert import_any(mk("code.txt", code)).mods[0].package_id == "brrainz.harmony"
        assert len(import_any(mk("lo.json", '{"load_order": ["brrainz.harmony", "ludeon.rimworld"]}')).mods) == 2
        u16 = mk("u16.json", '\ufeff{"format": "rmm-modlist", "mods": [{"package_id": "brrainz.harmony"}], "name": "Caf\xe9"}'.encode("utf-16-le"), True)
        assert import_any(u16).name == "Caf\xe9"
        assert load_modlist_json(mk("ansi.json", '{"format": "rmm-modlist", "mods": [{"package_id": "brrainz.harmony"}], "name": "Caf\xe9"}'.encode("cp1252"), True)).name == "Caf\xe9"
        deep = "[" * 100000 + "]" * 100000
        assert import_any(mk("deep.html", f'<script id="rmm-modlist">{deep}</script> 2009463077')).mods
        for bad in (lambda: load_modlist_json(mk("deep.json", deep)),
                    lambda: decode_share_code("RMM1:" + base64.urlsafe_b64encode(zlib.compress(deep.encode())).decode())):
            try:
                bad()
                raise AssertionError("deep json")
            except ValueError:
                pass
        for bad in (mk("empty.txt", "nothing here"), mk("empty.json", '{"a": 1}')):
            try:
                import_any(bad)
                raise AssertionError(bad)
            except ValueError:
                pass
        assert mod_to_ref(fb) == ModRef("foo.bar", fb.name, "123456789")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    _selftest()
    print("mods selftest ok")
