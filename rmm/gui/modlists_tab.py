"""Modlists tab: saved mod lists (profiles) — CRUD, edit, apply to active, download missing, import/export/share."""
from __future__ import annotations

import copy
import os
import re
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, simpledialog, ttk

from rmm.gui.theme import ui_scale
from rmm.gui.widgets import add_placeholder, ago, not_ready, style_text, track_ready
from rmm.core import compare_lists
from rmm.models import WORKSHOP_URL, ModList

FORMATS = [  # (fmt for Manager.export_modlist, label, extension, hint)
    ("rml", "RimWorld mod list (.rml)", ".rml", "load it in RimWorld's own mod manager"),
    ("modsconfig", "ModsConfig.xml (.xml)", ".xml", "active-mods file, RimSort/RimPy can import it"),
    ("txt", "Text list (.txt)", ".txt", "name | packageId | workshop link per line"),
    ("json", "RMM modlist (.json)", ".json", "full copy with notes, re-import it here"),
    ("html", "Web page (.html)", ".html", "clickable links + “open all” (browser method)"),
]
IMPORT_TYPES = [("All supported lists", "*.rml *.rws *.xml *.json *.txt"), ("RimWorld mod list", "*.rml"),
                ("RimWorld save game", "*.rws"), ("ModsConfig / XML", "*.xml"), ("JSON (RMM, RimSort, RimPy)", "*.json"),
                ("Text list", "*.txt"), ("All files", "*.*")]
STATUS = {"installed": "✔ Installed", "missing": "✖ Missing", "unknown": "? Unknown", "pending": "…"}


# ---------------------------------------------------------------- dialogs
def _dialog(parent, app, title: str) -> tuple[tk.Toplevel, ttk.Frame]:
    top = tk.Toplevel(parent)
    top.withdraw()
    top.title(title)
    top.configure(background=app.colors["bg"])
    top.transient(parent.winfo_toplevel())
    top.bind("<Escape>", lambda e: top.destroy())
    frm = ttk.Frame(top, padding=16)
    frm.pack(fill="both", expand=True)
    return top, frm


