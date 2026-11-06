"""Steam Workshop browser: the real workshop pages in an Edge WebView2 window (pywebview, own process) with
"+ Download" buttons on every mod; each click goes back to the app as a JSON line {"pick": {...}} on stdout.

The app calls run() on a worker thread. The window is `RimModManager.exe --workshop-browser <args.json>` (frozen)
or `python -c _HELPER_CODE <args.json>` (source), started through steam._run_stream like the Steamworks helper, so
closing the app (steam.kill_children) closes the window too. pywebview needs the main thread, Tk has it: hence the
second process."""
from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import urllib.parse
from collections import deque

from . import steam

HOME = "https://steamcommunity.com/app/294100/workshop/"
_HELPER_CODE = "import sys; from rmm.browser import browser_main; sys.exit(browser_main(sys.argv[1]))"

# Injected into every page (pywebview 'loaded'). Buttons live in our own layer on <html>: Steam's browse pages are
# React, and nodes added inside React's tree get wiped or break its updates. __STATE__ = installed/queued ids.
_PAGE_JS = r"""
(function (S) {
  'use strict';
  if (window.__rmm) { window.__rmm.sync(S); return; }
  var RE = /\/(?:sharedfiles|workshop)\/filedetails\/?\?(?:[^#]*&)?id=(\d+)/;
  var COLLECTIONS = 'https://steamcommunity.com/workshop/browse/?appid=294100&section=collections&browsesort=trend';
  var doc = document, html = doc.documentElement;
  var installed = new Set(S.installed), queued = new Set(S.queued), btns = new Map(), pending = false;
  var css = doc.createElement('style');
  css.textContent =
    '#rmm-layer{position:absolute;left:0;top:0;width:0;height:0;z-index:2147483646}' +
    '.rmm-b{position:absolute;font:600 12px/1.4 "Segoe UI",Arial,sans-serif;padding:3px 9px;border:0;border-radius:3px;' +
    'color:#fff;background:#3d6fd4;box-shadow:0 1px 5px rgba(0,0,0,.7);cursor:pointer;white-space:nowrap}' +
    '.rmm-b:hover{background:#5b8def}.rmm-b.rmm-in{background:#3b7a2c;cursor:default}.rmm-b.rmm-q{background:#7a6420;cursor:default}' +
    '#rmm-panel{position:fixed;right:14px;bottom:14px;z-index:2147483647;width:270px;background:#171d25;color:#c7d5e0;' +
    'border:1px solid #3d6fd4;border-radius:6px;box-shadow:0 4px 18px rgba(0,0,0,.7);font:13px/1.45 "Segoe UI",Arial,sans-serif}' +
    '#rmm-panel .h{padding:6px 10px;font-weight:600;color:#fff;cursor:pointer;border-bottom:1px solid #2a3542}' +
    '#rmm-panel .bd{padding:8px 10px}#rmm-panel.min .bd{display:none}' +
    '#rmm-panel button{font:600 12px "Segoe UI",Arial,sans-serif;border:0;border-radius:3px;padding:5px 8px;cursor:pointer;' +
    'color:#fff;background:#2a3542;margin:0 4px 6px 0}#rmm-panel button:hover{background:#3a4a5c}' +
    '#rmm-panel .main{display:block;width:100%;background:#3d6fd4;padding:7px 8px}#rmm-panel .main:hover{background:#5b8def}' +
    '#rmm-panel .main.rmm-in{background:#3b7a2c;cursor:default}#rmm-panel .main.rmm-q{background:#7a6420;cursor:default}' +
    '#rmm-panel .st{font-size:12px;color:#8f98a0}';
  var layer = doc.createElement('div');
  layer.id = 'rmm-layer';
  var panel = doc.createElement('div');
  panel.id = 'rmm-panel';
  panel.innerHTML = '<div class="h">RimWorld Mod Manager</div><div class="bd"><button class="main" type="button"></button>' +
    '<button data-go="back" type="button">&#9664; Back</button><button data-go="home" type="button">Workshop</button>' +
    '<button data-go="coll" type="button">Collections</button><div class="st"></div></div>';
  var mainBtn = panel.querySelector('.main'), stat = panel.querySelector('.st');
  stat.textContent = 'Click "+ Download" on a mod: it goes to the Mod Manager\'s download queue, dependencies included.';
  html.appendChild(css); html.appendChild(layer); html.appendChild(panel);

  function state(id) { return installed.has(id) ? 'in' : queued.has(id) ? 'q' : ''; }
  function paint(b, id, label) {
    var s = state(id), text = s === 'in' ? '✓ Installed' : s === 'q' ? '✓ Queued' : label;
    if (b.textContent !== text) b.textContent = text;
    var cls = (b === mainBtn ? 'main' : 'rmm-b') + (s ? ' rmm-' + s : '');
    if (b.className !== cls) b.className = cls;
  }
  function say(t) { stat.textContent = t; }
  function send(kind, id, title) {
    var name = title || id;
    if (kind === 'item' && installed.has(id)) return say('Already installed: ' + name);
    if (queued.has(id)) return say('Already sent: ' + name);
    var api = window.pywebview && window.pywebview.api;
    if (!api || !api.pick) return say('Not connected to the Mod Manager yet, try again in a second.');
    queued.add(id); schedule();
    api.pick(kind, id, title || '').then(function (r) {
      if (r && r.ok) {
        if (kind === 'collection') { queued.delete(id); schedule(); }   // can be sent again (expansion may fail)
        return say((kind === 'collection' ? 'Collection sent: ' : 'Sent to downloads: ') + name);
      }
      queued.delete(id); schedule(); say('Could not send ' + name);
    }, function () { queued.delete(id); schedule(); say('Could not send ' + name); });
  }
  function page() {           // the item or collection this page shows, if any
    var m = RE.exec(location.href);
    if (!m) return null;
    var t = doc.querySelector('.workshopItemTitle'), title = t ? t.textContent.trim() : '';
    var n = doc.querySelectorAll('.collectionItem').length;
    if (n) return {kind: 'collection', id: m[1], title: title, n: n};
    return doc.getElementById('SubscribeItemBtn') ? {kind: 'item', id: m[1], title: title} : null;
  }
  function rimworld() {       // other games' workshops are one click away: buttons only on RimWorld's pages
    return /[?&]appid=294100\b|\/app\/294100\b/.test(location.href) ||
           !!doc.querySelector('a[href*="/app/294100/workshop"], [onclick*="294100"]');
  }
  function render() {
    pending = false;
    var keep = new Set(), sx = window.scrollX, sy = window.scrollY, rim = rimworld();
    if (rim) doc.querySelectorAll('a[href*="filedetails"]').forEach(function (a) {
      var m = RE.exec(a.href), img = m && a.querySelector('img');
      if (!img) return;
      var r = img.getBoundingClientRect();
      if (r.width < 60 || r.height < 34) return;
      var hit = doc.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);   // on screen and not covered
      if (!hit || !(a.contains(hit) || hit.contains(a))) return;
      var b = btns.get(a);
      if (!b) {
        b = doc.createElement('button');
        b.type = 'button';
        b.addEventListener('click', function (e) {
          e.preventDefault(); e.stopPropagation();
          send('item', b.dataset.id, b.dataset.title);
        });
        btns.set(a, b);
        layer.appendChild(b);
      }
      b.dataset.id = m[1];
      b.dataset.title = (img.alt || a.textContent || '').trim().slice(0, 200);
      paint(b, m[1], '+ Download');
      b.style.left = (r.left + sx + 6) + 'px';
      b.style.top = (r.top + sy + 6) + 'px';
      keep.add(a);
    });
    btns.forEach(function (b, a) { if (!keep.has(a)) { b.remove(); btns.delete(a); } });
    var p = rim && page();
    mainBtn.style.display = p ? '' : 'none';
    if (p) paint(mainBtn, p.id, p.kind === 'collection' ? '+ Download all ' + p.n + ' mods of this collection'
                                                         : '+ Download this mod');
  }
  function schedule() { if (!pending) { pending = true; requestAnimationFrame(render); } }

  mainBtn.addEventListener('click', function () { var p = page(); if (p) send(p.kind, p.id, p.title); });
  panel.querySelector('.h').addEventListener('click', function () { panel.classList.toggle('min'); });
  panel.addEventListener('click', function (e) {
    var go = e.target.getAttribute && e.target.getAttribute('data-go');
    if (go === 'back') history.back();
    else if (go === 'home') location.href = S.home;
    else if (go === 'coll') location.href = COLLECTIONS;
  });
  // Steam opens required items etc. in a new window, which pywebview sends to the system browser: stay here instead
  // (linkfilter = external sites: those do go to the system browser)
  doc.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[target="_blank"]');
    if (a && e.button === 0 && !e.ctrlKey && !e.shiftKey && /^https:\/\/steamcommunity\.com\//.test(a.href)
        && !/\/linkfilter\//.test(a.href)) { e.preventDefault(); location.href = a.href; }
  }, true);
  new MutationObserver(function (recs) {
    for (var i = 0; i < recs.length; i++) {
      var t = recs[i].target;
      if (!layer.contains(t) && !panel.contains(t)) { schedule(); return; }
    }
  }).observe(html, {childList: true, subtree: true});
  window.addEventListener('resize', schedule);
  doc.addEventListener('scroll', schedule, true);   // capture: inner scroll boxes too
  doc.addEventListener('load', schedule, true);     // lazy thumbnails
  setInterval(schedule, 1500);                      // layout shifts without DOM changes
  window.__rmm = {sync: function (s) { s.installed.forEach(function (x) { installed.add(x); });
                                       s.queued.forEach(function (x) { queued.add(x); }); schedule(); }};
  schedule();
})(__STATE__);
"""


