"""Texture optimization with todds, the encoder RimSort uses: each mod's Textures/**/*.png also as .dds, which
RimWorld 1.5+ loads natively (faster game start, less video memory). clean=True deletes the .dds files that have
a matching .png. todds.exe is downloaded from GitHub on first use into the app folder."""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import tempfile
import time
import zipfile

from . import config, steam
from .models import Progress, noop_progress

TODDS_URL = "https://github.com/todds-encoder/todds/releases/download/0.4.1/todds_Windows_0.4.1.zip"
# RimSort's presets. Optimize: BC1, BC7 for pngs with alpha, only pngs newer than their dds, flipped for Unity,
# sizes rounded to multiples of 4, only paths containing "Textures". Clean: every dds next to a png there.
OPTIMIZE_ARGS = ["-f", "BC1", "-af", "BC7", "-on", "-vf", "-fs", "-ss", "Textures", "-p"]
CLEAN_ARGS = ["-cl", "-o", "-ss", "Textures", "-p"]
_RE_PROGRESS = re.compile(r"Progress:\s*(\d+)\s*/\s*(\d+)")
_MARKER = "todds_run.json"     # exists while an optimize runs: a killed run's half-written files are removed next time


def todds_path() -> str:
    return os.path.join(config.APP_DIR, "todds", "todds.exe")


