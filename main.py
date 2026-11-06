"""RimWorld Mod Manager entry point.

    python main.py                               -> GUI
    main.exe --steamworks '<json>'               -> Steamworks worker subprocess (no GUI)
    main.exe --workshop-browser <args.json>      -> Steam Workshop browser window (subprocess)
    main.exe --selftest [--online] [module ...]  -> run the modules' self-tests; exit code 1 on any failure
"""
from __future__ import annotations

import gc
import importlib
import sys
import time
import traceback

SELFTEST_MODULES = [
    "rmm.config", "rmm.paths", "rmm.mods", "rmm.database", "rmm.steam", "rmm.browser", "rmm.textures", "rmm.sorting",
    "rmm.ai", "rmm.core", "rmm.supporter",
    "rmm.gui.theme", "rmm.gui.widgets", "rmm.gui.app", "rmm.gui.mods_tab", "rmm.gui.modlists_tab", "rmm.gui.workshop_tab",
    "rmm.gui.ai_tab", "rmm.gui.downloads_tab", "rmm.gui.tools_tab", "rmm.gui.settings_tab",
]
APP_TITLE = "RimWorld Mod Manager"


class _Tee:
    """Minimal text stream writing to several streams (console + report file)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text: str) -> int:
        for st in self.streams:
            st.write(text)
        return len(text)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def _ensure_console() -> None:
    """A --windowed exe has no stdout: write the self-test report to %TEMP%/rmm_selftest.txt and, when started
    from a console, show it there too."""
    if sys.stdout is None:
        import os
        import tempfile
        path = os.path.join(tempfile.gettempdir(), "rmm_selftest.txt")
        streams = [open(path, "w", encoding="utf-8", errors="replace")]
        try:
            import ctypes
            if ctypes.windll.kernel32.AttachConsole(-1):
                streams.append(open("CONOUT$", "w", encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - no console: the file and the exit code still report the result
            pass
        sys.stdout = sys.stderr = _Tee(*streams)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # cp1252 consoles must not crash on mod names
        except Exception:  # noqa: BLE001
            pass


def run_selftests(online: bool = False, only: list[str] | None = None) -> int:
    """Import each module and run its _selftest() (+ _selftest_online() when online). Returns the exit code."""
    _ensure_console()
    names = [n for n in SELFTEST_MODULES if not only or n in only or n.rsplit(".", 1)[-1] in only]
    unknown = [o for o in only or [] if not any(o in (n, n.rsplit(".", 1)[-1]) for n in SELFTEST_MODULES)]
    for o in unknown:
        print(f"FAIL  {o}  (unknown module; choose from {', '.join(n.rsplit('.', 1)[-1] for n in SELFTEST_MODULES)})")
    failed = len(unknown)
    start = time.perf_counter()
    # isolate the tests from the user's real %APPDATA%\RimModManager (app.log, settings, db): config resolves
    # APP_DIR from RMM_APP_DIR at import, so set it before any rmm import. --online may read the saved API key
    # read-only via RMM_REAL_SETTINGS (a portable.txt install keeps it elsewhere: the AI test then just skips).
    import os
    import shutil
    import tempfile
    if online:
        os.environ.setdefault("RMM_REAL_SETTINGS", os.path.join(os.environ.get("APPDATA", ""), "RimModManager", "settings.json"))
    tmp = None if "rmm.config" in sys.modules else tempfile.mkdtemp(prefix="rmm_selftest_")
    if tmp:
        os.environ["RMM_APP_DIR"] = tmp
    try:
        failed += _run_modules(names, online)
    finally:
        if tmp:
            import logging
            log = logging.getLogger("rmm")
            for h in list(log.handlers):   # Windows: an open app.log would block the rmtree
                h.close()
                log.removeHandler(h)
            os.environ.pop("RMM_APP_DIR", None)
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"{len(names)} module(s), {failed} failure(s), {time.perf_counter() - start:.1f}s")
    return 1 if failed else 0


def _run_modules(names: list[str], online: bool) -> int:
    """Run the self-tests of the given modules; returns the number of failures."""
    failed = 0
    for name in names:
        t = time.perf_counter()
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a broken module is a failure, not a crash of the runner
            failed += 1
            print(f"FAIL  {name}  (import, {time.perf_counter() - t:.2f}s)")
            traceback.print_exc(file=sys.stdout)
            continue
        tests = [("_selftest", "")] + ([("_selftest_online", " [online]")] if online else [])
        for fn_name, label in tests:
            fn = getattr(mod, fn_name, None)
            if fn is None:
                if fn_name == "_selftest":
                    print(f"SKIP  {name}  (no _selftest)")
                continue
            gc.collect()        # free the previous test's Tk objects here, not later in some worker thread
            t = time.perf_counter()
            try:
                fn()
                print(f"PASS  {name}{label}  ({time.perf_counter() - t:.2f}s)")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL  {name}{label}  ({time.perf_counter() - t:.2f}s)")
                traceback.print_exc(file=sys.stdout)
            if sys.stdout is not None:
                sys.stdout.flush()
    return failed


def _fatal(message: str, tb: str) -> None:
    try:
        from rmm.config import get_logger
        get_logger().error("%s\n%s", message, tb)
    except Exception:  # noqa: BLE001
        pass
    if sys.stderr is not None:
        print(message, tb, sep="\n", file=sys.stderr)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{message}\n\n{tb[-1500:]}", APP_TITLE, 0x10)  # MB_ICONERROR
    except Exception:  # noqa: BLE001
        pass


def _run_gui() -> int:
    try:
        from rmm.gui.app import main as gui_main
    except Exception:  # noqa: BLE001 - missing tkinter / broken GUI module: tell the user instead of vanishing
        _fatal("The user interface could not be loaded.", traceback.format_exc())
        return 1
    try:
        return int(gui_main() or 0)
    except Exception:  # noqa: BLE001
        _fatal(f"{APP_TITLE} crashed.", traceback.format_exc())
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--steamworks"]:
        from rmm.steam import steamworks_worker_main
        return int(steamworks_worker_main(argv[1] if len(argv) > 1 else "{}") or 0)
    if argv[:1] == ["--workshop-browser"] and len(argv) > 1:
        from rmm.browser import browser_main
        return int(browser_main(argv[1]) or 0)
    if argv[:1] == ["--selftest"]:
        rest = argv[1:]
        return run_selftests("--online" in rest, [a for a in rest if not a.startswith("-")])
    return _run_gui()


if __name__ == "__main__":
    sys.exit(main())