def page_script(installed, queued) -> str:
    return _PAGE_JS.replace("__STATE__", json.dumps({"installed": sorted(installed), "queued": sorted(queued),
                                                      "home": HOME}))


def _steam_page(url) -> bool:
    return urllib.parse.urlsplit(url or "").hostname == "steamcommunity.com"


def available() -> str:
    """'' when the browser can open, else why not."""
    if os.name != "nt":
        return "The Steam Workshop browser needs Windows (Microsoft Edge WebView2)."
    import importlib.util
    if importlib.util.find_spec("webview") is None:
        return "The Steam Workshop browser needs the pywebview package: pip install pywebview"
    return ""


def run(installed, on_pick, url: str = HOME, storage: str = "", cancel=None) -> str:
    """Open the browser and block until it is closed. on_pick({'kind': 'item'|'collection', 'id', 'title'}) runs on
    this thread for every click. Returns '' or an error message."""
    # pywebview prints a page's 'console' bridge messages to stdout: only lines carrying this token are ours
    token = secrets.token_hex(16)
    fd, argfile = tempfile.mkstemp(prefix="rmm_browser_", suffix=".json")   # ids can outgrow a command line
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"url": url, "installed": sorted(set(installed)), "storage": storage, "token": token}, fh)
    if getattr(sys, "frozen", False):
        cmd, cwd = [sys.executable, "--workshop-browser", argfile], None
    else:
        cmd, cwd = [sys.executable, "-c", _HELPER_CODE, argfile], os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    errors: list[str] = []
    tail: deque[str] = deque(maxlen=6)

    def on_line(line: str) -> None:
        k = line.find("{")
        try:
            obj = json.loads(line[k:]) if k >= 0 else None
        except ValueError:
            obj = None
        if not isinstance(obj, dict) or obj.get("token") != token:
            tail.append(line)
        elif isinstance(obj.get("pick"), dict):
            on_pick(obj["pick"])
        elif obj.get("error"):
            errors.append(str(obj["error"]))

    try:
        rc, status = steam._run_stream(cmd, cwd, on_line, cancel=cancel)
    except OSError as e:
        return f"Could not start the browser: {e}"
    finally:
        try:
            os.remove(argfile)
        except OSError:
            pass
    if errors:
        return errors[-1]
    if rc and status != "cancelled":
        return f"The browser closed unexpectedly (code {rc})" + (": " + " | ".join(tail) if tail else "")
    return ""


