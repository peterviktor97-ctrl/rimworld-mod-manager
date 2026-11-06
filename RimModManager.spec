# PyInstaller spec for dist\RimModManager.exe (single file, no console). Run by build.bat.
import re
import sys

if SPECPATH not in sys.path:
    sys.path.insert(0, SPECPATH)

from PyInstaller.utils.hooks import collect_submodules

a = Analysis(
    ['main.py'],
    # rmm modules are imported lazily (self-tests, GUI tabs) and anthropic only inside rmm/ai.py: bundle them explicitly
    hiddenimports=collect_submodules('rmm') + collect_submodules('anthropic'),
    excludes=['unittest', 'doctest', 'pdb', 'pydoc', 'pydoc_data', 'test', 'idlelib', 'turtle', 'turtledemo',
              'ensurepip', 'venv', 'lib2to3', 'curses', 'xmlrpc', 'setuptools', 'pkg_resources'],
)
# Every launch (and every Steamworks helper process) unpacks the whole bundle, so drop Tcl data the app never loads
# (784 of 1031 files, ~0.5 s of startup): time zones and message catalogs (used only by Tcl's `clock`), and encodings
# other than the ones Tcl/Tk ask for on Windows (system/font codepages cp<N>, ascii, iso8859-1, symbol, dingbats, macRoman).
_unused = re.compile(r'_tcl_data/(tzdata|msgs)/|_tcl_data/encoding/(?!(cp\d+|ascii|iso8859-1|symbol|dingbats|macRoman)\.enc$)')
a.datas = [d for d in a.datas if not _unused.match(d[0].replace('\\', '/'))]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='RimModManager', console=False, icon='assets/icon.ico')
