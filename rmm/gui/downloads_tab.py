"""Downloads tab: download queue + live log, update checks, SteamCMD management.

Handles the 'queue_download' event that App.queue_download emits (refs, method, opts): items are appended to the
queue, the tab selects itself and runs Manager.download(refs, method, **opts) on a background thread (one
method + opts group at a time; opts such as activate/update/with_deps stay with each item, so Retry keeps them),
streaming the progress lines into the log and per-item statuses, then emits 'scanned' (Manager.download already
rescanned when something got installed), 'active_changed' (activate=True) and 'downloads_done' (the results).
Queued/running/failed items are kept in download_queue.json, so the queue can be resumed after a crash or close.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

from rmm import config
from rmm.gui.widgets import not_ready, scrolled_tree, track_ready
from rmm.models import WORKSHOP_URL, DownloadResult, ModList, ModRef

METHODS: dict[str, tuple[str, str]] = {   # explanations stay one line even at the minimum window size
    "steamcmd": ("SteamCMD", "Anonymous or your account; installs into Mods; failures go via Steam."),
    "steamworks": ("Steam subscribe", "Subscribes via your running Steam client; fastest for big packs."),
    "steam_client": ("Steam client pages", "Opens each page in Steam; you click Subscribe."),
    "browser": ("Browser", "Opens workshop pages in your browser, in batches."),
}
INSTALL_MODES: dict[str, tuple[str, str]] = {
    "move": ("Move", "move the download into Mods (no duplicate files)"),
    "copy": ("Copy", "copy into Mods, keep SteamCMD's copy"),
    "link": ("Link", "junction Mods/<id> to SteamCMD's copy"),
}
LOG_MAX_LINES = 5000
OTHER_METHODS_HINT = "You can download with the “Steam subscribe” or “Browser” method above instead."

QUEUED, RUNNING, DONE, FAILED, OPENED, CANCELLED = "queued", "running", "done", "failed", "opened", "cancelled"
_STATE_TAG = {QUEUED: "dim", RUNNING: "info", DONE: "", FAILED: "error", OPENED: "warning", CANCELLED: "dim"}

_ID_RE = re.compile(r"(?<!\d)(\d{6,12})(?!\d)")
_FAIL_RE = re.compile(r"failed\s*\(([^)]*)\)", re.I)


def status_from_line(line: str) -> str:
    """Short per-item status for a progress line that mentions the item."""
    low = line.lower()
    m = _FAIL_RE.search(line)
    if m:
        return f"Error: {m.group(1)}"
    if "success" in low:
        return "Downloaded"
    for words, status in ((("error", "fail"), "Error"), (("retry",), "Retrying…"), (("subscrib",), "Subscribing…"),
                          (("install", "moving", "copying", "link"), "Installing…"), (("download",), "Downloading…"),
                          (("open",), "Opening…")):
        if any(w in low for w in words):
            return status
    return "Working…"


def final_status(res: DownloadResult, method: str) -> tuple[str, str]:
    """(state, status text) for a finished item."""
    if res.ok:
        return DONE, "✔ Installed" if method == "steamcmd" else "✔ Downloaded"
    msg = (res.message or "failed").splitlines()[0]     # one-line row; the full text goes to the log
    if msg.lower().startswith("cancel"):
        return CANCELLED, "Cancelled"
    if "opened" in msg.lower():
        where = "Steam" if method == "steam_client" and "browser" not in msg.lower() else "browser"
        return OPENED, f"Opened in {where}: click Subscribe"
    return FAILED, f"✖ {msg}"


QUEUE_FILE = "download_queue.json"
_RESUMABLE = (QUEUED, RUNNING, FAILED)


def save_queue(items: dict[str, dict], path: str) -> None:
    """Write the unfinished items (enough to re-enqueue them); remove the file when there are none."""
    rows = [{"workshop_id": it["ref"].workshop_id, "name": it["ref"].name, "package_id": it["ref"].package_id,
             "method": it["method"], "opts": it["opts"]}
            for it in items.values() if it["state"] in _RESUMABLE and it["ref"].workshop_id]
    if rows:
        config.atomic_write(path, json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    else:
        config._remove_quietly(path)


def load_queue(path: str) -> list[dict]:
    """Rows written by save_queue; [] when missing. A corrupt file is deleted."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        rows = None
    ok = isinstance(rows, list) and all(
        isinstance(r, dict) and str(r.get("workshop_id", "")).isdigit() and r.get("method") in METHODS
        and isinstance(r.get("opts"), dict) for r in rows)
    if not ok:
        config._remove_quietly(path)
        return []
    return rows