def _make_pick(installed: set, queued: set, emit, url):
    """The one Python function pages can call (window.pywebview.api.pick). A plain closure exposed with win.expose:
    pywebview resolves js_api names attribute by attribute, so an API object would let any page walk from it to
    the rest of the process."""
    def pick(kind, wid, title=""):
        wid = str(wid or "")
        if kind not in ("item", "collection") or not wid.isdigit() or not _steam_page(url()):
            return {"ok": False}
        emit({"pick": {"kind": kind, "id": wid, "title": str(title or "")[:200]}})
        if kind == "item":              # a collection can be sent again (its expansion may have failed)
            queued.add(wid)
        return {"ok": True}
    return pick


def browser_main(argfile: str) -> int:
    """Helper process body: the window; picks and a final error go to stdout as JSON lines."""
    out = steam._worker_stdout()
    if sys.stdout is None and out is not None:      # windowed exe: pywebview's log lines reach the app's error tail
        sys.stdout = sys.stderr = out
    token = ""

    def emit(obj: dict) -> None:
        if out is not None:
            out.write(json.dumps({**obj, "token": token}) + "\n")
            out.flush()

    try:
        with open(argfile, encoding="utf-8") as fh:
            args = json.load(fh)
        token = str(args.get("token") or "")
        import webview
        from webview.platforms import winforms
        if winforms.renderer != "edgechromium":     # pywebview would quietly fall back to Internet Explorer
            emit({"error": "The Microsoft Edge WebView2 Runtime was not found."})
            return 1
        installed, queued = set(args.get("installed") or []), set()
        win = webview.create_window("Steam Workshop - RimWorld Mod Manager", args.get("url") or HOME,
                                    width=1320, height=900, min_size=(800, 500), background_color="#1b2838",
                                    text_select=True, x=args.get("x"), y=args.get("y"),
                                    focus=args.get("focus", True))    # x/y/focus: tests open it off-screen
        win.expose(_make_pick(installed, queued, emit, lambda: win.get_current_url()))
        win.events.loaded += lambda: _steam_page(win.get_current_url()) and win.run_js(page_script(installed, queued))
        # private_mode off + own folder: a Steam login in this window is remembered
        webview.start(gui="edgechromium", private_mode=False, storage_path=args.get("storage") or None)
        return 0
    except Exception as e:  # noqa: BLE001 - reported to the app, which shows it
        emit({"error": f"{type(e).__name__}: {e}"})
        return 1