def _show_modal(top: tk.Toplevel, parent, focus: tk.Widget):
    top.update_idletasks()
    p = parent.winfo_toplevel()
    x = p.winfo_rootx() + max(0, (p.winfo_width() - top.winfo_reqwidth()) // 2)
    y = p.winfo_rooty() + max(0, (p.winfo_height() - top.winfo_reqheight()) // 3)
    top.geometry(f"+{x}+{y}")
    top.deiconify()
    top.grab_set()
    focus.focus_set()
    top.wait_window()


def choose(parent, app, title: str, prompt: str, options: list[str], multi: bool = False, ok: str = "OK") -> list[int] | None:
    """Modal list chooser; returns selected indices or None when cancelled."""
    top, frm = _dialog(parent, app, title)
    ttk.Label(frm, text=prompt, wraplength=480, justify="left").pack(anchor="w", pady=(0, 8))
    box = ttk.Frame(frm)
    box.pack(fill="both", expand=True)
    lb = tk.Listbox(box, selectmode="extended" if multi else "browse", height=min(12, max(4, len(options))), width=72,
                    activestyle="none", exportselection=False)
    style_text(lb, app.colors)
    sb = ttk.Scrollbar(box, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    lb.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    for o in options:
        lb.insert("end", o)
    if options:
        lb.selection_set(0)
    if multi:
        ttk.Label(frm, text="Ctrl/Shift-click to pick several.", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
    result: list[int] = []

    def accept(*_):
        result.extend(lb.curselection())
        top.destroy()
    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(12, 0))
    ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right")
    ttk.Button(btns, text=ok, style="Accent.TButton", command=accept).pack(side="right", padx=(0, 8))
    lb.bind("<Double-1>", accept)
    top.bind("<Return>", accept)
    _show_modal(top, parent, lb)
    return result or None


def ask_import_text(parent, app) -> tuple[str, str] | None:
    """Modal: optional name + multi-line text (links, collection links, share codes, ids). Returns (name, text)."""
    top, frm = _dialog(parent, app, "Import from text, links or share code")
    ttk.Label(frm, text="Import from text, links or a share code", style="Heading.TLabel").pack(anchor="w")
    ttk.Label(frm, style="Muted.TLabel", wraplength=560, justify="left",
              text="Paste Steam Workshop links, collection links (expanded automatically), RMM1: share codes, "
                   "packageIds or workshop ids — one per line.").pack(anchor="w", pady=(2, 10))
    name_var = tk.StringVar()
    row = ttk.Frame(frm)
    row.pack(fill="x", pady=(0, 8))
    ttk.Label(row, text="Name").pack(side="left", padx=(0, 8))
    name = ttk.Entry(row, textvariable=name_var)
    name.pack(side="left", fill="x", expand=True)
    add_placeholder(name, "optional — defaults to the collection/share-code name", app)
    box = ttk.Frame(frm)
    box.pack(fill="both", expand=True)
    txt = tk.Text(box, height=12, width=72, wrap="word", padx=8, pady=6, undo=True)
    style_text(txt, app.colors)
    sb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    ph = add_placeholder(txt, "https://steamcommunity.com/sharedfiles/filedetails/?id=2009463077\n"
                              "https://steamcommunity.com/sharedfiles/filedetails/?id=<collection id>\n"
                              "RMM1:eJx…\nbrrainz.harmony\n1541438694", app)
    result: list[tuple[str, str]] = []

    def paste():
        try:
            txt.insert("insert", top.clipboard_get())
        except tk.TclError:
            pass
        ph()

    def accept(*_):
        text = txt.get("1.0", "end-1c").strip()
        if text:
            result.append((name_var.get().strip(), text))
            top.destroy()
    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(12, 0))
    ttk.Button(btns, text="Paste from clipboard", command=paste).pack(side="left")
    ttk.Button(btns, text="Cancel", command=top.destroy).pack(side="right")
    ttk.Button(btns, text="Import", style="Accent.TButton", command=accept).pack(side="right", padx=(0, 8))
    txt.bind("<Control-Return>", lambda e: (accept(), "break")[1])
    _show_modal(top, parent, txt)
    return result[0] if result else None


# ---------------------------------------------------------------- the tab
class ModlistsTab(ttk.Frame):
    MOD_COLS = (("n", "#", 40, "e", False), ("name", "Name", 260, "w", True), ("pid", "Package id", 200, "w", True),
                ("wid", "Workshop id", 100, "w", False), ("status", "Status", 100, "w", False))

    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app, self.mgr = app, app.mgr
        self._names: list[str] = []                 # left tree row iid = index into this
        self._lists: dict[str, ModList] = {}
        self._cur: ModList | None = None
        self._status: dict[int, tuple[str, str | None]] = {}   # id(ModRef) -> (status, installed uid)
        self._want: str | None = None
        self._reload_seq = 0                        # only the newest list_modlists result is applied
        self._save_after = None
        self._dirty = False
        self._edit_seq = 0
        track_ready(self)
        self._build()
        self._apply_colors()
        for ev, cb in (("modlists_changed", self.reload), ("scanned", self._refresh_status),
                       ("theme_changed", self._apply_colors)):
            app.on(ev, cb)
        self._show_current()
        self.reload()

    # ------------------------------------------------------------ layout
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        head = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="Saved Modlists", style="PanelHeading.TLabel").pack(side="left")
        ttk.Label(head, text="  Manage profiles: apply one to your active load order, download missing mods, share or export.",
                  style="PanelMuted.TLabel").pack(side="left")
        pw = ttk.Panedwindow(self, orient="horizontal")
        pw.grid(row=1, column=0, sticky="nsew")

        # left: saved lists
        left = ttk.Frame(pw, style="Card.TFrame", padding=12)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="Profiles", style="PanelHeading.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        lf = ttk.Frame(left, style="Panel.TFrame")
        lf.grid(row=1, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.lists = ttk.Treeview(lf, columns=("name", "count", "updated"), show="headings", selectmode="browse")
        sc = ui_scale(self)                          # column widths are pixels, fonts scale with DPI
        for cid, text, w, anchor, stretch in (("name", "Name", 170, "w", True), ("count", "Mods", 50, "e", False),
                                              ("updated", "Updated", 110, "w", False)):
            self.lists.heading(cid, text=text)
            self.lists.column(cid, width=round(w * sc), minwidth=round(40 * sc), anchor=anchor, stretch=stretch)
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.lists.yview)
        self.lists.configure(yscrollcommand=lsb.set)
        self.lists.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")
        self.lists.bind("<<TreeviewSelect>>", self._on_list_select)
        self.lists.bind("<Delete>", lambda e: self._delete())
        self.lists.bind("<F2>", lambda e: self._rename())
        self.lists.bind("<Button-3>", self._list_menu)
        self.empty_lbl = ttk.Label(left, text="No saved lists yet.\nCreate one from your active mods or import one.",
                                   style="PanelMuted.TLabel", justify="center")
        lb = ttk.Frame(left, style="Panel.TFrame")
        lb.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        lb.columnconfigure((0, 1), weight=1, uniform="lb")
        for i, (text, cmd, style) in enumerate((("New from Active", self._new_from_active, "Accent.TButton"),
                                                ("New Empty", self._new_empty, "TButton"),
                                                ("Import File…", self._import_file, "TButton"),
                                                ("Import Text / URL…", self._import_text, "TButton"))):
            ttk.Button(lb, text=text, command=cmd, style=style).grid(row=i // 2, column=i % 2, sticky="ew",
                                                                     padx=(0 if i % 2 == 0 else 4, 0), pady=(0, 4))
        ttk.Button(lb, text="Load from RimWorld's ModLists…", command=self._from_game).grid(row=2, column=0, columnspan=2,
                                                                                           sticky="ew")
        pw.add(left, weight=1)

        # right: current list
        right = ttk.Frame(pw, style="Card.TFrame", padding=12)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(4, weight=1)
        top = ttk.Frame(right, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        self.b_delete = ttk.Button(top, text="Delete", style="Danger.TButton", command=self._delete)
        self.b_delete.pack(side="right")
        self.b_dup = ttk.Button(top, text="Duplicate", command=self._duplicate)
        self.b_dup.pack(side="right", padx=(0, 4))
        self.b_rename = ttk.Button(top, text="Rename", command=self._rename)
        self.b_rename.pack(side="right", padx=(0, 4))
        self.title_lbl = ttk.Label(top, text="", style="PanelHeading.TLabel")   # packed last: a long name is clipped
        self.title_lbl.pack(side="left", fill="x", expand=True)
        self.meta_lbl = ttk.Label(right, text="", style="PanelMuted.TLabel")
        self.meta_lbl.grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.notes = tk.Text(right, height=3, wrap="word", padx=8, pady=6, undo=True)
        self.notes.grid(row=2, column=0, sticky="ew")
        self._notes_ph = add_placeholder(self.notes, "Notes (saved automatically)…", self.app)
        self.notes.bind("<KeyRelease>", self._on_notes, add="+")
        self.summary_lbl = ttk.Label(right, text="", style="Panel.TLabel")
        self.summary_lbl.grid(row=3, column=0, sticky="w", pady=(8, 4))
        mf = ttk.Frame(right, style="Panel.TFrame")
        mf.grid(row=4, column=0, sticky="nsew")
        mf.columnconfigure(0, weight=1)
        mf.rowconfigure(0, weight=1)
        self.mods = ttk.Treeview(mf, columns=[c[0] for c in self.MOD_COLS], show="headings", selectmode="extended")
        for cid, text, w, anchor, stretch in self.MOD_COLS:
            self.mods.heading(cid, text=text)
            self.mods.column(cid, width=round(w * sc), minwidth=round(30 * sc), anchor=anchor, stretch=stretch)
        msb = ttk.Scrollbar(mf, orient="vertical", command=self.mods.yview)
        self.mods.configure(yscrollcommand=msb.set)
        self.mods.grid(row=0, column=0, sticky="nsew")
        msb.grid(row=0, column=1, sticky="ns")
        self.mods.bind("<Delete>", lambda e: self._remove())
        self.mods.bind("<Alt-Up>", lambda e: self._move(-1))
        self.mods.bind("<Alt-Down>", lambda e: self._move(1))
        self.mods.bind("<Double-1>", self._on_mod_double)
        self.mods.bind("<Button-3>", self._mod_menu)

        edit = ttk.Frame(right, style="Panel.TFrame")
        edit.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self.b_up = ttk.Button(edit, text="▲", width=3, command=lambda: self._move(-1))
        self.b_down = ttk.Button(edit, text="▼", width=3, command=lambda: self._move(1))
        self.b_remove = ttk.Button(edit, text="Remove selected", command=self._remove)
        self.b_replace = ttk.Button(edit, text="Replace with active mods", command=self._replace_with_active)
        for b in (self.b_up, self.b_down, self.b_remove, self.b_replace):
            b.pack(side="left", padx=(0, 4))
        ttk.Label(edit, text="Alt+↑/↓ move · Del removes · double-click opens", style="PanelMuted.TLabel").pack(side="right")

        act = ttk.Frame(right, style="Panel.TFrame")
        act.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        self.b_apply = ttk.Button(act, text="Apply to Active", style="Accent.TButton", command=self._apply)
        self.b_dl = ttk.Button(act, text="Download Missing", command=self._download_missing)
        self.b_cmp = ttk.Button(act, text="Compare with Active", command=self._compare)
        self.b_export = ttk.Button(act, text="Export…", command=self._export)   # incl. RimWorld's ModLists folder
        self.b_share = ttk.Button(act, text="Copy Share Code", command=self._copy_share)
        for b in (self.b_apply, self.b_dl, self.b_cmp):
            b.pack(side="left", padx=(0, 4))
        for b in (self.b_share, self.b_export):
            b.pack(side="right", padx=(4, 0))
        pw.add(right, weight=3)
        self._right_buttons = (self.b_delete, self.b_dup, self.b_rename, self.b_up, self.b_down, self.b_remove,
                               self.b_replace, self.b_apply, self.b_dl, self.b_cmp, self.b_export, self.b_share)

    def _apply_colors(self, *_):
        c = self.app.colors
        style_text(self.notes, c)
        self.mods.tag_configure("installed", foreground=c["ok"])
        self.mods.tag_configure("missing", foreground=c["err"])
        self.mods.tag_configure("unknown", foreground=c["warn"])
        self.mods.tag_configure("pending", foreground=c["muted"])
        if self._cur:
            self._update_summary()

    # ------------------------------------------------------------ loading
    def reload(self, *_):
        self._reload_seq += 1
        seq = self._reload_seq     # overlapping reloads can finish out of order: drop the stale ones
        self.app.run_bg(self.mgr.list_modlists, on_done=lambda ls: seq == self._reload_seq and self._on_lists(ls))

    def _on_lists(self, lists: list[ModList]):
        cur_name = self._cur.name if self._cur else None
        self._lists = {ml.name: ml for ml in lists}
        if self._dirty and cur_name in self._lists:
            self._lists[cur_name] = self._cur          # unsaved edits win over the file copy
        self._names = sorted(self._lists, key=str.lower)
        self.lists.delete(*self.lists.get_children())
        for i, name in enumerate(self._names):
            ml = self._lists[name]
            self.lists.insert("", "end", iid=str(i), values=(name, len(ml.mods), ago(ml.updated or ml.created)))
        if self._names:
            self.empty_lbl.grid_remove()
        else:
            self.empty_lbl.grid(row=2, column=0, pady=(8, 0))
        want = self._want if self._want in self._lists else cur_name if cur_name in self._lists else None
        self._want = None
        if want is None and self._names:
            want = self._names[0]
        if want is None:
            self._cur = None
            self._show_current()
            return
        iid = str(self._names.index(want))
        same = self._cur is self._lists[want]
        self.lists.selection_set(iid)
        self.lists.see(iid)
        if not same:
            self._select(self._lists[want])

    def _on_list_select(self, _e=None):
        sel = self.lists.selection()
        if sel:
            ml = self._lists.get(self._names[int(sel[0])])
            if ml is not None and ml is not self._cur:
                self._select(ml)

    def _select(self, ml: ModList):
        self._flush_save()
        self._cur = ml
        self._status = {}
        self._show_current()
        self._refresh_status()

    def _show_current(self):
        ml = self._cur
        for b in self._right_buttons:
            b.state(["!disabled"] if ml else ["disabled"])
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        if ml is None:
            self.title_lbl.configure(text="No modlist selected")
            self.meta_lbl.configure(text="Pick a list on the left, or create/import one.")
            self.summary_lbl.configure(text="")
            self.mods.delete(*self.mods.get_children())
            self.notes.configure(state="disabled")
            self._notes_ph()
            return
        self.title_lbl.configure(text=ml.name)
        self._update_meta()
        self.notes.insert("1.0", ml.notes or "")
        self.notes.edit_reset()
        self._notes_ph()
        self._render_mods()

    def _update_meta(self):
        ml = self._cur
        bits = [f"source: {ml.source}"]
        if ml.game_version:
            bits.append(f"game {ml.game_version}")
        if ml.created:
            bits.append("created " + time.strftime("%Y-%m-%d", time.localtime(ml.created)))
        if ml.updated:
            bits.append("updated " + ago(ml.updated))
        self.meta_lbl.configure(text=" · ".join(bits))

    def _render_mods(self, select: list[int] | None = None):
        self.mods.delete(*self.mods.get_children())
        installed = getattr(self.mgr, "installed", {})
        for i, r in enumerate(self._cur.mods):
            st, uid = self._status.get(id(r), ("pending", None))
            pid = r.package_id or getattr(installed.get(uid), "package_id", "")   # known once downloaded
            self.mods.insert("", "end", iid=str(i), tags=(st,),
                             values=(i + 1, r.name or r.package_id or r.workshop_id, pid or "—",
                                     r.workshop_id or "—", STATUS.get(st, st)))
        if select:
            self.mods.selection_set([str(i) for i in select])
            self.mods.see(str(select[0]))
        self._update_summary()

    def _update_summary(self):
        mods = self._cur.mods if self._cur else []
        counts = {"installed": 0, "missing": 0, "unknown": 0, "pending": 0}
        for r in mods:
            st = self._status.get(id(r), ("pending",))[0]
            counts[st] = counts.get(st, 0) + 1
        c = self.app.colors
        if counts["pending"] == len(mods) and mods:
            text, color = f"{len(mods)} mods · checking what's installed…", c["muted"]
        else:
            text = f"{len(mods)} mods · {counts['installed']} installed · {counts['missing']} missing"
            if counts["unknown"]:
                text += f" · {counts['unknown']} unknown (no workshop id)"
            color = c["warn"] if counts["missing"] or counts["unknown"] else c["ok"]
        self.summary_lbl.configure(text=text if mods or self._cur is None else "Empty list — add mods from the "
                                   "Workshop or AI tabs, or “Replace with active mods”.", foreground=color if mods else c["muted"])

    def _refresh_status(self, *_):
        ml = self._cur
        if ml is None:
            return
        refs = list(ml.mods)

        def job():
            out = {}  # keyed by our own ref objects: refs_status may hand back copies
            for ref, (r2, st) in zip(refs, self.mgr.refs_status(refs)):
                out[id(ref)] = (st, self.mgr.match_ref(r2) if st == "installed" else None)
            return out

        def done(status):
            if self._cur is ml:
                self._status = status
                sel = [int(i) for i in self.mods.selection()]
                self._render_mods(select=sel or None)
        self.app.run_bg(job, on_done=done, on_error=lambda e: self.app.status(f"Status check failed: {e}"))

    # ------------------------------------------------------------ editing + autosave
    def _changed(self):
        self._cur.updated = time.time()
        self._dirty = True
        self._edit_seq += 1
        if self._save_after:
            self.after_cancel(self._save_after)
        self._save_after = self.after(500, self._flush_save)

    def _flush_save(self):
        if self._save_after:
            self.after_cancel(self._save_after)
            self._save_after = None
        if not self._dirty or self._cur is None:
            return
        cur, ml, seq = self._cur, copy.deepcopy(self._cur), self._edit_seq
        name = ml.name

        def done(_):
            if seq == self._edit_seq:
                self._dirty = False
            if ml.name != name:              # the file held another list: saved under a free name instead
                cur.name = ml.name
                if self._cur is cur:
                    self._want = ml.name
                self.app.emit("modlists_changed")
                return
            iid = str(self._names.index(ml.name)) if ml.name in self._names else None
            if iid and self.lists.exists(iid):
                self.lists.item(iid, values=(ml.name, len(ml.mods), ago(ml.updated)))
            if self._cur is not None and self._cur.name == ml.name:
                self._update_meta()
        self.app.run_bg(self.mgr.save_modlist, ml, on_done=done)

    def flush(self) -> None:
        """App.close() calls this before teardown: write a pending autosave now (a small JSON write)."""
        if self._save_after:
            self.after_cancel(self._save_after)
            self._save_after = None
        if self._dirty and self._cur is not None:   # a failure propagates: App.close asks before losing it
            self.mgr.save_modlist(copy.deepcopy(self._cur))
            self._dirty = False

    def _on_notes(self, _e=None):
        if self._cur is None:
            return
        text = self.notes.get("1.0", "end-1c")
        if text != (self._cur.notes or ""):
            self._cur.notes = text
            self._changed()

    def _selected_idx(self) -> list[int]:
        return sorted(int(i) for i in self.mods.selection())

    def _remove(self):
        sel = self._selected_idx()
        if not self._cur or not sel:
            return
        for i in reversed(sel):
            del self._cur.mods[i]
        self._render_mods(select=[min(sel[0], len(self._cur.mods) - 1)] if self._cur.mods else None)
        self._changed()
        return "break"

    def _move(self, delta: int):
        sel = self._selected_idx()
        mods = self._cur.mods if self._cur else []
        if not sel or (delta < 0 and sel[0] == 0) or (delta > 0 and sel[-1] == len(mods) - 1):
            return "break"
        for i in (sel if delta < 0 else reversed(sel)):
            mods[i], mods[i + delta] = mods[i + delta], mods[i]
        self._render_mods(select=[i + delta for i in sel])
        self._changed()
        return "break"

    def _replace_with_active(self):
        ml = self._cur
        if not ml or not_ready(self) or not self.app.ask_yes_no("Replace with active mods",
                                             f"Replace the {len(ml.mods)} mods in “{ml.name}” with your current active "
                                             f"list ({len(self.mgr.active)} mods)?"):
            return

        def done(new: ModList):
            if self._cur is ml:
                ml.mods = new.mods
                ml.game_version = new.game_version or ml.game_version
                self._status = {}
                self._render_mods()
                self._changed()
                self._refresh_status()
        self.app.run_bg(self.mgr.modlist_from_active, ml.name, on_done=done)

    # ------------------------------------------------------------ CRUD
    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(title, "Name:", initialvalue=initial, parent=self)
        name = re.sub(r"\s+", " ", name or "").strip()
        return name or None

    def _save_new(self, ml: ModList, toast: str):
        """Save under the first free name (never over another list); `toast` gets the final name as {name}."""
        def job():
            ml.name = self.mgr.unique_modlist_name(ml.name)
            self.mgr.save_modlist(ml)

        def done(_):
            self._want = ml.name
            self.app.emit("modlists_changed")
            self.app.toast(toast.format(name=ml.name), "ok")
        self.app.run_bg(job, on_done=done)

    def _new_from_active(self):
        if not_ready(self):
            return
        name = self._ask_name("New modlist from active mods", time.strftime("My modlist %Y-%m-%d"))
        if not name:
            return
        if name in self._lists and not self.app.ask_yes_no("Overwrite?", f"A modlist named “{name}” exists. Overwrite it?"):
            return
        if self._cur and self._cur.name == name and self._save_after:  # drop a pending autosave of the list being replaced
            self.after_cancel(self._save_after)
            self._save_after, self._dirty = None, False

        def job():
            ml = self.mgr.modlist_from_active(name)
            self.mgr.save_modlist(ml)
            return ml

        def done(ml):                        # ml.name differs when the name clashed with another list's file
            self._want = ml.name
            self.app.emit("modlists_changed")
            self.app.toast(f"Saved “{ml.name}” ({len(ml.mods)} mods)", "ok")
        self.app.run_bg(job, on_done=done)

    def _new_empty(self):
        name = self._ask_name("New empty modlist")
        if not name:
            return
        now = time.time()
        self._save_new(ModList(name=name, game_version=self.mgr.game_version, created=now, updated=now),
                       "Created “{name}”")

    def _rename(self):
        ml = self._cur
        if not ml:
            return
        self._flush_save()  # before the dialog, so the autosave has landed before the rename runs
        new = self._ask_name("Rename modlist", ml.name)
        if not new or new == ml.name:
            return
        if new in self._lists:
            self.app.error("Rename modlist", f"A modlist named “{new}” already exists.")
            return
        old = ml.name

        def done(_):
            ml.name = new
            self._want = new
            self.app.emit("modlists_changed")
        self.app.run_bg(self.mgr.rename_modlist, old, new, on_done=done,
                        on_error=lambda e: self.app.error("Rename modlist", str(e) or type(e).__name__))

    def _duplicate(self):
        if not self._cur:
            return
        ml = copy.deepcopy(self._cur)
        ml.name = f"{self._cur.name} (copy)"
        ml.created = ml.updated = time.time()
        self._save_new(ml, "Duplicated as “{name}”")

    def _delete(self):
        ml = self._cur
        if not ml:
            return
        self._flush_save()  # before the dialog, so a late autosave cannot resurrect the file
        if not self.app.ask_yes_no("Delete modlist", f"Delete “{ml.name}” ({len(ml.mods)} mods)? This cannot be undone."):
            return
        idx = self._names.index(ml.name) if ml.name in self._names else 0
        rest = [n for n in self._names if n != ml.name]

        def done(_):
            self._cur = None
            self._want = rest[min(idx, len(rest) - 1)] if rest else None
            self.app.emit("modlists_changed")
            self.app.toast(f"Deleted “{ml.name}”", "info")
        self.app.run_bg(self.mgr.delete_modlist, ml.name, on_done=done,
                        on_error=lambda e: self.app.error("Delete modlist", str(e) or type(e).__name__))

    # ------------------------------------------------------------ apply / download
    def _apply(self):
        ml = self._cur
        if not ml or not_ready(self):
            return
        if not self.app.ask_yes_no("Apply modlist",
                                   f"Replace your active mods with “{ml.name}” ({len(ml.mods)} mods)?\n\n"
                                   "Only the working list changes — press Save in the Mods tab to write it to the game. "
                                   "Ctrl+Z undoes it."):
            return
        self._flush_save()
        self.app.run_bg(self.mgr.apply_modlist, copy.deepcopy(ml), busy="Applying modlist…",
                        on_done=lambda missing: self._after_apply(ml, missing))

    def _after_apply(self, ml: ModList, missing):
        self.app.emit("active_changed")
        n_active = len(self.mgr.active)
        self.app.toast(f"Applied “{ml.name}”: {n_active} active mods" + (f", {len(missing)} missing" if missing else "")
                       + " — review & Save in the Mods tab.", "info")
        if missing:
            self._offer_download(missing, f"{len(missing)} mods from “{ml.name}” are not installed.", activate=True)

    def _offer_download(self, missing, intro: str, activate: bool = False):
        dl = [r for r in missing if r.workshop_id]
        no_id = [r for r in missing if not r.workshop_id]
        msg = intro
        if no_id:
            msg += (f"\n\n{len(no_id)} have no known workshop id (local/unknown mods) and must be installed by hand:\n"
                    + "\n".join(f"• {r.name or r.package_id}" for r in no_id[:10]) + ("\n…" if len(no_id) > 10 else ""))
        if not dl:
            self.app.info("Missing mods", msg)
            return
        if self.app.ask_yes_no("Download missing mods", msg + f"\n\nDownload {len(dl)} mods (and their dependencies) now?"):
            self._queue(dl, activate)

    def _queue(self, dl, activate: bool = False):
        """Manager.download adds the missing dependencies; activate only for the applied list (then it auto-sorts)."""
        self.app.queue_download(dl, activate=activate)
        self.app.toast(f"Queued {len(dl)} downloads + missing dependencies"
                       + (" — they are activated when installed" if activate else "") + ". See the Downloads tab.", "info")

    def _download_missing(self):
        ml = self._cur
        if not ml or not_ready(self):
            return
        self.app.run_bg(self.mgr.missing_refs, list(ml.mods), busy="Checking missing mods…",
                        on_done=lambda missing: self._download_found(ml, missing))

    def _download_found(self, ml, missing):
        if not missing:
            self.app.toast(f"Everything in “{ml.name}” is installed.", "info")
            return
        dl = [r for r in missing if r.workshop_id]
        if len(dl) < len(missing):
            self._offer_download(missing, f"{len(missing)} mods from “{ml.name}” are not installed.")
        else:
            self._queue(dl)

    def _compare(self):
        ml = self._cur
        if not ml or not_ready(self):
            return
        listed = [self.mgr.match_ref(r) for r in ml.mods]
        only_list, only_active, moved = compare_lists(listed, list(self.mgr.active))
        inst = self.mgr.installed
        lines = [f"Only in “{ml.name}” ({len(only_list)}):"]
        for i in only_list:
            r = ml.mods[i]
            lines.append(f"  • {r.name or r.package_id or r.workshop_id}" + ("" if listed[i] else "  (not installed)"))
        lines += ["", f"Only active ({len(only_active)}):"]
        lines += [f"  • {getattr(inst.get(u), 'name', '') or u}" for u in only_active]
        lines += ["", f"Order: {moved} shared mod(s) would have to move to match the list." if moved
                  else "Order: shared mods are in the same order."]
        top, frm = _dialog(self, self.app, f"Compare “{ml.name}” with active")
        txt = tk.Text(frm, width=70, height=24, wrap="word", padx=8, pady=6)
        style_text(txt, self.app.colors)
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)
        close = ttk.Button(frm, text="Close", command=top.destroy)
        close.pack(anchor="e", pady=(8, 0))
        _show_modal(top, self, close)

    # ------------------------------------------------------------ import
    def _import_file(self):
        path = filedialog.askopenfilename(parent=self, title="Import modlist", filetypes=IMPORT_TYPES)
        if path:
            self._import_job(lambda **_: self.mgr.import_path(path), os.path.basename(path))

    def _import_text(self):
        got = ask_import_text(self, self.app)
        if got:
            name, text = got
            self._import_job(lambda progress: self.mgr.import_text(text, progress=progress), "pasted text", name,
                             progress=True)

    def _import_job(self, load, what: str, name: str = "", progress: bool = False, source: str = ""):
        def job(**kw):
            ml = load(**kw)
            if not ml.mods:
                raise ValueError(f"No mods found in {what}.")
            ml.name = self.mgr.unique_modlist_name(name or ml.name or os.path.splitext(what)[0])
            if source:
                ml.source = source
            ml.created = ml.created or time.time()
            ml.updated = time.time()
            self.mgr.save_modlist(ml)
            return ml

        def done(ml):
            self._want = ml.name
            self.app.emit("modlists_changed")
            self.app.toast(f"Imported “{ml.name}” ({len(ml.mods)} mods)", "ok")
        self.app.run_bg(job, progress=progress, busy=f"Importing {what}…", on_done=done,
                        on_error=lambda e: self.app.error("Import failed", f"Could not import {what}:\n\n{e}"))

    def _from_game(self):
        self.app.run_bg(self.mgr.game_modlists, on_done=self._pick_game_lists)

    def _pick_game_lists(self, paths: list[str]):
        if not paths:
            self.app.info("RimWorld ModLists", "No saved lists (.rml) found in RimWorld's ModLists folder.\n\n"
                                               "Save one in-game from the mod manager (Mods → Save list) first.")
            return
        names = [os.path.splitext(os.path.basename(p))[0] for p in paths]
        idx = choose(self, self.app, "Load from RimWorld's ModLists", "Pick the in-game mod lists to import:",
                     names, multi=True, ok="Import")
        for i in idx or []:
            path = paths[i]
            self._import_job(lambda p=path, **_: self.mgr.import_path(p), os.path.basename(path), names[i], source="game")

    # ------------------------------------------------------------ export / share
    def _export(self):
        ml = self._cur
        if not ml:
            return
        idx = choose(self, self.app, "Export modlist", f"Export “{ml.name}” as:",
                     ["RimWorld's ModLists folder  —  load it in-game: Mods → Load list",
                      *(f"{label}  —  {hint}" for _, label, _, hint in FORMATS)], ok="Next…")
        if not idx:
            return
        if idx[0] == 0:
            self._export_game()
            return
        fmt, label, ext, _ = FORMATS[idx[0] - 1]
        safe = re.sub(r'[<>:"/\\|?*]+', "_", ml.name).strip() or "modlist"
        path = filedialog.asksaveasfilename(parent=self, title=f"Export {label}", defaultextension=ext,
                                            initialfile=safe + ext, filetypes=[(label, "*" + ext), ("All files", "*.*")])
        if not path:
            return
        self._flush_save()

        lost = self._no_pid_note(ml) if fmt in ("rml", "modsconfig") else ""

        def done(_):
            self.app.toast(f"Exported to {path}{lost}", "warn" if lost else "ok")
            if fmt == "html" and self.app.ask_yes_no("Open page", "Open the exported page in your browser now?"):
                from rmm.config import open_path
                open_path(path)            # a hand-built file:// URL breaks on '#', '%' or '?' in the path
        self.app.run_bg(self.mgr.export_modlist, copy.deepcopy(ml), path, fmt, busy="Exporting…", on_done=done)

    def _export_game(self):
        ml = self._cur
        if not ml:
            return
        self._flush_save()
        lost = self._no_pid_note(ml)

        def run(overwrite: bool = False):
            self.app.run_bg(self.mgr.export_to_game_modlists, copy.deepcopy(ml), overwrite, busy="Exporting to RimWorld…",
                            on_done=lambda p: self.app.info("Exported to RimWorld",
                                                            f"Saved as {p}\n\nIn-game: Mods → Load list → “{ml.name}”.{lost}"),
                            on_error=failed)

        def failed(e):
            if not isinstance(e, FileExistsError):
                self.app.error("Export to RimWorld failed", str(e) or type(e).__name__)
            elif self.app.ask_yes_no("Export to RimWorld", f"RimWorld already has a saved mod list named “{ml.name}”.\n\n"
                                                           "Replace it? (The old one is backed up first.)"):
                run(overwrite=True)
        run()

    def _no_pid_note(self, ml: ModList) -> str:
        """RimWorld's formats hold packageIds only: name the mods left out because theirs is unknown until downloaded."""
        n = sum(1 for r in ml.mods if not r.package_id and not self._status.get(id(r), ("", None))[1])
        return f" — {n} mod(s) without a known packageId were left out (download them first)" if n else ""

    def _copy_share(self):
        ml = self._cur
        if not ml:
            return
        def done(code):
            self.clipboard_clear()
            self.clipboard_append(code)
            self.app.toast(f"Share code copied ({len(ml.mods)} mods, {len(code)} chars) — paste it into Import text.", "info")
        self.app.run_bg(self.mgr.share_code, copy.deepcopy(ml), busy="Building share code…", on_done=done)

    # ------------------------------------------------------------ mod rows
    def _row(self, iid):
        r = self._cur.mods[int(iid)]
        return r, self._status.get(id(r), ("pending", None))[1]

    def _on_mod_double(self, e):
        iid = self.mods.identify_row(e.y)
        if iid and self._cur:
            r, uid = self._row(iid)
            if uid:
                self.app.show_mod_details(uid=uid)
            elif r.workshop_id:
                webbrowser.open(WORKSHOP_URL.format(r.workshop_id))

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast(f"Copied {text}", "info")

    def _mod_menu(self, e):
        iid = self.mods.identify_row(e.y)
        if not iid or not self._cur:
            return
        if iid not in self.mods.selection():
            self.mods.selection_set(iid)
        r, uid = self._row(iid)
        url = WORKSHOP_URL.format(r.workshop_id)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Show in Mods tab", command=lambda: self.app.show_mod_details(uid=uid),
                         state="normal" if uid else "disabled")
        menu.add_command(label="Open workshop page", command=lambda: webbrowser.open(url),
                         state="normal" if r.workshop_id else "disabled")
        menu.add_command(label="Copy package id", command=lambda: self._copy(r.package_id),
                         state="normal" if r.package_id else "disabled")
        menu.add_separator()
        menu.add_command(label="Move up", command=lambda: self._move(-1))
        menu.add_command(label="Move down", command=lambda: self._move(1))
        menu.add_command(label="Remove from list", command=self._remove)
        menu.tk_popup(e.x_root, e.y_root)

    def _list_menu(self, e):
        iid = self.lists.identify_row(e.y)
        if not iid:
            return
        self.lists.selection_set(iid)
        self._on_list_select()
        menu = tk.Menu(self, tearoff=0)
        for label, cmd in (("Apply to active", self._apply), ("Download missing", self._download_missing), (None, None),
                           ("Rename", self._rename), ("Duplicate", self._duplicate), ("Export…", self._export),
                           ("Copy share code", self._copy_share), (None, None), ("Delete", self._delete)):
            menu.add_separator() if label is None else menu.add_command(label=label, command=cmd)
        menu.tk_popup(e.x_root, e.y_root)