def _fmt_date(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


class DownloadsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app = app
        self.mgr = app.mgr
        self._items: dict[str, dict] = {}       # key (workshop id or ref key) -> {'ref','method','opts','state','status'}
        self._batch: list[str] = []             # keys in the running download
        self._run: tuple[str, dict] = ("", {})  # (method, opts) of the running download
        self._busy = ""                         # '' | 'download' | 'install'
        self._cancel = None                     # threading.Event of the running job
        self._lines: queue.Queue = queue.Queue()   # (fraction|None, text) from worker threads
        self._polling = False
        self._updates: dict[str, tuple] = {}    # workshop id -> (Mod, remote time)
        self._queue_file = config.app_path(QUEUE_FILE)
        self._resume = load_queue(self._queue_file)   # rows left from last time; not overwritten until answered
        self._saved = None                      # last rows written, to skip identical writes

        s = self.mgr.settings
        method = s.get("download_method", "steamcmd")
        self.method = tk.StringVar(value=method if method in METHODS else "steamcmd")
        mode = s.get("install_mode", "move")
        self.install_mode = tk.StringVar(value=mode if mode in INSTALL_MODES else "move")
        track_ready(self)
        self._build()
        self._apply_colors()

        app.on("queue_download", self._on_queue)
        app.on("scanned", lambda *a: self.after_idle(self._offer_resume))
        app.on("settings_changed", lambda *a: self._sync_settings())
        app.on("theme_changed", lambda *a: self.after_idle(self._apply_colors))
        self.after(300, self._refresh_steamcmd)

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x")
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)

        mf = ttk.Frame(top, style="Card.TFrame", padding=12)
        mf.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(mf, text="Download Method", style="PanelHeading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        for i, (key, (label, expl)) in enumerate(METHODS.items(), start=1):
            ttk.Radiobutton(mf, text=label, value=key, variable=self.method,
                            command=self._method_changed).grid(row=i, column=0, sticky="nw", pady=1)
            ttk.Label(mf, text=expl, style="PanelMuted.TLabel").grid(row=i, column=1, sticky="w", padx=(8, 0))

        sf = ttk.Frame(top, style="Card.TFrame", padding=12)
        sf.grid(row=0, column=1, sticky="nsew")
        ttk.Label(sf, text="SteamCMD", style="PanelHeading.TLabel").pack(anchor="w")
        self.sc_status = ttk.Label(sf, text="Checking…", style="Panel.TLabel")
        self.sc_status.pack(anchor="w")
        self.sc_path = ttk.Label(sf, text="", style="PanelMuted.TLabel")
        self.sc_path.pack(anchor="w")
        row = ttk.Frame(sf, style="Panel.TFrame")
        row.pack(anchor="w", pady=(4, 2))
        self.sc_btn = ttk.Button(row, text="Install", command=self._install_steamcmd)
        self.sc_btn.pack(side="left")
        ttk.Button(row, text="Open folder", command=self._open_steamcmd_folder).pack(side="left", padx=4)
        row = ttk.Frame(sf, style="Panel.TFrame")
        row.pack(anchor="w", pady=(4, 0))
        ttk.Label(row, text="Install mode:", style="Panel.TLabel").pack(side="left")
        for key, (label, _e) in INSTALL_MODES.items():
            ttk.Radiobutton(row, text=label, value=key, variable=self.install_mode,
                            command=self._mode_changed).pack(side="left", padx=(6, 0))
        self.mode_expl = ttk.Label(sf, text="", style="PanelMuted.TLabel")
        self.mode_expl.pack(anchor="w")
        self._update_mode_expl()

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(10, 4))
        self.btn_missing = ttk.Button(bar, text="Download Missing (Active)", style="Accent.TButton",
                                      command=self._download_missing)
        self.btn_missing.pack(side="left")
        ttk.Button(bar, text="Retry Failed", command=self.retry_failed).pack(side="left", padx=(6, 0))
        self.btn_failed_list = ttk.Button(bar, text="Save failed as modlist", command=self.save_failed_modlist,
                                          state="disabled")
        self.btn_failed_list.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Remove selected", command=self._remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Clear finished", command=self.clear_finished).pack(side="left", padx=(6, 0))
        self.btn_cancel = ttk.Button(bar, text="Cancel", style="Danger.TButton", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="right")
        self.queue_label = ttk.Label(bar, text="Queue is empty", style="Pill.TLabel")
        self.queue_label.pack(side="right", padx=8)

        self.pbar = ttk.Progressbar(self, mode="determinate", maximum=1.0)
        self.pbar.pack(fill="x", pady=(0, 6))

        vpane = ttk.PanedWindow(self, orient="vertical")
        vpane.pack(fill="both", expand=True)
        hpane = ttk.PanedWindow(vpane, orient="horizontal")
        vpane.add(hpane, weight=3)

        qf = ttk.Frame(hpane, style="Card.TFrame", padding=10)
        hpane.add(qf, weight=3)
        ttk.Label(qf, text="Download Queue", style="PanelHeading.TLabel").pack(anchor="w", pady=(0, 4))
        f, self.tree = scrolled_tree(qf, [("name", "Name", 240, True), ("wid", "Workshop id", 100, False),
                                          ("method", "Method", 110, False), ("status", "Status", 220, True)],
                                     sortable=False, height=4)
        f.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._open_pages(self.tree))

        uf = ttk.Frame(hpane, style="Card.TFrame", padding=10)
        hpane.add(uf, weight=2)
        head = ttk.Frame(uf, style="Panel.TFrame")
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Mod Updates", style="PanelHeading.TLabel").pack(side="left")
        self.upd_label = ttk.Label(head, text="not checked yet", style="PanelMuted.TLabel")
        self.upd_label.pack(side="left", padx=8)
        ttk.Label(uf, text="Workshop subscriptions update via Steam, other mods via SteamCMD.",
                  style="PanelMuted.TLabel").pack(side="bottom", anchor="w", pady=(2, 0))
        ub = ttk.Frame(uf, style="Panel.TFrame")
        ub.pack(side="bottom", fill="x", pady=(4, 0))
        self.btn_check = ttk.Button(ub, text="Check for updates", command=self.check_updates)
        self.btn_check.pack(side="left")
        ttk.Button(ub, text="Update selected", style="Accent.TButton", command=self.update_selected).pack(side="left", padx=4)
        ttk.Button(ub, text="Select all", command=lambda: self.upd_tree.selection_set(self.upd_tree.get_children(""))
                   ).pack(side="left")
        f, self.upd_tree = scrolled_tree(uf, [("name", "Name", 200, True), ("source", "Source", 80, False),
                                              ("local", "Local copy", 120, False), ("remote", "On Steam", 120, False)],
                                         height=4)
        f.pack(fill="both", expand=True)
        self.upd_tree.bind("<Double-1>", lambda e: self._open_pages(self.upd_tree))

        lf = ttk.Frame(vpane, style="Card.TFrame", padding=10)
        vpane.add(lf, weight=2)
        head = ttk.Frame(lf, style="Panel.TFrame")
        head.pack(fill="x", pady=(0, 4))
        ttk.Label(head, text="Output Log", style="PanelHeading.TLabel").pack(side="left")
        ttk.Button(head, text="Clear log", style="Small.TButton", command=self._clear_log).pack(side="right")
        ttk.Button(head, text="Copy log", style="Small.TButton", command=self._copy_log).pack(side="right", padx=4)
        tf = ttk.Frame(lf)
        tf.pack(fill="both", expand=True)
        fonts = getattr(self.app, "fonts", None) or {}
        self.log = tk.Text(tf, height=4, wrap="none", state="disabled", font=fonts.get("mono", ("Consolas", 9)),
                           relief="flat", borderwidth=0, padx=6, pady=4, undo=False)
        sy = ttk.Scrollbar(tf, orient="vertical", command=self.log.yview)
        sx = ttk.Scrollbar(tf, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.log.pack(side="left", fill="both", expand=True)

    def _apply_colors(self) -> None:
        c = getattr(self.app, "colors", None) or {}
        for tree in (self.tree, self.upd_tree):
            tree.tag_configure("ok", foreground=c.get("ok", "#3ba55d"))
            tree.tag_configure("error", foreground=c.get("err", "#e5484d"))
            tree.tag_configure("warning", foreground=c.get("warn", "#e0a526"))
            tree.tag_configure("info", foreground=c.get("accent", "#5b8def"))
            tree.tag_configure("dim", foreground=c.get("muted", "#9aa0a6"))
        self.log.configure(background=c.get("field", "#232428"), foreground=c.get("text", "#e6e6e6"),
                           insertbackground=c.get("text", "#e6e6e6"), selectbackground=c.get("select", "#35507a"))
        self.log.tag_configure("err", foreground=c.get("err", "#e5484d"))
        self.log.tag_configure("ok", foreground=c.get("ok", "#3ba55d"))
        self.log.tag_configure("time", foreground=c.get("muted", "#9aa0a6"))

    # ------------------------------------------------------------------ settings
    def _method_changed(self) -> None:
        self._save_setting("download_method", self.method.get())

    def _mode_changed(self) -> None:
        self._update_mode_expl()
        self._save_setting("install_mode", self.install_mode.get())

    def _update_mode_expl(self) -> None:
        self.mode_expl.configure(text="SteamCMD downloads: " + INSTALL_MODES[self.install_mode.get()][1])

    def _save_setting(self, key: str, value) -> None:
        s = self.mgr.settings
        if s.get(key) == value:
            return
        s.set(key, value)
        self.app.run_bg(s.save, on_done=lambda _r: self.app.emit("settings_changed"))

    def _sync_settings(self) -> None:
        s = self.mgr.settings
        if s.get("download_method") in METHODS:
            self.method.set(s.get("download_method"))
        if s.get("install_mode") in INSTALL_MODES:
            self.install_mode.set(s.get("install_mode"))
            self._update_mode_expl()
        if not self._busy:
            self._refresh_steamcmd()

    # ------------------------------------------------------------------ queue
    def _on_queue(self, refs: list[ModRef], method: str | None = None, opts: dict | None = None) -> None:
        """'queue_download' handler. Before the startup scan finished every installed mod looks missing: refuse."""
        if not not_ready(self):
            self.enqueue(refs, method, **(opts or {}))

    def enqueue(self, refs: list[ModRef], method: str | None = None, **opts) -> int:
        """Append refs to the queue (skipping ones already queued/running), show the tab, start downloading.
        opts go to Manager.download (activate / update / with_deps)."""
        method = method if method in METHODS else self.method.get()
        added = 0
        for ref in refs:
            key = ref.workshop_id or ref.key()
            old = self._items.get(key)
            if old and old["state"] in (QUEUED, RUNNING):
                continue
            item = {"ref": ref, "method": method, "opts": opts, "state": QUEUED, "status": "Queued"}
            if not ref.workshop_id:
                item.update(state=FAILED, status="✖ No workshop id known, cannot download")
            self._items[key] = item
            self._show_item(key)
            added += 1
        self.app.select_tab("Downloads")
        if added:
            self._log(f"Queued {added} mod(s) via {METHODS[method][0]}.")
        self._start_next()
        return added

    def _offer_resume(self) -> None:
        """First 'scanned': offer to re-queue what was unfinished last time."""
        rows, self._resume = self._resume, []
        if not rows:
            return
        if self.app.ask_yes_no("Downloads", f"{len(rows)} download(s) were not finished last time. Resume them?"):
            groups: dict[tuple, list[ModRef]] = {}
            for r in rows:
                groups.setdefault((r["method"], tuple(sorted(r["opts"].items()))), []).append(
                    ModRef(str(r.get("package_id") or ""), str(r.get("name") or ""), str(r["workshop_id"])))
            for (method, opts), refs in groups.items():
                self.enqueue(refs, method, **dict(opts))
        else:
            self._persist()                     # 'no': the file now holds only the current queue (or is gone)

    def _persist(self) -> None:
        """Save the queue on every change (skipped while the resume question is pending)."""
        if self._resume:
            return
        key = [(k, it["state"], it["method"], it["opts"]) for k, it in self._items.items() if it["state"] in _RESUMABLE]
        if key == self._saved:
            return
        try:
            save_queue(self._items, self._queue_file)
            self._saved = key                   # only after success, so a failed write retries
        except OSError as e:
            config.get_logger().warning("Could not save the download queue: %s", e)

    def _show_item(self, key: str) -> None:
        it = self._items[key]
        ref = it["ref"]
        values = (ref.name or ref.package_id or ref.workshop_id, ref.workshop_id or "-",
                  METHODS[it["method"]][0], it["status"])
        tags = (_STATE_TAG[it["state"]],) if _STATE_TAG[it["state"]] else ()
        if self.tree.exists(key):
            self.tree.item(key, values=values, tags=tags)
        else:
            self.tree.insert("", "end", iid=key, values=values, tags=tags)

    def _set_state(self, key: str, state: str, status: str) -> None:
        it = self._items.get(key)
        if it:
            it["state"], it["status"] = state, status
            self._show_item(key)

    def _start_next(self) -> None:
        self._update_controls()
        if self._busy:
            return
        pending = [k for k, it in self._items.items() if it["state"] == QUEUED]
        if not pending:
            return
        method, opts = self._items[pending[0]]["method"], self._items[pending[0]]["opts"]
        self._batch = [k for k in pending if self._items[k]["method"] == method and self._items[k]["opts"] == opts]
        for k in self._batch:
            self._set_state(k, RUNNING, "Waiting…")
        refs = [self._items[k]["ref"] for k in self._batch]
        self._busy = "download"
        self._run = (method, opts)
        self.pbar.configure(value=0)
        self._log(f"Starting {METHODS[method][0]} for {len(refs)} mod(s)…")
        self._cancel = self.app.run_bg(self._download_work, refs, method, opts, on_done=self._download_done,
                                       on_error=self._download_failed, busy=f"Downloading {len(refs)} mod(s)…",
                                       progress=True, cancellable=True)
        self._update_controls()
        self._poll()

    def _tee(self, progress):
        """Progress callback for worker threads: forwards to the status bar and to this tab's line queue."""
        def cb(f=None, text=""):
            if progress:
                progress(f, text)
            self._lines.put((f, text))
        return cb

    def _download_work(self, refs: list[ModRef], method: str, opts: dict, progress=None, cancel=None):
        """Worker thread. Manager.download adds the missing dependencies (unless with_deps=False) and rescans by
        itself when something got installed; the added dependencies get named rows (names from the DB)."""
        results = self.mgr.download(refs, method, progress=self._tee(progress), cancel=cancel, **opts) or []
        asked = {r.workshop_id for r in refs}
        extra = [r.workshop_id for r in results if r.workshop_id not in asked]
        try:
            names = {w: m.display_name for w, m in self.mgr.db.get_many(extra).items()} if extra else {}
        except Exception:  # noqa: BLE001 - names are cosmetic
            names = {}
        return results, any(r.ok for r in results), names

    def _download_done(self, out) -> None:
        results, changed, names = out
        self._drain()
        method, opts = self._run
        asked = {self._items[k]["ref"].workshop_id for k in self._batch if k in self._items}
        for r in results:                      # dependencies Manager.download added: shown (and retried) like the rest
            w = r.workshop_id
            if w and w not in asked and self._items.get(w, {}).get("state") not in (QUEUED, RUNNING):
                self._items[w] = {"ref": ModRef(name=names.get(w, ""), workshop_id=w), "method": method, "opts": opts,
                                  "state": RUNNING, "status": ""}
                self._batch.append(w)
                asked.add(w)
        by_id = {r.workshop_id: r for r in results}
        cancelled = self._cancel is not None and self._cancel.is_set()
        counts = {DONE: 0, FAILED: 0, OPENED: 0, CANCELLED: 0}   # final_status never returns QUEUED/RUNNING
        for k in self._batch:
            it = self._items.get(k)
            if not it:
                continue
            res = by_id.get(it["ref"].workshop_id)
            if it["state"] == DONE:             # Steam reported it installed while the batch ran: stays done
                state, status = DONE, it["status"]
            elif res:
                state, status = final_status(res, it["method"])
                if state == FAILED:
                    self._log(f"{it['ref'].name or k}: {res.message}", tag="err")
            else:
                state, status = (CANCELLED, "Cancelled") if cancelled else (FAILED, "✖ No result returned")
            counts[state] += 1
            self._set_state(k, state, status)
        self._log(f"Finished: {counts[DONE]} ok, {counts[FAILED]} failed, {counts[OPENED]} opened, "
                  f"{counts[CANCELLED]} cancelled.", tag="err" if counts[FAILED] else "ok")
        if counts[OPENED]:
            self._log("Subscribe on the opened pages; Steam then downloads them into the workshop folder. "
                      "Press F5 (rescan) afterwards.")
        updated = [w for w, r in by_id.items() if r.ok and w in self._updates]
        for w in updated:
            del self._updates[w]
            if self.upd_tree.exists(w):
                self.upd_tree.delete(w)
        if updated:
            self.upd_label.configure(text=f"{len(self._updates)} update(s) left" if self._updates else "all updated")
        self._finish_batch()
        if changed:
            self.app.emit("scanned")
            if opts.get("activate"):           # Manager.download activated + sorted what it installed
                self.app.emit("active_changed")
        self.app.emit("downloads_done", results)
        self.app.toast(f"Downloads finished: {counts[DONE]} ok, {counts[FAILED]} failed",
                       level="warning" if counts[FAILED] else "ok")
        self._start_next()

    def _download_failed(self, exc: BaseException) -> None:
        self._drain()
        left = [k for k in self._batch if self._items.get(k, {}).get("state") != DONE]   # done ones stay done
        # a cancel (e.g. during SteamCMD's auto-install) is not a failure; read it before _finish_batch clears _cancel
        if isinstance(exc, InterruptedError) or (self._cancel is not None and self._cancel.is_set()):
            for k in left:
                self._set_state(k, CANCELLED, "Cancelled")
            self._log("Cancelled.")
            self._finish_batch()
            self.app.emit("downloads_done", [])
            self.app.toast("Download cancelled", level="warning")
            self._start_next()
            return
        msg = str(exc) or type(exc).__name__
        short = msg.splitlines()[0].rstrip(" :")
        self._log(f"Download failed: {msg}", tag="err")
        no_space = type(exc).__name__ == "NotEnoughSpaceError"      # free space first; another method won't help
        steamcmd = not no_space and any(self._items.get(k, {}).get("method") == "steamcmd" for k in self._batch)
        if steamcmd:
            self._log(OTHER_METHODS_HINT)
        for k in left:
            self._set_state(k, FAILED, f"✖ {short}")
        self._finish_batch()
        self.app.emit("downloads_done", [])
        self.app.toast(short if no_space else f"Download failed: {short}" + (f" — {OTHER_METHODS_HINT}" if steamcmd else ""),
                       level="warning" if no_space else "error")
        self._start_next()

    def _finish_batch(self) -> None:
        if self._cancel is not None and self._cancel.is_set():   # the status-bar Cancel stops the queue too
            for k, it in self._items.items():
                if it["state"] == QUEUED:
                    self._set_state(k, CANCELLED, "Cancelled")
        self._batch = []
        self._busy = ""
        self._cancel = None
        self.pbar.configure(value=0)
        self._update_controls()

    def cancel(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
            self._log("Cancelling…")
        for k, it in self._items.items():
            if it["state"] == QUEUED:
                self._set_state(k, CANCELLED, "Cancelled")
        self._update_controls()

    def retry_failed(self) -> None:
        """Re-queue failed/cancelled items with their own opts, via the selected method; an update keeps its
        method (update_selected picks it by where the mod lives: Steam for subscriptions, else SteamCMD)."""
        groups: dict[tuple, list[ModRef]] = {}
        for it in self._items.values():
            if it["state"] in (FAILED, CANCELLED) and it["ref"].workshop_id:
                method = it["method"] if it["opts"].get("update") else None
                groups.setdefault((method, tuple(sorted(it["opts"].items()))), []).append(it["ref"])
        if not groups:
            self.app.toast("Nothing to retry")
            return
        for (method, opts), refs in groups.items():
            self.enqueue(refs, method, **dict(opts))

    def save_failed_modlist(self) -> None:
        """Save the failed items as a new modlist 'Failed downloads <date time>' (never over another list)."""
        refs = [it["ref"] for it in self._items.values() if it["state"] == FAILED]
        if not refs:
            return
        ml = ModList(name=time.strftime("Failed downloads %Y-%m-%d %H:%M"), mods=list(refs),
                     game_version=self.mgr.game_version or "")

        def job():
            ml.name = self.mgr.unique_modlist_name(ml.name)
            self.mgr.save_modlist(ml)
            return ml.name

        def done(name):
            self.app.emit("modlists_changed")
            self.app.toast(f"Saved modlist “{name}” ({len(refs)} mods)", "ok")
        self.app.run_bg(job, on_done=done, busy="Saving modlist…")

    def clear_finished(self) -> None:
        for k in [k for k, it in self._items.items() if it["state"] not in (QUEUED, RUNNING)]:
            del self._items[k]
            self.tree.delete(k)
        self._update_controls()

    def _remove_selected(self) -> None:
        for k in self.tree.selection():
            it = self._items.get(k)
            if it and it["state"] != RUNNING:
                del self._items[k]
                self.tree.delete(k)
        self._update_controls()

    def _update_controls(self) -> None:
        self.btn_cancel.configure(state="normal" if self._busy else "disabled")
        self.sc_btn.configure(state="disabled" if self._busy else "normal")
        n = {s: 0 for s in _STATE_TAG}
        for it in self._items.values():
            n[it["state"]] += 1
        if not self._items:
            text = "Queue is empty"
        else:
            text = f"{n[QUEUED] + n[RUNNING]} pending · {n[DONE]} done · {n[FAILED]} failed"
        self.queue_label.configure(text=text)
        self.btn_failed_list.configure(state="normal" if n[FAILED] else "disabled")
        self._persist()

    def _download_missing(self) -> None:
        if not_ready(self):
            return

        def done(refs):
            if refs:
                self.enqueue(refs, activate=True)    # + their dependencies, activated and sorted once installed
            else:
                self.app.toast("Nothing missing: every active mod is installed")
        self.app.run_bg(self.mgr.missing_refs, on_done=done, busy="Looking for missing mods…")

    # ------------------------------------------------------------------ progress / log
    def _poll(self) -> None:
        if self._polling:
            return
        self._polling = True
        self.after(100, self._poll_tick)

    def _poll_tick(self) -> None:
        self._drain()
        if self._busy or not self._lines.empty():
            self.after(100, self._poll_tick)
        else:
            self._polling = False

    def _drain(self, limit: int = 1000) -> None:
        """Move queued progress lines into the log + item statuses (Tk thread, batched)."""
        lines, frac = [], None
        batch = set(self._batch)
        updates: dict[str, str] = {}
        for _ in range(limit):
            try:
                f, text = self._lines.get_nowait()
            except queue.Empty:
                break
            if f is not None:
                frac = f
            if not text:
                continue
            lines.append(text)
            if batch:
                for wid in _ID_RE.findall(text):
                    if wid in batch:
                        updates[wid] = status_from_line(text)
        if frac is not None:
            self.pbar.configure(value=max(0.0, min(1.0, frac)))
        final = self._run[0] == "steamworks"       # Steam's 'installed' is final: count it now, not at the batch end
        done = 0
        for k, status in updates.items():
            if self._items.get(k, {}).get("state") == RUNNING:
                if final and status == "Downloaded":
                    self._set_state(k, *final_status(DownloadResult(k, True), "steamworks"))
                    done += 1
                else:
                    self._set_state(k, RUNNING, status)
        if done:
            self._update_controls()
        if lines:
            self._log_many(lines)

    def _log(self, text: str, tag: str = "") -> None:
        self._log_many([text], tag)

    def _log_many(self, lines: list[str], tag: str = "") -> None:
        log = self.log
        at_end = log.yview()[1] >= 0.999
        stamp = time.strftime("%H:%M:%S ")
        log.configure(state="normal")
        for line in lines:
            low = line.lower()
            t = tag or ("err" if ("error" in low or "failed" in low) else "ok" if "success" in low else "")
            log.insert("end", stamp, ("time",))
            log.insert("end", line.rstrip() + "\n", (t,) if t else ())
        n = int(log.index("end-1c").split(".")[0])
        if n > LOG_MAX_LINES:
            log.delete("1.0", f"{n - LOG_MAX_LINES}.0")
        log.configure(state="disabled")
        if at_end:
            log.see("end")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.log.get("1.0", "end-1c"))
        self.app.toast("Log copied to clipboard")

    # ------------------------------------------------------------------ updates
    def check_updates(self, quiet: bool = False) -> None:
        """quiet (startup check): toast only when updates exist, failures just logged."""
        if not_ready(self):
            return
        self.btn_check.configure(state="disabled")
        self.upd_label.configure(text="checking…")
        self.app.run_bg(self.mgr.check_updates, on_done=self._quiet_updates if quiet else self._show_updates,
                        on_error=self._quiet_failed if quiet else self._updates_failed,
                        busy="Checking Steam for mod updates…", progress=True)

    def _quiet_updates(self, pairs) -> None:
        self._show_updates(pairs)
        if n := len(self._updates):
            self.app.toast(f"{n} mod update{'s' if n != 1 else ''} available (Downloads tab)", "info")

    def _quiet_failed(self, exc: BaseException) -> None:
        self.btn_check.configure(state="normal")
        self.upd_label.configure(text="check failed")
        logging.getLogger("rmm").info("startup update check failed: %s", exc)

    def _updates_failed(self, exc: BaseException) -> None:
        self.btn_check.configure(state="normal")
        self.upd_label.configure(text="check failed")
        self._log(f"Update check failed: {exc}", tag="err")
        self.app.error("Update check failed", "Couldn't reach Steam to check for mod updates (no internet?). "
                       "Try again later.\n\nDetails are in the Downloads log." if isinstance(exc, OSError)
                       else (str(exc) or type(exc).__name__).splitlines()[0])

    def _show_updates(self, pairs) -> None:
        self.btn_check.configure(state="normal")
        self.upd_tree.delete(*self.upd_tree.get_children(""))
        self._updates = {}
        for mod, remote in pairs or []:
            wid = mod.workshop_id
            if not wid or wid in self._updates:
                continue
            self._updates[wid] = (mod, remote)
            self.upd_tree.insert("", "end", iid=wid, values=(mod.name, mod.source, _fmt_date(mod.mtime), _fmt_date(remote)))
        self.upd_tree.selection_set(self.upd_tree.get_children(""))
        n = len(self._updates)
        self.upd_label.configure(text=f"{n} update(s) available · checked {time.strftime('%H:%M')}" if n
                                 else f"all mods up to date · checked {time.strftime('%H:%M')}")
        self._log(f"Update check: {n} mod(s) have a newer version on Steam.")

    def update_selected(self) -> None:
        groups: dict[str, list[ModRef]] = {}
        for wid in self.upd_tree.selection():
            mod, _remote = self._updates.get(wid, (None, 0))
            if mod is None:
                continue
            method = "steamworks" if mod.source == "workshop" else "steamcmd"
            groups.setdefault(method, []).append(ModRef(package_id=mod.package_id, name=mod.name, workshop_id=wid))
        if not groups:
            self.app.toast("Select mods to update first")
            return
        for method, refs in groups.items():
            self.enqueue(refs, method, update=True, with_deps=False)   # replace the installed copy in place

    def _open_pages(self, tree: ttk.Treeview) -> None:
        for iid in tree.selection()[:10]:
            wid = iid if iid.isdigit() else self._items.get(iid, {}).get("ref", ModRef()).workshop_id
            if wid:
                webbrowser.open_new_tab(WORKSHOP_URL.format(wid))

    # ------------------------------------------------------------------ SteamCMD
    def _refresh_steamcmd(self) -> None:
        def info():
            sc = self.mgr.steamcmd()
            return sc.is_installed(), os.path.dirname(sc.exe)

        def show(res):
            installed, root = res
            c = getattr(self.app, "colors", None) or {}
            self.sc_status.configure(text="✔ Installed" if installed else "Not installed (installed automatically on first use)",
                                     foreground=c.get("ok", "#3ba55d") if installed else c.get("muted", "#9aa0a6"))
            self.sc_path.configure(text=root)
            self.sc_btn.configure(text="Reinstall" if installed else "Install")
            self._sc_root = root

        self.app.run_bg(info, on_done=show, on_error=lambda e: self.sc_status.configure(text=f"SteamCMD error: {e}"))

    def _install_steamcmd(self) -> None:
        if self._busy:
            return
        self._busy = "install"
        self._update_controls()
        self._log("Installing SteamCMD…")

        def work(progress=None, cancel=None):
            sc = self.mgr.steamcmd()
            sc.install(progress=self._tee(progress), cancel=cancel)
            return sc.is_installed()

        def done(ok):
            self._drain()
            self._finish_batch()
            self._log("SteamCMD is ready." if ok else "SteamCMD install finished but steamcmd.exe was not found.",
                      tag="ok" if ok else "err")
            self._refresh_steamcmd()
            self._start_next()

        def failed(exc):
            self._drain()
            cancelled = isinstance(exc, InterruptedError) or (self._cancel is not None and self._cancel.is_set())
            self._finish_batch()                       # clears self._cancel, so read it first
            if cancelled:
                self._log("SteamCMD install cancelled.")
                self.app.toast("SteamCMD install cancelled", level="warning")
                self._refresh_steamcmd()               # a cancel can leave a partial extract
                self._start_next()
                return
            self._log(f"SteamCMD install failed: {exc}", tag="err")
            short = (str(exc) or type(exc).__name__).splitlines()[0].rstrip(":")   # the mirror URLs are in the log
            self.app.error("SteamCMD", f"Installing SteamCMD failed:\n{short}\n\n{OTHER_METHODS_HINT}")
            self._refresh_steamcmd()
            self._start_next()

        self._cancel = self.app.run_bg(work, on_done=done, on_error=failed, busy="Installing SteamCMD…",
                                       progress=True, cancellable=True)
        self._poll()

    def _open_steamcmd_folder(self) -> None:
        root = getattr(self, "_sc_root", "")
        if root and os.path.isdir(root):
            os.startfile(root)
        else:
            self.app.toast("SteamCMD folder does not exist yet", level="warning")


def _selftest() -> None:
    assert status_from_line('Success. Downloaded item 123456 to "C:\\x" (10 bytes)') == "Downloaded"
    assert status_from_line("ERROR! Download item 123456 failed (Timeout).") == "Error: Timeout"
    assert status_from_line("Downloading item 123456 ...") == "Downloading…"
    assert status_from_line("Installing 123456 into Mods") == "Installing…"
    assert status_from_line("Subscribed 123456") == "Subscribing…"
    assert _ID_RE.findall("item 1234567 at 12:00, (99 bytes) 12345678901234") == ["1234567"]
    assert final_status(DownloadResult("1", True), "steamcmd") == (DONE, "✔ Installed")
    assert final_status(DownloadResult("1", True), "steamworks")[0] == DONE
    assert final_status(DownloadResult("1", False, "opened in Steam"), "steam_client")[0] == OPENED
    assert final_status(DownloadResult("2", False, "opened in the browser"), "steam_client")[1].endswith("browser: click Subscribe")
    assert final_status(DownloadResult("1", False, "Timeout"), "steamcmd") == (FAILED, "✖ Timeout")
    assert final_status(DownloadResult("1", False, "cancelled"), "browser") == (CANCELLED, "Cancelled")
    assert final_status(DownloadResult("1", False, "Could not get it:\nmirror 1: x"), "steamcmd")[1] == "✖ Could not get it:"
    _selftest_queue()


def _selftest_queue() -> None:
    """Queue behaviour with a synchronous stand-in App: opts reach Manager.download, dependencies it added get rows,
    Retry keeps the opts, the status-bar Cancel also cancels queued batches, nothing is queued before the startup scan."""
    import shutil
    import tempfile
    import threading
    import urllib.error
    from rmm.gui.workshop_tab import _SmokeApp, _SmokeMgr
    from rmm.models import Mod
    old_dir, config.APP_DIR = config.APP_DIR, tempfile.mkdtemp(prefix="rmm_dlq_")   # never the real queue file
    qf = config.app_path(QUEUE_FILE)
    # save/load round trip; only unfinished items with a workshop id are kept; corrupt files are dropped
    items = {"1": {"ref": ModRef("a.b", "Älpha", "1"), "method": "steamcmd", "opts": {"activate": True}, "state": FAILED},
             "2": {"ref": ModRef("c.d", "Done", "2"), "method": "browser", "opts": {}, "state": DONE},
             "x": {"ref": ModRef("e.f", "NoId", ""), "method": "steamcmd", "opts": {}, "state": QUEUED}}
    save_queue(items, qf)
    assert load_queue(qf) == [{"workshop_id": "1", "name": "Älpha", "package_id": "a.b", "method": "steamcmd",
                               "opts": {"activate": True}}], load_queue(qf)
    save_queue({}, qf)
    assert not os.path.exists(qf) and load_queue(qf) == []
    for bad in (b"{nope", b'[{"workshop_id": "1", "method": "fax", "opts": {}}]'):
        config.atomic_write(qf, bad)
        assert load_queue(qf) == [] and not os.path.exists(qf)
    try:
        root = tk.Tk()
    except tk.TclError:
        shutil.rmtree(config.APP_DIR, ignore_errors=True)
        config.APP_DIR = old_dir
        return
    root.withdraw()
    try:
        mgr, calls, fail = _SmokeMgr(), [], []

        def download(refs, method=None, progress=None, cancel=None, **opts):
            calls.append(([r.workshop_id for r in refs], method, opts))
            res = [DownloadResult(r.workshop_id, not fail, "Timeout" if fail else "") for r in refs]
            return res + ([DownloadResult("2009463077", True)] if opts.get("with_deps", True) and not fail else [])
        mgr.download = download
        app = _SmokeApp(root, mgr)
        tab = DownloadsTab(root, app)
        app.emit("queue_download", [ModRef("a.b", "Alpha", "1111")], None, {"activate": True})
        assert not calls and "Still loading" in app.toasts[-1], app.toasts      # startup scan still running
        app.emit("scanned")
        app.emit("queue_download", [ModRef("a.b", "Alpha", "1111")], None, {"activate": True})
        assert calls[-1] == (["1111"], "steamcmd", {"activate": True}), calls
        dep = tab._items["2009463077"]                # the dependency Manager.download added: a named, finished row
        assert dep["ref"].name == "Harmony" and dep["state"] == DONE and dep["opts"] == {"activate": True}, dep
        fail.append(1)
        tab.enqueue([ModRef("c.c", "Gamma", "3333")], "steamworks", update=True, with_deps=False)
        tab.enqueue([ModRef("d.d", "Delta", "7777")], activate=True)
        fail.clear()
        assert tab._items["3333"]["state"] == FAILED == tab._items["7777"]["state"]
        assert str(tab.btn_failed_list.cget("state")) == "normal"
        tab.save_failed_modlist()                     # the failed rows become a new modlist
        fl = [m for m in mgr.lists.values() if m.name.startswith("Failed downloads ")]
        assert len(fl) == 1 and sorted(r.workshop_id for r in fl[0].mods) == ["3333", "7777"], fl
        tab.retry_failed()                            # own opts; an update also keeps its method
        assert sorted(calls[-2:]) == [(["3333"], "steamworks", {"update": True, "with_deps": False}),
                                      (["7777"], "steamcmd", {"activate": True})], calls[-2:]
        n, ev = len(calls), threading.Event()
        ev.set()                                      # the status-bar Cancel set the running job's event
        tab._items["4444"] = {"ref": ModRef(workshop_id="4444"), "method": "steamcmd", "opts": {}, "state": RUNNING,
                              "status": ""}
        tab._items["5555"] = {"ref": ModRef(workshop_id="5555"), "method": "steamcmd", "opts": {}, "state": QUEUED,
                              "status": "Queued"}
        tab._batch, tab._busy, tab._cancel, tab._run = ["4444"], "download", ev, ("steamcmd", {})
        tab._download_done(([DownloadResult("4444", False, "cancelled")], False, {}))
        assert tab._items["5555"]["state"] == CANCELLED and len(calls) == n, (tab._items["5555"], calls[n:])
        tab._items["8888"] = {"ref": ModRef(workshop_id="8888"), "method": "steamcmd", "opts": {}, "state": RUNNING,
                              "status": ""}
        tab._batch, tab._busy, tab._cancel = ["8888"], "download", None
        tab._download_failed(InterruptedError("SteamCMD installation cancelled"))   # cancel during the auto-install
        assert tab._items["8888"]["state"] == CANCELLED and "cancelled" in app.toasts[-1], (tab._items["8888"], app.toasts)
        from ..core import NotEnoughSpaceError
        tab._items["8888"]["state"] = RUNNING
        tab._batch, tab._busy, tab._cancel = ["8888"], "download", None
        tab._download_failed(NotEnoughSpaceError("Not enough disk space: need about 2.0 GB on C:, only 1.0 GB free."))
        assert tab._items["8888"]["state"] == FAILED and app.toasts[-1].startswith("Not enough disk space"), app.toasts
        # Steam subscribe: a mod Steam reports installed counts as done at once, and stays done if the batch ends badly
        def counts() -> list[int]:                    # pending, done, failed
            return [int(x) for x in re.findall(r"\d+", str(tab.queue_label.cget("text")))]
        for w in ("454545", "464646", "474747"):
            tab._items[w] = {"ref": ModRef(workshop_id=w), "method": "steamworks", "opts": {}, "state": RUNNING,
                             "status": ""}
        tab._batch, tab._busy, tab._cancel, tab._run = ["454545", "464646"], "download", None, ("steamworks", {})
        tab._update_controls()
        before = counts()
        tab._lines.put((0.5, "Success: Steam installed 454545"))
        tab._lines.put((0.5, "Subscribing 464646 failed (No connection)"))
        tab._drain()
        assert counts() == [before[0] - 1, before[1] + 1, before[2]], (before, counts())
        assert tab._items["454545"]["state"] == DONE and tab._items["464646"]["state"] == RUNNING
        tab._download_done(([DownloadResult("454545", False, "cancelled"), DownloadResult("464646", False, "x")], False, {}))
        assert tab._items["454545"]["state"] == DONE and tab._items["464646"]["state"] == FAILED, tab._items["454545"]
        tab._items["464646"]["state"] = RUNNING
        tab._batch, tab._busy, tab._run = ["464646", "474747"], "download", ("steamworks", {})
        tab._lines.put((0.9, "Success: Steam installed 474747"))
        tab._download_failed(RuntimeError("helper crashed"))     # drains first: 474747 was installed before the crash
        assert tab._items["474747"]["state"] == DONE and tab._items["464646"]["state"] == FAILED
        tab._items["464646"]["state"] = CANCELLED    # out of the saved queue for the checks below
        tab._update_controls()
        tab._show_updates([(Mod("x.y", "x.y", "Xy", "", "local", "6666"), 1)])
        tab.update_selected()                         # an update replaces the installed copy, no dependency closure
        assert calls[-1] == (["6666"], "steamcmd", {"update": True, "with_deps": False}), calls[-1]
        shown = []
        app.error = lambda title, msg: shown.append(msg)
        tab._updates_failed(urllib.error.URLError("[Errno 11001] getaddrinfo failed"))
        assert shown[-1].startswith("Couldn't reach Steam") and "urlopen" not in shown[-1], shown
        n = len(shown)
        tab._quiet_failed(urllib.error.URLError("offline"))   # startup check: no dialog
        tab._quiet_updates([(Mod("x.y", "x.y", "Xy", "", "local", "6666"), 1)])
        assert len(shown) == n and app.toasts[-1] == "1 mod update available (Downloads tab)", app.toasts
        assert [r["workshop_id"] for r in load_queue(qf)] == ["8888"], load_queue(qf)   # the one still failed
        tab._items["9999"] = {"ref": ModRef("u.p", "Upd", "9999"), "method": "steamworks",
                              "opts": {"update": True, "with_deps": False}, "state": QUEUED, "status": ""}
        tab._update_controls()                        # as if the app closed with it still queued
        # startup: the rows left last time are offered once; 'yes' re-queues them with their method + opts
        tab2 = DownloadsTab(root, app)
        assert len(tab2._resume) == 2
        tab2._persist()                               # a queue change while the question is pending...
        assert len(load_queue(qf)) == 2               # ...does not drop the rows
        n = len(calls)
        app.ask_yes_no = lambda title, msg: True
        tab2._offer_resume()
        assert sorted(calls[n:]) == [(["8888"], "steamcmd", {}),
                                     (["9999"], "steamworks", {"update": True, "with_deps": False})], calls[n:]
        tab2.clear_finished()
        tab2._items.clear()
        tab2._update_controls()
        assert not os.path.exists(qf)                 # empty queue: file gone
    finally:
        for j in root.tk.splitlist(root.tk.call("after", "info")):
            root.tk.call("after", "cancel", j)
        root.destroy()
        shutil.rmtree(config.APP_DIR, ignore_errors=True)
        config.APP_DIR = old_dir
