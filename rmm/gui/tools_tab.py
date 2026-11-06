"""Tools tab: Player.log viewer, ModsConfig backups, mod database status, user load-order rules."""
from __future__ import annotations

import json
import os
import time
import tkinter as tk
from tkinter import filedialog, ttk

from rmm.gui.widgets import ago, scrolled_tree
from rmm.sorting import _FLAG_KEYS, _PAIR_KEYS, _comment, _empty_rule, merge_rules, rules_from_json

LEVEL_LABEL = {"error": "✖ Error", "warning": "⚠ Warning", "info": "ℹ Info"}
LOG_FILTERS = {
    "Errors & warnings": ("error", "warning"),
    "Errors": ("error",),
    "Warnings": ("warning",),
    "All": ("error", "warning", "info"),
}
# (label, user_rules key); load_top / load_bottom are flags without a second mod
RELATIONS = [("loads after", "load_after"), ("loads before", "load_before"),
             ("is incompatible with", "incompatible_with"), ("always loads at the top", "load_top"),
             ("always loads at the bottom", "load_bottom")]
REL_LABEL = {k: label for label, k in RELATIONS}


def _fmt_ts(v) -> str:
    """Unix time (int/float/numeric str) -> local date; other strings as-is; '' when unknown."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v or "")
    if f <= 0:
        return ""
    if f > 1e11:  # milliseconds
        f /= 1000
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(f))


def _scrolled_text(parent, font, height: int = 8, wrap: str = "word") -> tuple[ttk.Frame, tk.Text]:
    frame = ttk.Frame(parent)
    text = tk.Text(frame, height=height, wrap=wrap, font=font, relief="flat", borderwidth=0, padx=6, pady=4,
                   state="disabled", undo=False)
    sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    text.pack(side="left", fill="both", expand=True)
    return frame, text


def _set_text(widget: tk.Text, content: str) -> None:
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", content)
    widget.configure(state="disabled")


class ToolsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app = app
        self.mgr = app.mgr
        fonts = getattr(app, "fonts", None) or {}
        self._mono = fonts.get("mono", ("Consolas", 9))
        self._texts: list[tk.Text] = []
        self._trees: list[ttk.Treeview] = []

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self._build_log()
        self._build_backups()
        self._build_db()
        self._build_rules()
        self._build_unused()
        self._build_textures()
        self._apply_colors()

        app.on("db_updated", lambda *a: self.refresh_db())
        app.on("scanned", lambda *a: self._refresh_mod_choices())
        app.on("active_changed", lambda *a: self._preview_backup())     # the diff is against the active list
        for ev in ("scanned", "active_changed"):                          # Find results go stale
            app.on(ev, lambda *a: self._clear_unused())
        app.on("theme_changed", lambda *a: self.after_idle(self._apply_colors))

    def focus_search(self) -> None:
        """Ctrl+F: the Game log search box (the other pages have none)."""
        if self.nb.index("current") == 0:
            self._log_entry.focus_set()
            self._log_entry.select_range(0, "end")

    def _page(self, title: str, on_show) -> ttk.Frame:
        page = ttk.Frame(self.nb, padding=8)
        self.nb.add(page, text=title)
        page.bind("<Map>", lambda e: on_show() if e.widget is page else None)
        return page

    def _apply_colors(self) -> None:
        c = getattr(self.app, "colors", None) or {}
        for t in self._texts:
            t.configure(background=c.get("field", "#232428"), foreground=c.get("text", "#e6e6e6"),
                        insertbackground=c.get("text", "#e6e6e6"), selectbackground=c.get("select", "#35507a"))
            t.tag_configure("add", foreground=c.get("ok", "#3ba55d"))
            t.tag_configure("remove", foreground=c.get("err", "#e5484d"))
            t.tag_configure("head", foreground=c.get("accent", "#5b8def"))
        for tree in self._trees:
            tree.tag_configure("error", foreground=c.get("err", "#e5484d"))
            tree.tag_configure("warning", foreground=c.get("warn", "#e0a526"))
            tree.tag_configure("info", foreground=c.get("muted", "#9aa0a6"))
            tree.tag_configure("dim", foreground=c.get("muted", "#9aa0a6"))

    # ================================================================== Game log
    def _build_log(self) -> None:
        self._log_entries: list = []
        self._log_loaded = False
        page = self._page("Game log", lambda: None if self._log_loaded else self.load_log())
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        ttk.Button(bar, text="Reload Player.log", style="Accent.TButton", command=self.load_log).pack(side="left")
        ttk.Button(bar, text="Open file", command=self._open_player_log).pack(side="left", padx=4)
        ttk.Label(bar, text="Show").pack(side="left", padx=(12, 4))
        self.log_filter = tk.StringVar(value="Errors & warnings")
        cb = ttk.Combobox(bar, textvariable=self.log_filter, values=list(LOG_FILTERS), state="readonly", width=17)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._fill_log())
        ttk.Label(bar, text="Search").pack(side="left", padx=(12, 4))
        self.log_search = tk.StringVar()
        self._log_entry = ttk.Entry(bar, textvariable=self.log_search, width=24)
        self._log_entry.pack(side="left")
        self._search_job = None
        self.log_search.trace_add("write", lambda *a: self._debounce_search())
        self.log_summary = ttk.Label(bar, text="", style="Muted.TLabel")
        self.log_summary.pack(side="right")

        pane = ttk.PanedWindow(page, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(8, 0))
        f, self.log_tree = scrolled_tree(pane, [("level", "Level", 90, False), ("count", "×", 50, False),
                                                 ("mods", "Suspected mods", 220, False), ("text", "Message", 600, True)])
        self._trees.append(self.log_tree)
        pane.add(f, weight=3)
        self.log_tree.bind("<<TreeviewSelect>>", lambda e: self._show_log_entry())
        self.log_tree.bind("<Double-1>", lambda e: self._show_suspect())

        df = ttk.Frame(pane)
        pane.add(df, weight=2)
        f, self.log_detail = _scrolled_text(df, self._mono, height=8, wrap="none")
        self._texts.append(self.log_detail)
        f.pack(fill="both", expand=True, pady=(6, 0))
        row = ttk.Frame(df)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Show suspected mod", command=self._show_suspect).pack(side="left")
        ttk.Button(row, text="Deactivate suspected mods", command=self._deactivate_suspects).pack(side="left", padx=4)
        ttk.Button(row, text="Copy", command=self._copy_log_entry).pack(side="left")
        ttk.Label(row, text="Suspects are guessed from package ids, DLL and folder names in the error text.",
                  style="Muted.TLabel").pack(side="right")

    def load_log(self) -> None:
        self._log_loaded = True
        self.log_summary.configure(text="Reading Player.log…")
        self.app.run_bg(self.mgr.read_player_log, on_done=self._log_loaded_cb,
                        on_error=lambda e: self.log_summary.configure(text=f"Could not read Player.log: {e}"),
                        busy="Reading Player.log…")

    def _log_loaded_cb(self, entries) -> None:
        self._log_entries = list(entries or [])
        self._fill_log()

    def _debounce_search(self) -> None:
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(250, self._fill_log)

    def _mod_name(self, uid: str) -> str:
        m = self.mgr.installed.get(uid) if self.mgr.installed else None
        return m.name if m else uid

    def _fill_log(self) -> None:
        self._search_job = None
        levels = LOG_FILTERS.get(self.log_filter.get(), ("error", "warning"))
        q = self.log_search.get().strip().lower()
        tree = self.log_tree
        tree.delete(*tree.get_children(""))
        shown = 0
        for i, e in enumerate(self._log_entries):
            if e.level not in levels:
                continue
            names = [self._mod_name(u) for u in e.mods]
            if q and q not in e.text.lower() and q not in e.detail.lower() and not any(q in n.lower() for n in names):
                continue
            mods = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
            tree.insert("", "end", iid=str(i), values=(LEVEL_LABEL.get(e.level, e.level), f"×{e.count}", mods,
                                                       e.text[:300]), tags=(e.level,))
            shown += 1
        n_err = sum(1 for e in self._log_entries if e.level == "error")
        n_warn = sum(1 for e in self._log_entries if e.level == "warning")
        self.log_summary.configure(text=f"{shown} shown · {len(self._log_entries)} entries · "
                                        f"{n_err} errors · {n_warn} warnings")
        if not self._log_entries:
            _set_text(self.log_detail, "No entries. Player.log is written by RimWorld while it runs; start the game once.")

    def _selected_entry(self):
        sel = self.log_tree.selection()
        if not sel:
            return None
        i = int(sel[0])
        return self._log_entries[i] if i < len(self._log_entries) else None

    def _show_log_entry(self) -> None:
        e = self._selected_entry()
        if e is None:
            return
        lines = [e.text, ""]
        if e.count > 1:
            lines.append(f"(repeated {e.count}×)")
        if e.mods:
            lines.append("Suspected mods:")
            for u in e.mods:
                m = self.mgr.installed.get(u) if self.mgr.installed else None
                lines.append(f"  • {m.name} [{u}]  {m.path}" if m else f"  • {u}")
            lines.append("")
        if e.detail and e.detail.strip() != e.text.strip():
            lines.append(e.detail)
        _set_text(self.log_detail, "\n".join(lines))

    def _show_suspect(self) -> None:
        e = self._selected_entry()
        if e and e.mods:
            self.app.show_mod_details(uid=e.mods[0])
        else:
            self.app.toast("No suspected mod for this entry")

    def _deactivate_suspects(self) -> None:
        e = self._selected_entry()
        uids = [u for u in (e.mods if e else []) if u in self.mgr.active]
        if not uids:
            self.app.toast("None of the suspected mods is active")
            return
        names = "\n".join(f"• {self._mod_name(u)}" for u in uids)
        if self.app.ask_yes_no("Deactivate mods", f"Deactivate these mods?\n\n{names}\n\n"
                                                  "Remember to Save in the Mods tab afterwards."):
            self.mgr.deactivate(uids)
            self.app.emit("active_changed")
            self.app.toast(f"Deactivated {len(uids)} mod(s); save to apply")

    def _copy_log_entry(self) -> None:
        text = self.log_detail.get("1.0", "end-1c")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.app.toast("Copied")

    def _open_player_log(self) -> None:
        cfg = self.mgr.config_dir
        path = os.path.join(os.path.dirname(os.path.normpath(cfg)), "Player.log") if cfg else ""
        if path and os.path.isfile(path):
            os.startfile(path)
        else:
            self.app.toast("Player.log not found", level="warning")

    # ================================================================== Backups
    def _build_backups(self) -> None:
        self._previews: dict = {}          # backup path -> parsed ModList
        page = self._page("Backups", self.refresh_backups)
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        ttk.Button(bar, text="Restore selected", style="Accent.TButton", command=self._restore).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.refresh_backups).pack(side="left", padx=4)
        ttk.Button(bar, text="Open folder", command=self._open_backup_folder).pack(side="left")
        ttk.Button(bar, text="Open file", command=self._open_backup_file).pack(side="left", padx=4)
        self.bk_summary = ttk.Label(bar, text="", style="Muted.TLabel")
        self.bk_summary.pack(side="right")
        ttk.Label(page, text="A copy of ModsConfig.xml is saved here every time the active list is written to the game.",
                  style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        pane = ttk.PanedWindow(page, orient="horizontal")
        pane.pack(fill="both", expand=True, pady=(6, 0))
        f, self.bk_tree = scrolled_tree(pane, [("date", "Date", 140, False), ("file", "File", 260, True),
                                                ("size", "Size", 70, False)])
        self._trees.append(self.bk_tree)
        pane.add(f, weight=3)
        self.bk_tree.bind("<<TreeviewSelect>>", lambda e: self._preview_backup())
        self.bk_tree.bind("<Double-1>", lambda e: self._restore())
        f, self.bk_preview = _scrolled_text(pane, self._mono, height=10)
        self._texts.append(self.bk_preview)
        pane.add(f, weight=2)
        _set_text(self.bk_preview, "Select a backup to compare it with the current active list.")

    def refresh_backups(self) -> None:
        def work():
            rows = []
            for p in self.mgr.backups():
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                rows.append((p, st.st_mtime, st.st_size))
            return rows

        self.app.run_bg(work, on_done=self._fill_backups)

    def _fill_backups(self, rows) -> None:
        tree = self.bk_tree
        sel = set(tree.selection())
        tree.delete(*tree.get_children(""))
        for p, mtime, size in rows:
            tree.insert("", "end", iid=p, values=(_fmt_ts(mtime), os.path.basename(p), f"{max(1, size // 1024)} KB"))
        keep = [p for p in sel if tree.exists(p)]
        if keep:
            tree.selection_set(keep)
        folder = os.path.dirname(rows[0][0]) if rows else ""
        self.bk_summary.configure(text=f"{len(rows)} backup(s)" + (f" in {folder}" if folder else ""))

    def _selected_backup(self) -> str:
        sel = self.bk_tree.selection()
        return sel[0] if sel else ""

    def _preview_backup(self) -> None:
        path = self._selected_backup()
        if not path:
            return
        if path in self._previews:         # the parsed file is cached; the diff is against the active list of now
            self._render_diff(self._diff_text(self._previews[path]))
            return
        _set_text(self.bk_preview, "Reading…")

        def done(ml):
            self._previews[path] = ml
            if self._selected_backup() == path:
                self._render_diff(self._diff_text(ml))

        self.app.run_bg(self.mgr.import_path, path, on_done=done,
                        on_error=lambda e: _set_text(self.bk_preview, f"Could not read this backup: {e}"))

    def _diff_text(self, ml) -> str:
        ids = [(r.package_id or r.workshop_id).lower() for r in ml.mods]
        names = {(r.package_id or r.workshop_id).lower(): r.name for r in ml.mods}
        cur = [u.lower() for u in self.mgr.active]
        cur_set, bk_set = set(cur), set(ids)
        added = [i for i in ids if i not in cur_set]      # restoring would add these
        removed = [u for u in cur if u not in bk_set]      # restoring would remove these
        lines = [f"# {len(ids)} mods in this backup, {len(cur)} active now", ""]
        if not added and not removed:
            lines.append("Same mods as the current active list" + (" (different order)." if ids != cur else "."))
        if added:
            lines.append(f"# Restoring adds {len(added)}:")
            lines += [f"+ {names.get(i) or i}  [{i}]" for i in added]
            lines.append("")
        if removed:
            lines.append(f"# Restoring removes {len(removed)}:")
            lines += [f"- {self._mod_name(u)}  [{u}]" for u in removed]
        return "\n".join(lines)

    def _render_diff(self, text: str) -> None:
        w = self.bk_preview
        w.configure(state="normal")
        w.delete("1.0", "end")
        for line in text.split("\n"):
            tag = {"+": "add", "-": "remove", "#": "head"}.get(line[:1], "")
            w.insert("end", line + "\n", (tag,) if tag else ())
        w.configure(state="disabled")

    def _restore(self) -> None:
        path = self._selected_backup()
        if not path:
            self.app.toast("Select a backup first")
            return
        msg = (f"Replace the game's ModsConfig.xml with\n{os.path.basename(path)}?\n\n"
               "The current file is backed up first.")
        if getattr(self.mgr, "dirty", False):
            msg += "\nUnsaved changes in the Mods tab will be lost."
        if not self.app.ask_yes_no("Restore backup", msg):
            return

        def work():
            if self.mgr.is_game_running():
                from rmm.core import GameRunningError
                raise GameRunningError("RimWorld is running. Close it first: it rewrites ModsConfig.xml when it exits.")
            self.mgr.restore_backup(path)

        def done(_r):
            self._previews.clear()
            self.app.emit("active_changed")
            self.app.toast("Backup restored")
            self.refresh_backups()

        self.app.run_bg(work, on_done=done, busy="Restoring backup…",
                        on_error=lambda e: self.app.error("Restore failed", str(e)))

    def _backup_dir(self) -> str:
        path = self._selected_backup() or next(iter(self.bk_tree.get_children("")), "")
        if path:
            return os.path.dirname(path)
        d = os.path.join(self.mgr.data_dir, "backups")
        os.makedirs(d, exist_ok=True)
        return d

    def _open_backup_folder(self) -> None:
        os.startfile(self._backup_dir())

    def _open_backup_file(self) -> None:
        path = self._selected_backup()
        if path and os.path.isfile(path):
            os.startfile(path)
        else:
            self.app.toast("Select a backup first")

    # ================================================================== Database
    def _build_db(self) -> None:
        self._db_prog = None
        self._db_busy = False
        page = self._page("Database", self.refresh_db)
        ttk.Label(page, text="Mod database", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(page, text="RimSort's community databases: every workshop mod's package id, dependencies and "
                             "load-order rules. Used for sorting, dependency fixes, search and the AI generator.",
                  style="Muted.TLabel", wraplength=720, justify="left").pack(anchor="w", pady=(0, 8))
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        self.db_btns = [
            ttk.Button(bar, text="Update now", style="Accent.TButton", command=lambda: self.update_db(False)),
            ttk.Button(bar, text="Force full update", command=lambda: self.update_db(True)),
            ttk.Button(bar, text="Import steamDB.json…", command=lambda: self._import_db("steam")),
            ttk.Button(bar, text="Import communityRules.json…", command=lambda: self._import_db("rules")),
        ]
        for i, b in enumerate(self.db_btns):
            b.pack(side="left", padx=(0 if i == 0 else 4, 0))
        ttk.Button(bar, text="Open app data folder", command=self._open_app_dir).pack(side="left", padx=4)
        self.db_pbar = ttk.Progressbar(page, mode="determinate", maximum=1.0)
        self.db_pbar.pack(fill="x", pady=(10, 2))
        self.db_msg = ttk.Label(page, text="", style="Muted.TLabel", wraplength=720, justify="left")
        self.db_msg.pack(anchor="w")
        # the facts go under the buttons: a short window cuts facts, never the buttons
        grid = ttk.Frame(page)
        grid.pack(anchor="w", pady=(8, 0))
        self._db_vals: dict[str, ttk.Label] = {}
        for r, (key, label) in enumerate((("steam", "Steam Workshop DB"), ("rules", "Community rules"),
                                          ("uti", "Use This Instead"), ("nvw", "No Version Warning"),
                                          ("updated", "Last updated"), ("source", "Source"), ("dir", "App data folder"))):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="w", padx=(0, 16), pady=2)
            self._db_vals[key] = ttk.Label(grid, text="…", wraplength=640, justify="left")
            self._db_vals[key].grid(row=r, column=1, sticky="w", pady=2)
        ttk.Label(page, text="Manual import: use a steamDB.json / communityRules.json you downloaded yourself "
                             "(e.g. from RimSort's GitHub) when automatic downloads are blocked.",
                  style="Muted.TLabel", wraplength=720, justify="left").pack(anchor="w", pady=(12, 0))

    def refresh_db(self) -> None:
        days = self.mgr.settings.get("db_auto_update_days", 3)

        def work():
            st = self.mgr.db.status()
            try:
                due = self.mgr.db.needs_update(float(days)) if days else False
            except (TypeError, ValueError):
                due = False
            from rmm import config
            return st, due, config.APP_DIR

        self.app.run_bg(work, on_done=self._show_db_status)

    def _show_db_status(self, res) -> None:
        st, due, app_dir = res
        n = lambda k: f"{int(st.get(k) or 0):,}"
        v = self._db_vals
        ver = _fmt_ts(st.get("steam_version"))
        v["steam"].configure(text=f"{n('steam_count')} mods" + (f" · version {ver}" if ver else ""))
        ts = _fmt_ts(st.get("rules_timestamp"))
        v["rules"].configure(text=f"{n('rules_count')} mods with rules" + (f" · {ts}" if ts else ""))
        v["uti"].configure(text=f"{n('uti_count')} replacement suggestions")
        v["nvw"].configure(text=f"{n('nvw_count')} mods that work on any version")
        upd = st.get("updated_at") or 0
        text = f"{_fmt_ts(upd)} ({ago(upd)})" if upd else "never"
        if due:
            text += " · update due"
        c = getattr(self.app, "colors", None) or {}
        v["updated"].configure(text=text, foreground=c.get("warn", "#e0a526") if due or not upd else "")
        v["source"].configure(text=st.get("source") or "-")
        v["dir"].configure(text=app_dir)

    def update_db(self, force: bool) -> None:
        if self._db_busy:
            return
        self._db_busy = True
        for b in self.db_btns:
            b.configure(state="disabled")
        self.db_msg.configure(text="Updating…")

        def prog(f=None, t=""):       # worker thread: picked up by _db_poll
            self._db_prog = (f, t)

        # App.update_db owns the job (one update, one Cancel), the toast and the 'db_updated' event
        self.app.update_db(force=force, on_progress=prog,
                           on_done=lambda s: self._db_done(str(s or "Database updated."), notify=False),
                           on_error=lambda e: self._db_done(f"Update failed: {e}", error=True, notify=False))
        self._db_poll()

    def _db_poll(self) -> None:
        if not self._db_busy:          # a late progress report must not overwrite the result message
            return
        p, self._db_prog = self._db_prog, None
        if p:
            f, t = p
            if f is not None:
                self.db_pbar.configure(value=max(0.0, min(1.0, f)))
            if t:
                self.db_msg.configure(text=t)
        self.after(150, self._db_poll)

    def _db_done(self, msg: str, error: bool = False, notify: bool = True) -> None:
        self._db_busy = False
        for b in self.db_btns:
            b.configure(state="normal")
        self.db_pbar.configure(value=0)
        self.db_msg.configure(text=msg)
        if notify and error:            # imports only: App.update_db reports updates itself
            self.app.toast(msg, level="error")
        elif notify:
            self.app.emit("db_updated")
            self.app.toast(msg.splitlines()[0], level="ok")
        self.refresh_db()

    def _import_db(self, kind: str) -> None:
        title = "steamDB.json" if kind == "steam" else "communityRules.json"
        path = filedialog.askopenfilename(parent=self, title=f"Import {title}", initialdir=self._rimsort_dbs(),
                                          filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        fn = self.mgr.db.import_steamdb_file if kind == "steam" else self.mgr.db.import_rules_file
        for b in self.db_btns:
            b.configure(state="disabled")
        self._db_busy = True
        self.app.run_bg(fn, path, on_done=lambda n: self._db_done(f"Imported {n:,} entries from {os.path.basename(path)}."),
                        on_error=lambda e: self._db_done(f"Import failed: {e}", error=True), busy=f"Importing {title}…")

    @staticmethod
    def _rimsort_dbs() -> str:
        try:
            from rmm import paths
            return paths.rimsort_dbs_dir() or os.path.expanduser("~")
        except Exception:
            return os.path.expanduser("~")

    def _open_app_dir(self) -> None:
        from rmm import config
        os.startfile(config.APP_DIR)

    # ================================================================== User rules
    def _build_rules(self) -> None:
        self._choices: list[str] = []
        self._names: dict[str, str] = {}
        page = self._page("User rules", self._show_rules_page)
        ttk.Label(page, text="Your own load-order rules", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(page, text="Applied by Auto-sort and Check together with the community rules. Pick a mod or type a "
                             "package id.", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        form = ttk.Frame(page)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)
        form.columnconfigure(2, weight=1)
        self.rule_a = tk.StringVar()
        self.rule_rel = tk.StringVar(value=RELATIONS[0][0])
        self.rule_b = tk.StringVar()
        self.rule_comment = tk.StringVar()
        self.cb_a = ttk.Combobox(form, textvariable=self.rule_a, postcommand=lambda: self._filter_choices(self.cb_a))
        self.cb_a.grid(row=0, column=0, sticky="ew")
        rel = ttk.Combobox(form, textvariable=self.rule_rel, values=[r[0] for r in RELATIONS], state="readonly", width=24)
        rel.grid(row=0, column=1, padx=6)
        rel.bind("<<ComboboxSelected>>", lambda e: self._rel_changed())
        self.cb_b = ttk.Combobox(form, textvariable=self.rule_b, postcommand=lambda: self._filter_choices(self.cb_b))
        self.cb_b.grid(row=0, column=2, sticky="ew")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Comment").pack(side="left")
        ttk.Entry(row, textvariable=self.rule_comment).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Add rule", style="Accent.TButton", command=self._add_rule).pack(side="left")

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(12, 4))
        self.rules_label = ttk.Label(bar, text="Rules", style="Heading.TLabel")
        self.rules_label.pack(side="left")
        ttk.Button(bar, text="Open file", command=self._open_rules_file).pack(side="right")
        ttk.Button(bar, text="Import RimSort user rules…", command=self._import_rimsort_rules).pack(side="right", padx=4)
        ttk.Button(bar, text="Remove selected", command=self._remove_rules).pack(side="right")
        f, self.rules_tree = scrolled_tree(page, [("a", "Mod", 260, True), ("rel", "Rule", 170, False),
                                                   ("b", "Other mod", 260, True), ("comment", "Comment", 200, True)])
        self._trees.append(self.rules_tree)
        f.pack(fill="both", expand=True)
        self.rules_tree.bind("<Delete>", lambda e: self._remove_rules())

    def _show_rules_page(self) -> None:
        if not self._choices:
            self._refresh_mod_choices()
        self._fill_rules()

    def _refresh_mod_choices(self) -> None:
        names: dict[str, str] = {}
        for m in list((self.mgr.installed or {}).values()):
            if m.package_id and m.package_id not in names:
                names[m.package_id] = m.name or m.package_id
        self._names = names
        self._choices = sorted((f"{n}  [{pid}]" for pid, n in names.items()), key=str.lower)
        if self.rules_tree.winfo_ismapped():
            self._fill_rules()

    def _filter_choices(self, cb: ttk.Combobox) -> None:
        q = cb.get().strip().lower()
        vals = [c for c in self._choices if q in c.lower()] if q else self._choices
        cb.configure(values=vals[:500])

    def _rel_changed(self) -> None:
        flag = dict((l, k) for l, k in RELATIONS)[self.rule_rel.get()] in _FLAG_KEYS
        self.cb_b.configure(state="disabled" if flag else "normal")

    @staticmethod
    def _pid_of(text: str) -> str:
        text = text.strip()
        if text.endswith("]") and "[" in text:
            text = text[text.rfind("[") + 1:-1]
        return text.strip().lower()

    def _label(self, pid: str) -> str:
        n = self._names.get(pid)
        return f"{n}  [{pid}]" if n else pid

    def _fill_rules(self) -> None:
        tree = self.rules_tree
        tree.delete(*tree.get_children(""))
        rules = self.mgr.user_rules or {}
        n = 0
        for pid in sorted(rules):
            r = rules[pid] or {}
            for k in _PAIR_KEYS:
                for other, comment in sorted((r.get(k) or {}).items()):
                    tree.insert("", "end", iid=f"{pid}\x1f{k}\x1f{other}",
                                values=(self._label(pid), REL_LABEL[k], self._label(other), _comment(comment)))
                    n += 1
            for k in _FLAG_KEYS:
                if r.get(k):
                    tree.insert("", "end", iid=f"{pid}\x1f{k}\x1f", values=(self._label(pid), REL_LABEL[k], "", ""))
                    n += 1
        self.rules_label.configure(text=f"Rules ({n})")

    def _add_rule(self) -> None:
        a = self._pid_of(self.rule_a.get())
        kind = dict((l, k) for l, k in RELATIONS)[self.rule_rel.get()]
        b = "" if kind in _FLAG_KEYS else self._pid_of(self.rule_b.get())
        if not a or " " in a or (kind in _PAIR_KEYS and (not b or " " in b)):
            self.app.error("User rules", "Pick the mod(s) from the list or type a package id (no spaces).")
            return
        if a == b:
            self.app.error("User rules", "A mod can't have a rule with itself.")
            return
        rules = rules_from_json(self.mgr.user_rules or {})
        e = rules.setdefault(a, _empty_rule())
        if kind in _FLAG_KEYS:
            e[kind] = True
            e["load_bottom" if kind == "load_top" else "load_top"] = False
        else:
            e[kind][b] = self.rule_comment.get().strip()
            opposite = {"load_after": "load_before", "load_before": "load_after"}.get(kind)
            if opposite:  # drop a contradicting rule (A before B vs A after B)
                e[opposite].pop(b, None)
        self._save_rules(rules)
        self.rule_comment.set("")
        self.app.toast("Rule added; run Auto-sort to apply it")

    def _remove_rules(self) -> None:
        sel = self.rules_tree.selection()
        if not sel:
            return
        rules = rules_from_json(self.mgr.user_rules or {})
        for iid in sel:
            pid, kind, other = iid.split("\x1f")
            e = rules.get(pid)
            if not e:
                continue
            if kind in _FLAG_KEYS:
                e[kind] = False
            else:
                e[kind].pop(other, None)
            if not any(e.values()):
                del rules[pid]
        self._save_rules(rules)

    def _save_rules(self, rules: dict) -> None:
        """Swap in the new dict (copy-on-write: a sort running in the background keeps its old dict) and persist."""
        self.mgr.user_rules = rules
        self._fill_rules()

        self.app.run_bg(self.mgr.save_user_rules, on_error=lambda e: self.app.error("User rules", f"Could not save user rules:\n{e}"))

    def _import_rimsort_rules(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Import user rules (RimSort userRules.json)",
                                          initialdir=self._rimsort_dbs(),
                                          filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return

        def work():
            with open(path, encoding="utf-8-sig") as f:
                return rules_from_json(json.load(f))

        def done(extra):
            rules, added = merge_rules(self.mgr.user_rules or {}, extra)
            self._save_rules(rules)
            self.app.toast(f"Imported {added} new rule(s)")

        self.app.run_bg(work, on_done=done, busy="Importing rules…")

    def _open_rules_file(self) -> None:
        path = os.path.join(self.mgr.data_dir, "user_rules.json")
        if not os.path.isfile(path):
            self.mgr.save_user_rules()
        os.startfile(path)

    # ================================================================== Unused mods
    def _build_unused(self) -> None:
        self._unused: dict = {}            # uid -> Mod from the last Find
        page = self._page("Unused mods", lambda: None)
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        ttk.Button(bar, text="Find", style="Accent.TButton", command=self.find_unused).pack(side="left")
        ttk.Button(bar, text="Delete selected…", command=self._delete_unused).pack(side="left", padx=4)
        self.un_summary = ttk.Label(bar, text="", style="Muted.TLabel")
        self.un_summary.pack(side="right")
        ttk.Label(page, text="Installed mods that are not active and not in any saved modlist. Core and DLCs are "
                             "never listed.", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        f, self.un_tree = scrolled_tree(page, [("name", "Mod", 300, True), ("id", "Package id", 220, True),
                                               ("source", "Source", 80, False), ("mb", "Size (MB)", 80, False)])
        self._trees.append(self.un_tree)
        f.pack(fill="both", expand=True, pady=(6, 0))

    def find_unused(self) -> None:
        self.un_summary.configure(text="Searching…")
        self.app.run_bg(self.mgr.unused_mods, busy="Finding unused mods…", on_done=self._fill_unused,
                        on_error=lambda e: (self.un_summary.configure(text=""), self.app.error("Find failed", str(e))))

    def _fill_unused(self, rows) -> None:
        tree = self.un_tree
        tree.delete(*tree.get_children(""))
        self._unused = {m.uid: m for m, _ in rows}
        for m, size in sorted(rows, key=lambda r: -r[1]):
            tree.insert("", "end", iid=m.uid, values=(m.name, m.package_id, m.source, f"{size / 1024 ** 2:.1f}"))
        total = sum(n for _, n in rows)
        self.un_summary.configure(text=f"{len(rows)} unused mod(s), "
                                       + (f"{total / 1024 ** 3:.2f} GB" if total >= 1024 ** 3 else f"{total / 1024 ** 2:.1f} MB"))

    def _clear_unused(self) -> None:
        if self._unused:
            self.un_tree.delete(*self.un_tree.get_children(""))
            self._unused = {}
            self.un_summary.configure(text="Mods changed, press Find again")

    def _delete_unused(self) -> None:
        cur, used = self.mgr.installed, set(self.mgr.active)
        sel = [m for u in self.un_tree.selection() if (m := self._unused.get(u))]
        # re-check against the current state: uids can be renumbered by a rescan, mods activated since Find
        mods = [c for m in sel if (c := cur.get(m.uid)) and m.uid not in used
                and os.path.normcase(c.path) == os.path.normcase(m.path)]
        if len(mods) < len(sel):
            self.app.toast("The list is out of date, press Find again", "warn")
            return
        tab = self.app.tabs.get("Mods")
        if not mods:
            self.app.toast("Find unused mods, then select the ones to delete")
        elif hasattr(tab, "delete_mods"):
            tab.delete_mods(mods, on_deleted=self.find_unused)

    # ================================================================== Textures (todds)
    def _build_textures(self) -> None:
        page = self._page("Textures", lambda: None)
        ttk.Label(page, text="Texture optimization", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(page, text="Converts each mod's PNG textures to DDS with todds, the encoder RimSort uses. RimWorld "
                             "1.5+ loads DDS directly: the game starts faster and uses less video memory. Later runs "
                             "only convert new or changed textures. The first run downloads todds (4 MB) from GitHub.",
                  style="Muted.TLabel", wraplength=720, justify="left").pack(anchor="w", pady=(0, 8))
        bar = ttk.Frame(page)
        bar.pack(fill="x")
        self.tx_btns = [ttk.Button(bar, text="Optimize textures", style="Accent.TButton",
                                   command=lambda: self.run_todds(False)),
                        ttk.Button(bar, text="Remove optimized textures…", command=lambda: self.run_todds(True))]
        for i, b in enumerate(self.tx_btns):
            b.pack(side="left", padx=(0 if i == 0 else 4, 0))
        ttk.Label(bar, text="Mods").pack(side="left", padx=(12, 4))
        self.tx_scope = tk.StringVar(value="Active mods")
        ttk.Combobox(bar, textvariable=self.tx_scope, values=("Active mods", "All installed mods"), state="readonly",
                     width=18).pack(side="left")
        self.tx_msg = ttk.Label(page, text="", wraplength=720, justify="left")
        self.tx_msg.pack(anchor="w", pady=(10, 0))

    def run_todds(self, clean: bool) -> None:
        from rmm import textures
        mods = self.mgr.installed
        uids = list(self.mgr.active) if self.tx_scope.get() == "Active mods" else list(mods)
        folders = [mods[u].path for u in uids if u in mods and not mods[u].is_official and mods[u].path]
        if not folders:
            self.app.toast("No mods to process", "warn")
            return
        if clean and not self.app.ask_yes_no(
                "Remove optimized textures", f"Delete the .dds textures that have a matching .png in {len(folders)} "
                "mod(s)?\n\nThis includes .dds files a mod shipped itself; downloading that mod again restores them."):
            return
        for b in self.tx_btns:
            b.state(["disabled"])
        self.tx_msg.configure(text="Working… progress and Cancel are in the status bar.")

        def end(msg: str, level: str) -> None:
            for b in self.tx_btns:
                b.state(["!disabled"])
            self.tx_msg.configure(text=msg)
            self.app.toast(msg, level)

        self.app.run_bg(textures.run, folders, clean, busy="Removing optimized textures…" if clean else "Optimizing textures…",
                        progress=True, cancellable=True,
                        on_done=lambda r: end(_todds_summary(r, clean, len(folders)), "warn" if r["status"] else "ok"),
                        on_error=lambda e: end("Cancelled", "warn") if isinstance(e, InterruptedError)
                        else end(f"Failed: {e}", "error"))


def _todds_summary(r: dict, clean: bool, n_mods: int) -> str:
    fixed = f" Removed {r['removed']} half-written file(s)." if r["removed"] else ""
    if r["status"]:
        return f"Stopped ({r['status']}) after {r['done']:,} of {r['total']:,} textures.{fixed}"
    if clean:
        return f"Removed the optimized textures of {n_mods} mod(s).{fixed}"
    if r["total"]:
        return f"Optimized {r['total']:,} texture(s) in {n_mods} mod(s).{fixed}"
    return f"All textures of {n_mods} mod(s) were already optimized.{fixed}"


def _selftest() -> None:
    assert _todds_summary({"done": 5, "total": 9, "status": "cancelled", "removed": 2}, False, 3) == \
        "Stopped (cancelled) after 5 of 9 textures. Removed 2 half-written file(s)."
    assert _todds_summary({"done": 0, "total": 0, "status": "", "removed": 0}, False, 3).startswith("All textures")
    assert _todds_summary({"done": 31596, "total": 31596, "status": "", "removed": 0}, False, 330) == \
        "Optimized 31,596 texture(s) in 330 mod(s)."
    raw = {"timestamp": 1, "rules": {"Some.Mod": {
        "loadAfter": {"Other.Mod": {"name": ["Other"], "comment": ["needs it", "really"]}},
        "loadBefore": {"x.y": {"name": "X"}}, "incompatibleWith": {"bad.mod": {"name": "B", "comment": "crash"}},
        "loadTop": {"value": True}, "loadBottom": {"value": False}}, "empty.mod": {}}}
    r = rules_from_json(raw)
    assert list(r) == ["some.mod"], r
    e = r["some.mod"]
    assert e["load_after"] == {"other.mod": "needs it; really"} and e["load_before"] == {"x.y": ""}
    assert e["incompatible_with"] == {"bad.mod": "crash"} and e["load_top"] is True and e["load_bottom"] is False
    assert rules_from_json(r) == r and rules_from_json(r) is not r        # own format round-trips as a copy
    merged, added = merge_rules(r, {"some.mod": {"load_after": {"other.mod": "", "new.mod": "c"}, "load_bottom": True},
                                    "b.c": {"load_before": {"a.b": ""}}})
    assert added == 3, added
    assert merged["some.mod"]["load_after"] == {"other.mod": "needs it; really", "new.mod": "c"}
    assert r["some.mod"]["load_after"] == {"other.mod": "needs it; really"}  # input untouched
    assert _fmt_ts(0) == "" and _fmt_ts("abc") == "abc" and _fmt_ts(1700000000000).startswith("2023")
    assert ToolsTab._pid_of("Harmony  [brrainz.harmony]") == "brrainz.harmony" and ToolsTab._pid_of(" A.B ") == "a.b"
