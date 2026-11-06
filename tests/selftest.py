"""Run every module's _selftest() (and _selftest_online() with --online); PASS/FAIL per module, exit code 1 on failure.

    python tests/selftest.py [--online] [module ...]      e.g.  python tests/selftest.py core mods
The runner lives in main.py so the frozen exe can run it too (`RimModManager.exe --selftest`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import run_selftests  # noqa: E402

if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(run_selftests("--online" in args, [a for a in args if not a.startswith("-")]))