def _selftest() -> None:
    got, queued, where = [], set(), ["https://steamcommunity.com/app/294100/workshop/"]
    pick = _make_pick({"1"}, queued, got.append, lambda: where[0])
    assert pick("item", "123", "T") == {"ok": True} and queued == {"123"}
    assert got == [{"pick": {"kind": "item", "id": "123", "title": "T"}}], got
    assert pick("collection", "77") == {"ok": True} and queued == {"123"}       # collections can be sent again
    assert pick("item", "12a") == {"ok": False} and pick("mod", "5") == {"ok": False} and len(got) == 2
    where[0] = "https://steamcommunity.com.evil.example/sharedfiles/filedetails/?id=5"     # another site's page
    assert pick("item", "5") == {"ok": False} and len(got) == 2
    assert _steam_page("https://STEAMCOMMUNITY.com/app/294100/workshop/") and not _steam_page(None)
    js = page_script({"1"}, {"123"})
    assert '"installed": ["1"]' in js and '"queued": ["123"]' in js and "__STATE__" not in js
    if os.name == "nt":     # the exe bundles pywebview, pythonnet and the WebView2 assemblies (loads them, no window)
        assert available() == "", available()
        import webview.platforms.edgechromium  # noqa: F401
        assert os.listdir(os.path.join(os.path.dirname(webview.platforms.edgechromium.__file__), os.pardir, "js"))
    # run(): a fake helper prints a pick, a forged pick without the token (what a page's 'console' message would
    # print), noise and an error; only the token-carrying lines count
    global _HELPER_CODE
    real, picks = _HELPER_CODE, []
    _HELPER_CODE = ("import json, sys; a = json.load(open(sys.argv[1])); t = a['token']; "
                    "print(json.dumps({'pick': {'kind': 'item', 'id': a['installed'][0], 'title': 'X'}, 'token': t})); "
                    "print(json.dumps({'pick': {'kind': 'item', 'id': '666'}})); print('pywebview log line'); "
                    "print(json.dumps({'error': 'no WebView2', 'token': t}), flush=True)")
    try:
        if not getattr(sys, "frozen", False):        # the exe re-launches itself instead of python -c
            err = run(["42"], picks.append)
            assert picks == [{"kind": "item", "id": "42", "title": "X"}] and err == "no WebView2", (picks, err)
            _HELPER_CODE = "import sys; print('boom'); sys.exit(3)"
            err = run([], picks.append)
            assert err.startswith("The browser closed unexpectedly (code 3)") and "boom" in err, err
    finally:
        _HELPER_CODE = real