def install(progress: Progress = noop_progress, cancel=None) -> str:
    exe = todds_path()
    if os.path.isfile(exe):
        return exe
    root = os.path.dirname(exe)
    zpath = os.path.join(root, "todds.zip")
    progress(None, "Downloading todds…")
    config.download_file(TODDS_URL, zpath, progress=progress, timeout=60, cancel=cancel)
    try:            # via .part: a cut-off extraction must not leave a todds.exe that later runs would trust
        with zipfile.ZipFile(zpath) as z, z.open("todds.exe") as src, open(exe + ".part", "wb") as dst:
            shutil.copyfileobj(src, dst)          # reading to the end checks the CRC
        os.replace(exe + ".part", exe)
    finally:
        for p in (zpath, exe + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass
    return exe


def _dds_truncated(path: str) -> bool:
    """A .dds shorter than its header promises (todds' BC1/BC7 output cut off by a kill)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            h = fh.read(148)
    except OSError:
        return False
    if len(h) < 128:
        return True
    if h[:4] != b"DDS ":
        return False
    height, width, _pitch, _depth, mips = struct.unpack("<5I", h[12:32])
    fourcc = h[84:88]
    fmt = struct.unpack("<I", h[128:132])[0] if fourcc == b"DX10" and len(h) >= 132 else 0
    if fourcc == b"DXT1" or fmt in (70, 71, 72):           # BC1: 8 bytes per 4x4 block
        block = 8
    elif fourcc in (b"DXT3", b"DXT5") or fmt in (97, 98, 99):   # BC2/3, BC7: 16
        block = 16
    else:
        return False
    need, w, hh = 128 + (20 if fourcc == b"DX10" else 0), width, height
    for _ in range(max(1, mips)):
        need += max(1, (w + 3) // 4) * max(1, (hh + 3) // 4) * block
        w, hh = max(1, w // 2), max(1, hh // 2)
    return size < need


def _remove_partial(folders: list[str], since: float) -> int:
    """Delete the truncated .dds files written since `since` under folders; returns how many."""
    n = 0
    for folder in folders:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if f.lower().endswith(".dds") and os.path.getmtime(p) >= since - 2 and _dds_truncated(p):
                        os.remove(p)
                        n += 1
                except OSError:
                    pass
    return n


def recover() -> int:
    """Delete the half-written .dds files of an optimize run that was killed (app closed mid-run, crash); the app
    calls this at startup and before every run. Returns how many were removed."""
    marker = os.path.join(config.APP_DIR, _MARKER)
    if not os.path.exists(marker):
        return 0
    n = 0
    try:
        with open(marker, encoding="utf-8") as fh:
            old = json.load(fh)
        n = _remove_partial(old["folders"], float(old["start"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        os.remove(marker)
    except OSError:
        pass
    return n


def run(folders: list[str], clean: bool = False, progress: Progress = noop_progress, cancel=None) -> dict:
    """todds over the mod folders. {'done', 'total': textures encoded / to encode, 'status': '' | 'cancelled' |
    'failed (code N)', 'removed': half-written .dds files deleted after a stopped run}."""
    folders = [f for f in dict.fromkeys(os.path.abspath(f) for f in folders) if os.path.isdir(f)]
    out = {"done": 0, "total": 0, "status": "", "removed": 0}
    if not folders:
        return out
    exe = install(progress, cancel)
    out["removed"] += recover()
    marker = config.app_path(_MARKER)
    fd, listfile = tempfile.mkstemp(prefix="rmm_todds_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(folders) + "\n")
    start = time.time()
    if not clean:
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"start": start, "folders": folders}, fh)
    verb = "Removing .dds files" if clean else "Optimizing textures"
    progress(None, f"{verb} of {len(folders)} mod(s)…")

    def on_line(line: str) -> None:
        m = _RE_PROGRESS.search(line)
        if m:
            out["done"], out["total"] = int(m.group(1)), int(m.group(2))
            progress(out["done"] / max(1, out["total"]), f"{out['done']:,}/{out['total']:,} textures")

    try:
        rc, status = steam._run_stream([exe, *(CLEAN_ARGS if clean else OPTIMIZE_ARGS), listfile], None, on_line,
                                       cancel=cancel)
    finally:
        os.remove(listfile)
    out["status"] = status or (f"failed (code {rc})" if rc else "")
    if not clean:
        if out["status"]:
            progress(None, "Removing half-written files…")
            out["removed"] += _remove_partial(folders, start)
        try:
            os.remove(marker)
        except OSError:
            pass
    return out


def _selftest() -> None:
    def dds(path: str, w: int, h: int, mips: int, fourcc: bytes, payload: int, fmt: int = 0) -> None:
        head = b"DDS " + struct.pack("<7I", 124, 0, h, w, 0, 0, mips) + bytes(44) + struct.pack("<2I", 32, 4) + fourcc
        head = head.ljust(128, b"\0") + (struct.pack("<5I", fmt, 3, 0, 1, 0) if fourcc == b"DX10" else b"")
        with open(path, "wb") as fh:
            fh.write(head + bytes(payload))

    with tempfile.TemporaryDirectory() as tmp:
        tex = os.path.join(tmp, "Mod", "Textures")
        os.makedirs(tex)
        p = os.path.join(tex, "a.dds")
        dds(p, 8, 8, 4, b"DXT1", 4 * 8 + 8 + 8 + 8)              # 8x8 BC1 with mips 8,4,2,1: 4+1+1+1 blocks
        assert not _dds_truncated(p)
        dds(p, 8, 8, 4, b"DXT1", 50)
        assert _dds_truncated(p)
        dds(p, 4, 4, 1, b"DX10", 16, fmt=98)                     # BC7: 16 bytes per block, 20-byte DX10 header
        assert not _dds_truncated(p) and os.path.getsize(p) == 128 + 20 + 16
        dds(p, 4, 4, 1, b"DX10", 15, fmt=98)
        assert _dds_truncated(p)
        dds(p, 4, 4, 1, b"\0\0\0\0", 0)                          # uncompressed / unknown: never touched
        assert not _dds_truncated(p)
        q = os.path.join(tex, "b.dds")
        dds(q, 8, 8, 1, b"DXT1", 3)
        old = os.path.join(tex, "c.dds")
        dds(old, 8, 8, 1, b"DXT1", 3)
        os.utime(old, (1, 1))                                    # truncated but older than the run: not ours
        assert _remove_partial([os.path.join(tmp, "Mod")], time.time()) == 1
        assert os.path.exists(p) and not os.path.exists(q) and os.path.exists(old)
        os.makedirs(config.APP_DIR, exist_ok=True)              # the self-test runner's temp app dir
        with open(os.path.join(config.APP_DIR, _MARKER), "w", encoding="utf-8") as fh:   # a killed run's marker
            json.dump({"start": time.time() - 5, "folders": [os.path.join(tmp, "Mod")]}, fh)
        dds(q, 8, 8, 1, b"DXT1", 3)
        assert recover() == 1 and not os.path.exists(q) and recover() == 0
    m = _RE_PROGRESS.search("\rProgress: 1342/31596")
    assert m and m.groups() == ("1342", "31596")