# ---------------------------------------------------------------- self-test
def _selftest() -> None:
    from rmm.gui.workshop_tab import _SmokeApp, _SmokeMgr
    from rmm.models import ModRef
    try:
        root = tk.Tk()
    except tk.TclError:
        return
    root.withdraw()
    try:
        mgr = _SmokeMgr()
        refs = [ModRef("brrainz.harmony", "Harmony", "2009463077"), ModRef("a.b", "Alpha", "1111"), ModRef("x.local", "Local")]
        mgr.lists["Base"] = ModList("Base", refs, created=1.0, updated=2.0)
        mgr.refs_status = lambda rs: [(r, "installed" if mgr.match_ref(r) else ("missing" if r.workshop_id else "unknown"))
                                      for r in rs]
        mgr.missing_refs = lambda rs=None: [r for r in rs if not mgr.match_ref(r)]
        mgr.share_code = lambda ml: "RMM1:abc"
        mgr.import_text = lambda text, progress=None: ModList("Imported", [ModRef("", "", "1111")])
        app = _SmokeApp(root, mgr)
        tab = ModlistsTab(root, app)
        assert tab._cur is mgr.lists["Base"], tab._cur
        tab._download_missing()                          # startup scan still running: everything looks missing
        assert not app.queued and "Still loading" in app.toasts[-1], app.toasts
        app.emit("scanned")
        assert [tab.mods.set(i, "status") for i in tab.mods.get_children()] == ["✔ Installed", "✖ Missing", "? Unknown"]
        tab.mods.selection_set("1")
        tab._move(-1)
        assert [r.package_id for r in tab._cur.mods] == ["a.b", "brrainz.harmony", "x.local"]
        assert tab.mods.set("0", "status") == "✖ Missing"   # status follows the ref, not the row index
        tab._flush_save()
        assert not tab._dirty and [r.package_id for r in mgr.lists["Base"].mods][0] == "a.b"
        tab._download_found(tab._cur, mgr.missing_refs(tab._cur.mods))   # x.local has no id → info path, no queue
        assert not app.queued
        tab._download_found(tab._cur, [tab._cur.mods[0]])  # plain "Download missing" must not touch the active list
        assert app.queued[-1] == ([ModRef("a.b", "Alpha", "1111")], None, {"activate": False}), app.queued
        tab._cur.notes = "typed just before closing"
        tab._changed()                                   # autosave pending (500 ms timer) …
        tab.flush()                                      # … App.close() writes it instead of dropping it
        assert tab._save_after is None and not tab._dirty and mgr.lists["Base"].notes == "typed just before closing"
        real_save, tab._dirty = mgr.save_modlist, True
        mgr.save_modlist = lambda ml: (_ for _ in ()).throw(PermissionError("read-only"))
        try:
            tab.flush()
            raise AssertionError("flush must raise")
        except PermissionError:
            assert tab._dirty                            # still unsaved: App.close asks before dropping it
        mgr.save_modlist, tab._dirty = real_save, False
        tab._import_job(lambda **_: mgr.import_text("x"), "pasted text", "base")   # case clash: must not replace "Base"
        assert tab._cur.name == "base (2)" and len(mgr.lists["Base"].mods) == 3, list(mgr.lists)
        tab._import_job(lambda **_: mgr.import_text("x"), "pasted text", "")
        assert tab._cur.name == "Imported" and "Imported" in mgr.lists
        tab._duplicate()
        assert "Imported (copy)" in mgr.lists and "“Imported (copy)”" in app.toasts[-1]
        tab._copy_share()
        assert root.clipboard_get() == "RMM1:abc"
        calls = []

        def export(ml, overwrite=False):
            calls.append(overwrite)
            if not overwrite:
                raise FileExistsError(ml.name)
            return "ModLists/x.rml"
        mgr.export_to_game_modlists, app.ask_yes_no = export, lambda t, m: True
        g = globals()
        real_choose, g["choose"] = g["choose"], lambda *a, **k: [0]
        try:
            tab._export()                                # first choice = RimWorld's ModLists; same name → asked, replaced
        finally:
            g["choose"] = real_choose
        assert calls == [False, True] and "x.rml" in app.toasts[-1], (calls, app.toasts[-1:])
        pending, real_bg = [], app.run_bg
        app.run_bg = lambda fn, *a, on_done=None, **k: pending.append(on_done)
        try:
            tab.reload()
            tab.reload()
        finally:
            app.run_bg = real_bg
        pending[1](mgr.list_modlists())
        pending[0]([])                                   # older reload finishing last must not wipe the tree
        assert tab._names and tab.lists.get_children(), tab._names
    finally:
        for j in root.tk.splitlist(root.tk.call("after", "info")):   # placeholder refreshes still pending
            root.tk.call("after", "cancel", j)
        root.destroy()


if __name__ == "__main__":
    _selftest()
    print("modlists_tab selftest ok")
