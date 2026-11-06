"""Main mod manager tab: inactive/active lists (search, filter, drag-and-drop), details pane, issues panel."""
from __future__ import annotations

import math
import os
import re
import struct
import time
from collections import OrderedDict
import tkinter as tk
from tkinter import ttk

from rmm.gui import theme
from rmm.models import CORE_ID, DLC_BY_APPID, DLCS, LEVEL_RANK, LOADABLE_SOURCES, RIMWORLD_APPID, WORKSHOP_URL, DbMod, Issue, Mod, ModRef

SOURCE_LABEL = {"core": "Official", "dlc": "Official", "local": "Local", "workshop": "Workshop",
                "steamcmd": "SteamCMD", "extra": "Extra"}
FILTERS = ["All", "Local", "Workshop", "SteamCMD", "Official", "Outdated"]
LEVEL_ICON = {"error": "✖", "warning": "⚠", "info": "ℹ"}
SAFE_ACTIONS = {"add", "sort", "download"}      # "Fix all safe" never removes or replaces mods
PREVIEW_MAX_W = 320
PREVIEW_CACHE = 40
PREVIEW_MAX_PIXELS = 8_300_000        # 4K UHD: decoding (Tk thread only) stays around 0.1-0.3 s and ~32 MB
PREVIEW_MAX_BYTES = 24 << 20
_TAG_RE = re.compile(r"<[^>]{1,80}>|\[/?(?:b|i|u|h\d|url[^\]]*|img|list|\*|quote|code|spoiler|strike)\]", re.I)
SHIFT, CTRL = 0x1, 0x4


def read_preview(path: str) -> bytes | str | None:
    """Worker thread: the image file's bytes when it is a PNG/GIF small enough to decode on the Tk thread, a reason
    string when it is not shown, None when there is no preview. Size comes from the PNG IHDR / GIF header."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
            elif head[:4] == b"GIF8":
                w, h = struct.unpack("<HH", head[6:10])
            else:
                return "preview is not PNG/GIF (not shown)"
            if w * h > PREVIEW_MAX_PIXELS or os.fstat(fh.fileno()).st_size > PREVIEW_MAX_BYTES:
                return "preview image too large to show"
            return head + fh.read() if w and h else "preview could not be decoded"
    except FileNotFoundError:
        return None
    except (OSError, struct.error):
        return "preview could not be read"


class SearchEntry(ttk.Entry):
    """ttk.Entry with a muted placeholder and a debounced change callback (Esc clears)."""

    def __init__(self, parent, placeholder: str, on_change, **kw):
        self.var = tk.StringVar()
        super().__init__(parent, textvariable=self.var, **kw)
        self._ph, self._on_change, self._showing, self._after = placeholder, on_change, False, None
        self.bind("<FocusIn>", self._focus_in, add="+")
        self.bind("<FocusOut>", self._focus_out, add="+")
        self.bind("<Escape>", lambda e: self.clear())
        self.var.trace_add("write", self._changed)
        self._focus_out()

    def query(self) -> str:
        return "" if self._showing else self.var.get().strip().lower()

    def clear(self) -> None:
        if not self._showing and self.var.get():
            self.var.set("")
            if self.focus_get() is not self:
                self._focus_out()

    def _focus_in(self, _e=None) -> None:
        if self._showing:
            self.var.set("")
            self._showing = False
            self.configure(style="TEntry")

    def _focus_out(self, _e=None) -> None:
        if not self.var.get():
            self._showing = True
            self.configure(style="Placeholder.TEntry")
            self.var.set(self._ph)

    def _changed(self, *_a) -> None:
        if self._showing:
            return
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(150, self._fire)

    def _fire(self) -> None:
        self._after = None
        self._on_change()


class ModsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self.mgr = app, app.mgr
        self.s = theme.ui_scale(self)
        self._iid: dict[str, str] = {}          # uid -> tree iid (shared by both lists: a uid lives in exactly one)
        self._uid: dict[str, str] = {}          # iid -> uid
        self._rows: dict[str, dict] = {}         # tree path -> {iid: (values, tags)} as currently displayed
        self._sync_jobs: dict[str, str] = {}
        self._by_pid: dict[str, list[str]] = {}
        self._by_wid: dict[str, str] = {}
        self._hay: dict[str, str] = {}
        self._active_set: set[str] = set()
        self._issues: list[Issue] = []
        self._issue_level: dict[str, str] = {}
        self._check_after = self._check_running = self._check_pending = self._announce_check = None
        self._preview_cache: OrderedDict[str, tk.PhotoImage | str | None] = OrderedDict()
        self._preview_loading: set[str] = set()
        self._detail: tuple[str | None, DbMod | None] = (None, None)
        self._detail_after = None
        self._rendered: tuple = (None, None)
        self._links: list[str] = []
        self._drag: dict | None = None
        self._scroll_job = None
        self._sort = ("name", False)
        self._menu = tk.Menu(self, tearoff=0)
        self._build()
        self._apply_colors()
        app.on("scanned", self._on_scanned)
        app.on("active_changed", self._on_active_changed)
        app.on("downloads_done", self._on_scanned)
        app.on("db_updated", lambda *a: self.schedule_check())
        app.on("settings_changed", lambda *a: self.refresh())
        app.on("theme_changed", self._apply_colors)
        app.on("dirty_changed", lambda *a: self._update_dirty())
        self._reindex()
        self.refresh()
        self._render_details()          # the "Select a mod…" tips

    def p(self, n: float) -> int:
        return max(1, round(n * self.s))

    # ================================================================ layout
    def _build(self) -> None:
        p = self.p
        bar = self._bar = ttk.Frame(self, padding=(p(8), p(8), p(8), p(6)))
        bar.pack(fill="x")
        ttk.Button(bar, text="▶ Launch game", style="Success.TButton", command=self.launch).pack(side="right")   # packed first: never clipped
        ttk.Button(bar, text="⟳ Rescan", command=self.app.rescan).pack(side="right", padx=(0, p(4)))
        groups = []
        for items in ((("Save", self.save, "Accent.TButton"), ("Revert", self.revert, None), None,
                       ("Undo", self.app.undo, None), ("Redo", self.app.redo, None)),
                      (("Auto-sort", self.auto_sort, None), ("Fix dependencies", self.fix_dependencies, None),
                       ("Check", lambda: self.run_check(announce=True), None))):
            g = ttk.Frame(bar)
            g.pack(side="left", padx=(p(10) if groups else 0, 0))
            groups.append(g)
            for item in items:
                if item is None:
                    ttk.Frame(g, width=p(10)).pack(side="left")
                    continue
                text, cmd, style = item
                ttk.Button(g, text=text, command=cmd, **({"style": style} if style else {})).pack(side="left", padx=(0, p(4)))
        self._tools, self._wrapped = groups[1], False
        self.lbl_dirty = ttk.Label(bar, text="● Unsaved changes", style="Warn.TLabel")
        self._dirty_w = self.lbl_dirty.winfo_reqwidth()     # reserved, so saving/editing never re-wraps the bar
        self.lbl_dirty.configure(text="")
        self.lbl_dirty.pack(side="left", padx=(p(8), 0))
        bar.bind("<Configure>", self._wrap_toolbar, add="+")

        vpane = ttk.PanedWindow(self, orient="vertical")
        vpane.pack(fill="both", expand=True, padx=p(8), pady=(0, p(8)))
        hpane = ttk.PanedWindow(vpane, orient="horizontal")
        vpane.add(hpane, weight=4)
        hpane.add(self._build_inactive(hpane), weight=3)
        hpane.add(self._build_active(hpane), weight=3)
        hpane.add(self._build_details(hpane), weight=3)
        vpane.add(self._build_issues(vpane), weight=1)
        self._vpane, self._hpane = vpane, hpane
        self._sash_tries = 0
        self.bind("<Map>", self._init_sashes, add="+")

    def _init_sashes(self, _e=None) -> None:
        w, h = self._hpane.winfo_width(), self._vpane.winfo_height()
        if w < 200 or h < 200:
            self._sash_tries += 1
            if self._sash_tries < 30:
                self.after(50, self._init_sashes)
            return
        self.unbind("<Map>")
        self._hpane.sashpos(0, int(w * 0.31))
        self._hpane.sashpos(1, int(w * 0.64))
        self._vpane.sashpos(0, max(int(h * 0.65), h - self.p(180)))   # the mod lists get the room on small screens

    def _wrap_toolbar(self, _e=None) -> None:
        """Auto-sort / Fix dependencies / Check move to a second toolbar row when one row does not fit."""
        bar, g = self._bar, self._tools
        if bar.winfo_width() <= 1:
            return
        need = sum(w.winfo_reqwidth() for w in bar.pack_slaves() if w is not self.lbl_dirty) + self._dirty_w
        wrap = bar.winfo_width() < need + self.p(40)      # + paddings
        if wrap != self._wrapped:
            self._wrapped = wrap
            if wrap:        # side=bottom packed first = a full-width row under the others
                g.pack(side="bottom", anchor="w", padx=0, pady=(self.p(6), 0), before=bar.pack_slaves()[0])
            else:
                g.pack(side="left", anchor="center", padx=(self.p(10), 0), pady=0, before=self.lbl_dirty)

    def _make_tree(self, parent, cols, sortable=False) -> ttk.Treeview:
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True)
        tree = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", selectmode="extended", height=6)
        for cid, title, width, stretch in cols:
            kw = {"command": lambda c=cid: self._sort_by(c)} if sortable else {}
            tree.heading(cid, text=title, anchor="w", **kw)
            tree.column(cid, width=self.p(width), minwidth=self.p(30), stretch=stretch,
                        anchor="e" if cid == "idx" else "w")
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self._rows[str(tree)] = {}
        return tree

    def _header(self, parent, title: str) -> ttk.Label:
        hdr = ttk.Frame(parent, style="Panel.TFrame")
        hdr.pack(fill="x", pady=(0, self.p(2)))
        ttk.Label(hdr, text=title, style="PanelHeading.TLabel").pack(side="left")
        cnt = ttk.Label(hdr, style="Pill.TLabel")
        cnt.pack(side="left", padx=(self.p(8), 0))
        return cnt

    def _build_inactive(self, parent) -> ttk.Frame:
        p = self.p
        f = ttk.Frame(parent, style="Card.TFrame", padding=p(10))
        self.cnt_in = self._header(f, "Inactive Mods")
        row = ttk.Frame(f, style="Panel.TFrame")
        row.pack(fill="x", pady=p(4))
        self.filter_var = tk.StringVar(value="All")
        cb = ttk.Combobox(row, textvariable=self.filter_var, values=FILTERS, state="readonly", width=10)
        cb.pack(side="right", padx=(p(4), 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_inactive())
        self.search_in = SearchEntry(row, "Search inactive…", self._refresh_inactive)
        self.search_in.pack(side="left", fill="x", expand=True)
        self.tree_in = self._make_tree(f, [("name", "Name", 220, True), ("src", "Source", 76, False),
                                           ("ver", "Versions", 76, False)], sortable=True)
        btns = ttk.Frame(f, style="Panel.TFrame")      # packed before the tree: a short pane shrinks the tree, not the buttons
        btns.pack(side="bottom", fill="x", pady=(p(6), 0), before=self.tree_in.master)
        ttk.Button(btns, text="Activate ▶", style="Accent.TButton", command=self.activate_selected).pack(side="right")
        ttk.Button(btns, text="Delete…", width=0, command=lambda: self.delete_selected(self.tree_in)).pack(side="left")
        self._bind_tree(self.tree_in)
        self.tree_in.bind("<Return>", lambda e: (self.activate_selected(), "break")[1])
        return f

    def _build_active(self, parent) -> ttk.Frame:
        p = self.p
        f = ttk.Frame(parent, style="Card.TFrame", padding=p(10))
        self.cnt_act = self._header(f, "Active Load Order")
        self.search_act = SearchEntry(f, "Search active…", self._refresh_active)
        self.search_act.pack(fill="x", pady=p(4))
        self.tree_act = self._make_tree(f, [("idx", "#", 40, False), ("name", "Name", 240, True),
                                            ("src", "Source", 76, False)])
        btns = ttk.Frame(f, style="Panel.TFrame")
        btns.pack(side="bottom", fill="x", pady=(p(6), 0), before=self.tree_act.master)
        ttk.Button(btns, text="◀ Deactivate", command=self.deactivate_selected).pack(side="left")
        ttk.Button(btns, text="▲", width=3, command=lambda: self.move_selected("up")).pack(side="left", padx=(p(4), 0))
        ttk.Button(btns, text="▼", width=3, command=lambda: self.move_selected("down")).pack(side="left", padx=(p(4), 0))
        ttk.Button(btns, text="Clear", width=0, command=self.clear_active).pack(side="right")   # natural width
        t = self.tree_act
        self._bind_tree(t)
        for seq, fn in (("<Delete>", self.deactivate_selected), ("<BackSpace>", self.deactivate_selected),
                        ("<Return>", self.deactivate_selected), ("<Control-Up>", lambda: self.move_selected("up")),
                        ("<Control-Down>", lambda: self.move_selected("down")),
                        ("<Control-Home>", lambda: self.move_selected("top")),
                        ("<Control-End>", lambda: self.move_selected("bottom"))):
            t.bind(seq, lambda e, fn=fn: (fn(), "break")[1])
        self._drop_line = tk.Frame(t, height=max(2, p(2)), bd=0, highlightthickness=0)
        self._drop_line._rmm_custom_colors = True
        return f

    def _build_details(self, parent) -> ttk.Frame:
        f = ttk.Frame(parent, style="Card.TFrame", padding=self.p(10))
        hdr = ttk.Frame(f, style="Panel.TFrame")
        hdr.pack(fill="x", pady=(0, self.p(4)))
        ttk.Label(hdr, text="Mod Details", style="PanelHeading.TLabel").pack(side="left")
        box = ttk.Frame(f, style="Panel.TFrame")
        box.pack(fill="both", expand=True)
        t = tk.Text(box, wrap="word", padx=self.p(14), pady=self.p(10), cursor="arrow", width=30, height=10,
                    font=self.app.fonts["body"], spacing1=self.p(1), spacing3=self.p(1), takefocus=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set, state="disabled")
        t.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor="arrow"))
        self.details = t
        return f

    def _build_issues(self, parent) -> ttk.Frame:
        p = self.p
        f = ttk.Frame(parent, style="Card.TFrame", padding=p(10))
        hdr = ttk.Frame(f, style="Panel.TFrame")
        hdr.pack(fill="x", pady=(0, p(4)))
        ttk.Label(hdr, text="Issues & Load Order Checks", style="PanelHeading.TLabel").pack(side="left")
        self.lbl_counts = ttk.Label(hdr, style="PanelMuted.TLabel")
        self.lbl_counts.pack(side="left", padx=(p(10), 0))
        self.lbl_checking = ttk.Label(hdr, style="Small.TLabel", background=self.app.colors.get("panel", "#171a24"))
        self.lbl_checking.pack(side="left", padx=(p(10), 0))
        ttk.Button(hdr, text="Fix All Safe", style="Accent.TButton", command=self.fix_all_safe).pack(side="right")
        ttk.Button(hdr, text="Fix Selected", command=self.fix_selected).pack(side="right", padx=(0, p(6)))
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        t = ttk.Treeview(box, columns=("level", "mod", "msg"), show="headings", height=4, selectmode="extended")
        for cid, title, w, st in (("level", "Level", 90, False), ("mod", "Mod", 200, False), ("msg", "Message", 600, True)):
            t.heading(cid, text=title, anchor="w")
            t.column(cid, width=p(w), minwidth=p(40), stretch=st)
        sb = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        t.bind("<<TreeviewSelect>>", self._on_issue_select)
        t.bind("<Double-1>", lambda e: self.fix_selected() if t.identify_region(e.x, e.y) == "cell" else None)
        t.bind("<Return>", lambda e: self.fix_selected())
        self.tree_iss = t
        return f

    def _bind_tree(self, t: ttk.Treeview) -> None:
        t.bind("<ButtonPress-1>", lambda e: self._press(e, t))
        t.bind("<B1-Motion>", lambda e: self._motion(e, t))
        t.bind("<ButtonRelease-1>", lambda e: self._release(e, t))
        t.bind("<Double-1>", lambda e: self._double(e, t))
        t.bind("<Button-3>", lambda e: self._popup(e, t))
        t.bind("<<TreeviewSelect>>", lambda e: self._on_select(t))
        t.bind("<Control-a>", lambda e: (t.selection_set(t.get_children()), "break")[1])
        t.bind("<Control-A>", lambda e: (t.selection_set(t.get_children()), "break")[1])

    def _apply_colors(self, *_a) -> None:
        c, f = self.app.colors, self.app.fonts
        for t in (self.tree_in, self.tree_act, self.tree_iss):
            t.tag_configure("missing", foreground=c["err"])
            t.tag_configure("error", foreground=c["err"])
            t.tag_configure("warning", foreground=c["warn"])
            t.tag_configure("info", foreground=c["info"])
            t.tag_configure("official", foreground=c["muted"])
            t.tag_configure("dim", foreground=c["muted"])
            t.tag_configure("ok", foreground=c["ok"])
        self._drop_line.configure(bg=c["accent"])
        d = self.details
        d.configure(background=c["field"], foreground=c["text"], highlightbackground=c["border"],
                    highlightcolor=c["border"], selectbackground=c["select"], inactiveselectbackground=c["select"])
        d.tag_configure("h1", font=f["title"], spacing1=self.p(4), spacing3=self.p(2))
        d.tag_configure("sect", font=f["bold"], foreground=c["text"], spacing1=self.p(10), spacing3=self.p(3))
        d.tag_configure("muted", foreground=c["muted"])
        d.tag_configure("key", foreground=c["muted"])
        d.tag_configure("ok", foreground=c["ok"])
        d.tag_configure("warn", foreground=c["warn"])
        d.tag_configure("err", foreground=c["err"])
        d.tag_configure("link", foreground=c["link"], underline=True)
        d.tag_configure("mono", font=f["mono"])
        d.tag_configure("small", font=f["small"], foreground=c["muted"])
        d.tag_configure("indent", lmargin1=self.p(12), lmargin2=self.p(28))
        d.tag_configure("desc", spacing2=self.p(2))
        d.tag_raise("link")
        if self._detail != (None, None):
            self._render_details()

    # ================================================================ data refresh
    def _on_scanned(self, *_a) -> None:
        self._preview_cache.clear()
        self._reindex()
        self.refresh()
        self.schedule_check()

    def _on_active_changed(self, *_a) -> None:
        self.refresh()
        self.schedule_check()

    def _reindex(self) -> None:
        by_pid: dict[str, list[str]] = {}
        by_wid, hay = {}, {}
        for uid, m in list(self.mgr.installed.items()):
            by_pid.setdefault(m.package_id or uid, []).append(uid)
            if m.workshop_id:
                by_wid.setdefault(m.workshop_id, uid)
            hay[uid] = f"{m.name}\n{m.package_id}\n{uid}\n{m.workshop_id}\n{' '.join(m.authors)}".lower()
        self._by_pid, self._by_wid, self._hay = by_pid, by_wid, hay

    def refresh(self) -> None:
        self._active_set = set(self.mgr.active)
        self._refresh_inactive()
        self._refresh_active()
        self._update_counts()
        self._update_dirty()
        if self._detail != (None, None):
            self._show_details_later(*self._detail, delay=120)

    def _iid_for(self, uid: str) -> str:
        iid = self._iid.get(uid)
        if iid is None:
            iid = self._iid[uid] = f"m{len(self._iid)}"
            self._uid[iid] = uid
        return iid

    def _outdated(self, m: Mod) -> bool:
        vs = self.mgr.version_short
        return bool(vs) and not m.is_official and vs not in m.supported_versions

    def _match(self, uid: str, q: str) -> bool:
        hay = self._hay.get(uid, uid)
        return all(tok in hay for tok in q.split())

    def _refresh_inactive(self) -> None:
        flt, q = self.filter_var.get(), self.search_in.query()
        act = self._active_set
        want = {"Local": ("local", "extra"), "Workshop": ("workshop",), "SteamCMD": ("steamcmd",),
                "Official": ("core", "dlc")}.get(flt)
        mods, total = [], 0
        for uid, m in list(self.mgr.installed.items()):
            if uid in act:
                continue
            total += 1
            if want and m.source not in want or flt == "Outdated" and not self._outdated(m):
                continue
            if q and not self._match(uid, q):
                continue
            mods.append(m)
        col, rev = self._sort
        key = {"src": lambda m: (SOURCE_LABEL.get(m.source, m.source), m.name.casefold()),
               "ver": lambda m: (m.supported_versions[-1:] or [""], m.name.casefold())}.get(col, lambda m: m.name.casefold())
        mods.sort(key=key, reverse=rev)
        rows = []
        for m in mods:
            tag = "official" if m.is_official else "warning" if self._outdated(m) else ""
            rows.append((self._iid_for(m.uid), (m.name, SOURCE_LABEL.get(m.source, m.source),
                                                " ".join(m.supported_versions[-3:])), (tag,) if tag else ()))
        self._sync(self.tree_in, rows)
        self.cnt_in.configure(text=f"{len(rows)} of {total}" if len(rows) != total else str(total))

    def _refresh_active(self) -> None:
        q = self.search_act.query()
        inst = self.mgr.installed
        rows = []
        for i, uid in enumerate(self.mgr.active):
            if q and not self._match(uid, q):
                continue
            m = inst.get(uid)
            if m is None:
                vals, tag = (i + 1, uid, "Missing"), "missing"
            else:
                lvl = self._issue_level.get(uid)
                tag = (lvl if lvl in ("error", "warning") else
                       "warning" if self._outdated(m) else "official" if m.is_official else "")
                vals = (i + 1, m.name, SOURCE_LABEL.get(m.source, m.source))
            rows.append((self._iid_for(uid), vals, (tag,) if tag else ()))
        self._sync(self.tree_act, rows)
        n = len(self.mgr.active)
        self.cnt_act.configure(text=f"{len(rows)} of {n}" if len(rows) != n else str(n))

    def _sort_by(self, col: str) -> None:
        prev, rev = self._sort
        self._sort = (col, not rev if prev == col else False)
        for cid, title in (("name", "Name"), ("src", "Source"), ("ver", "Versions")):
            arrow = (" ▼" if self._sort[1] else " ▲") if cid == col else ""
            self.tree_in.heading(cid, text=title + arrow)
        self._refresh_inactive()

    def _sync(self, tree: ttk.Treeview, rows: list[tuple[str, tuple, tuple]]) -> None:
        """Make `tree` show `rows` [(iid, values, tags)] with minimal Tk calls (keeps selection & scroll).
        Big fills (>2000 rows) are chunked with after() so the UI stays responsive."""
        key = str(tree)
        job = self._sync_jobs.pop(key, None)
        if job:
            self.after_cancel(job)
        gen = self._sync_steps(tree, rows)

        def step():
            try:
                next(gen)
                self._sync_jobs[key] = self.after(1, step)
            except StopIteration:
                self._sync_jobs.pop(key, None)
        step()

    def _sync_steps(self, tree: ttk.Treeview, rows):
        cache = self._rows[str(tree)]
        want = {r[0] for r in rows}
        cur = tree.get_children("")
        gone = [i for i in cur if i not in want]
        if gone:
            tree.delete(*gone)
            for i in gone:
                cache.pop(i, None)
        remaining = [i for i in cur if i in want]
        pos, placed, ops = 0, set(), 0
        chunked = len(rows) > 2000
        for idx, (iid, values, tags) in enumerate(rows):
            old = cache.get(iid)
            if old is None:
                tree.insert("", idx, iid=iid, values=values, tags=tags)
                cache[iid] = (values, tags)
                ops += 1
            else:
                while pos < len(remaining) and remaining[pos] in placed:
                    pos += 1
                if pos < len(remaining) and remaining[pos] == iid:
                    pos += 1
                else:
                    tree.move(iid, "", idx)
                    placed.add(iid)
                    ops += 1
                if old != (values, tags):
                    tree.item(iid, values=values, tags=tags)
                    cache[iid] = (values, tags)
                    ops += 1
            if chunked and ops >= 500:
                ops = 0
                yield

    def _update_counts(self) -> None:
        errs = sum(1 for i in self._issues if i.level == "error")
        warns = sum(1 for i in self._issues if i.level == "warning")
        self.lbl_counts.configure(text=f"{len(self.mgr.active)} active / {len(self.mgr.installed)} installed · "
                                       f"{errs} error{'s' * (errs != 1)} · {warns} warning{'s' * (warns != 1)}")

    def _update_dirty(self) -> None:
        self.lbl_dirty.configure(text="● Unsaved changes" if getattr(self.mgr, "dirty", False) else "")

    # ================================================================ checks / issues
    def schedule_check(self, delay: int = 400) -> None:
        if self._check_after:
            self.after_cancel(self._check_after)
        self._check_after = self.after(delay, self.run_check)

    def run_check(self, announce: bool = False) -> None:
        self._check_after = None
        if announce:
            self._announce_check = True
        if self._check_running:
            self._check_pending = True
            return
        self._check_running = True
        self.lbl_checking.configure(text="checking…")
        self.app.run_bg(self.mgr.check, on_done=self._check_done, on_error=self._check_failed)

    def _check_done(self, issues) -> None:
        self._check_running = False
        self.lbl_checking.configure(text="")
        self._set_issues(list(issues or []))
        if self._check_pending:
            self._check_pending = False
            self.run_check()
        elif self._announce_check:
            self._announce_check = False
            e = sum(1 for i in self._issues if i.level == "error")
            w = sum(1 for i in self._issues if i.level == "warning")
            self.app.toast("No problems found" if not e and not w else f"{e} error(s), {w} warning(s)",
                           "ok" if not e and not w else "error" if e else "warn")

    def _check_failed(self, exc) -> None:
        self._check_running = False
        self.lbl_checking.configure(text="")
        self.app.toast(f"Check failed: {exc}", "error")
        if self._check_pending:
            self._check_pending = False
            self.run_check()
        else:
            self._announce_check = False

    def _set_issues(self, issues: list[Issue]) -> None:
        self._issues = issues
        worst: dict[str, str] = {}
        for i in issues:
            if i.uid and LEVEL_RANK.get(i.level, 3) < LEVEL_RANK.get(worst.get(i.uid, ""), 3):
                worst[i.uid] = i.level
        self._issue_level = worst
        t = self.tree_iss
        t.delete(*t.get_children(""))
        inst = self.mgr.installed
        for n, i in enumerate(issues):
            m = inst.get(i.uid)
            t.insert("", "end", iid=str(n), tags=(i.level,),
                     values=(f"{LEVEL_ICON.get(i.level, '•')} {i.level.title()}", m.name if m else i.uid or "—",
                             i.message))
        if not issues:
            t.insert("", "end", iid="none", tags=("ok",), values=("✔ OK", "", "No problems found in the active list."))
        self._refresh_active()
        self._update_counts()
        if self._detail[0]:
            self._show_details_later(*self._detail, delay=0)

    def _on_issue_select(self, _e=None) -> None:
        sel = [i for i in self.tree_iss.selection() if i.isdigit()]
        if len(sel) == 1:
            uid = self._issues[int(sel[0])].uid
            iid = self._iid.get(uid)
            if uid and iid and self.tree_act.exists(iid):
                self.tree_act.selection_set(iid)
                self.tree_act.see(iid)
            if uid:
                self._show_details_later(uid, None, delay=0)

    def fix_selected(self) -> None:
        issues = [self._issues[int(i)] for i in self.tree_iss.selection() if i.isdigit() and int(i) < len(self._issues)]
        fixable = [i for i in issues if i.fix]
        if not fixable:
            self.app.toast("Select an issue that has an automatic fix" if not issues else
                           "The selected issue(s) have no automatic fix", "warn")
            return
        self._apply_fixes(fixable, safe_only=False)

    def fix_all_safe(self) -> None:
        fixable = [i for i in self._issues if i.fix and i.fix.get("action") in SAFE_ACTIONS]
        if not fixable:
            self.app.toast("Nothing that can be fixed safely", "info")
            return
        self._apply_fixes(fixable, safe_only=True)

    def _apply_fixes(self, issues: list[Issue], safe_only: bool) -> None:
        """Apply fixes in the background (sort last, once per pass). "Fix all safe" re-checks and repeats
        (adding a dependency can make a sort necessary) until nothing new is fixable."""
        def work():
            refs: dict[str, ModRef] = {}
            done_keys: set = set()
            errors: list[str] = []
            batch = issues
            for _ in range(4 if safe_only else 1):
                todo = [i for i in batch if (i.kind, i.uid, i.fix.get("action")) not in done_keys]
                if not todo:
                    break
                sorted_once = False
                for iss in sorted(todo, key=lambda i: i.fix.get("action") == "sort"):   # sort last
                    done_keys.add((iss.kind, iss.uid, iss.fix.get("action")))
                    if iss.fix.get("action") == "sort":
                        if sorted_once:
                            continue
                        sorted_once = True
                    try:                                  # a failed fix (e.g. folder exists) must not stop the rest
                        for r in self.mgr.apply_fix(iss) or []:
                            refs.setdefault(r.key(), r)
                    except Exception as e:
                        errors.append(f"{iss.message}: {e}")
                if safe_only:
                    batch = [i for i in self.mgr.check() if i.fix and i.fix.get("action") in SAFE_ACTIONS]
            return len(done_keys) - len(errors), list(refs.values()), errors

        before = self.mgr.installed                       # install_local / replace rescan (scan() swaps the dict)

        def refresh():
            if self.mgr.installed is not before:
                self.app.emit("scanned")
            self.app.emit("active_changed")

        def done(res):
            n, refs, errors = res
            refresh()
            if errors:
                self.app.error("Some fixes were not applied", "\n".join(errors[:15]))
            if n:
                self.app.toast(f"Applied {n} fix(es)", "ok")
            if refs:
                self._offer_download(refs, ask=safe_only)

        self.app.run_bg(work, on_done=done, busy="Applying fixes…",
                        on_error=lambda e: (refresh(), self.app.error("Applying fixes failed", str(e) or type(e).__name__)))

    def _offer_download(self, refs: list[ModRef], ask: bool = True) -> None:
        names = "\n".join(f"• {r.name or r.package_id or r.workshop_id}" for r in refs[:15])
        more = f"\n…and {len(refs) - 15} more" if len(refs) > 15 else ""
        if not ask or self.app.ask_yes_no("Download mods", f"{len(refs)} mod(s) are not installed:\n\n{names}{more}"
                                                           "\n\nQueue them for download now?"):
            self.app.queue_download(refs, activate=True)

    # ================================================================ toolbar actions
    def _sel_uids(self, tree: ttk.Treeview) -> list[str]:
        uids = [self._uid[i] for i in tree.selection() if i in self._uid]
        if tree is self.tree_act:
            order = {u: n for n, u in enumerate(self.mgr.active)}
            uids.sort(key=lambda u: order.get(u, 1 << 30))
        return uids

    def _next_after(self, tree: ttk.Treeview, iids) -> str | None:
        kids, sel = tree.get_children(""), set(iids)
        idx = [n for n, k in enumerate(kids) if k in sel]
        if not idx:
            return None
        for k in kids[idx[-1] + 1:]:
            if k not in sel:
                return k
        for k in reversed(kids[:idx[0]]):
            if k not in sel:
                return k
        return None

    def _select(self, tree: ttk.Treeview, uids, see: bool = True) -> None:
        iids = [self._iid[u] for u in uids if u in self._iid and tree.exists(self._iid[u])]
        tree.selection_set(iids)
        if iids:
            tree.focus(iids[0])
            if see:
                tree.see(iids[0])

    def activate_selected(self, index: int | None = None) -> None:
        uids = self._sel_uids(self.tree_in)
        if not uids:
            return
        nxt = self._next_after(self.tree_in, self.tree_in.selection())
        self.mgr.activate(uids, index)
        self.app.emit("active_changed")
        self._select(self.tree_act, uids)
        if nxt and self.tree_in.exists(nxt):
            self.tree_in.selection_set(nxt)
            self.tree_in.focus(nxt)
            self.tree_in.see(nxt)

    def deactivate_selected(self, uids: list[str] | None = None) -> None:
        uids = uids or self._sel_uids(self.tree_act)
        if not uids:
            return
        nxt = self._next_after(self.tree_act, [self._iid[u] for u in uids if u in self._iid])
        self.mgr.deactivate(uids)
        self.app.emit("active_changed")
        self._select(self.tree_in, uids, see=False)
        if nxt and self.tree_act.exists(nxt):
            self.tree_act.selection_set(nxt)
            self.tree_act.focus(nxt)
            self.tree_act.see(nxt)

    def move_selected(self, how: str) -> None:
        self._move(self._sel_uids(self.tree_act), how)

    def _move(self, uids: list[str], how: str, target: str | None = None, after: bool = False) -> None:
        """Reorder via set_active() with an explicitly computed list (unambiguous, one undo step)."""
        act = list(self.mgr.active)
        sel = set(uids)
        block = [u for u in act if u in sel]
        if not block:
            return
        rest = [u for u in act if u not in sel]
        new = list(act)
        if how == "up":
            for i in range(1, len(new)):
                if new[i] in sel and new[i - 1] not in sel:
                    new[i - 1], new[i] = new[i], new[i - 1]
        elif how == "down":
            for i in range(len(new) - 2, -1, -1):
                if new[i] in sel and new[i + 1] not in sel:
                    new[i], new[i + 1] = new[i + 1], new[i]
        elif how == "top":
            new = block + rest
        elif how == "bottom":
            new = rest + block
        elif how == "to":
            if target in sel:
                return
            pos = rest.index(target) + (1 if after else 0) if target in rest else len(rest)
            new = rest[:pos] + block + rest[pos:]
        if new == act:
            return
        self.mgr.set_active(new)
        self.app.emit("active_changed")
        self._select(self.tree_act, block, see=False)
        iid = self._iid.get(block[0])
        if iid and self.tree_act.exists(iid):
            self.tree_act.see(iid)

    def clear_active(self) -> None:
        keep = [u for u in self.mgr.active if (m := self.mgr.installed.get(u)) and m.is_official]
        if not keep and CORE_ID in self.mgr.installed:
            keep = [CORE_ID]
        if len(self.mgr.active) <= len(keep):
            return
        if self.app.ask_yes_no("Clear active list", "Deactivate every mod except Core and the DLCs?\n\n"
                                                    "(Undo with Ctrl+Z, nothing is saved until you press Save.)"):
            self.mgr.set_active(keep)
            self.app.emit("active_changed")

    def delete_selected(self, tree) -> None:
        mods = [m for u in self._sel_uids(tree) if (m := self.mgr.installed.get(u)) and not m.is_official]
        if not mods:
            self.app.toast("Select mods to delete (Core and DLCs can't be deleted)")
            return
        self.delete_mods(mods)

    def delete_mods(self, mods, on_deleted=None) -> None:
        """Confirm, then delete these (non-official) mods from disk; also used by Tools → Unused mods."""
        subs = sum(m.source == "workshop" for m in mods)
        names = "\n".join(f"• {m.name}" for m in mods[:8]) + (f"\n…and {len(mods) - 8} more" if len(mods) > 8 else "")
        if not self.app.ask_yes_no("Delete mods", f"Permanently delete {len(mods)} mod(s) from disk?\n\n{names}\n\n"
                                   + (f"{subs} Steam workshop mod(s) will also be unsubscribed.\n\n" if subs else "")
                                   + "This can't be undone; you'd have to download them again."):
            return

        def done(problems):
            self.app.emit("scanned")
            self.app.emit("active_changed")
            if on_deleted:
                on_deleted()
            steam_msgs = [p for p in problems or [] if p.startswith(("Could not unsubscribe", "Unsubscribe failed"))]
            failed = [p for p in problems or [] if p not in steam_msgs]
            if failed:
                self.app.error("Some mods were not deleted", "\n".join((failed + steam_msgs)[:15]))
            elif steam_msgs:                            # all folders are gone, only the Steam unsubscribe failed
                self.app.info("Mods deleted", f"Deleted {len(mods)} mod(s), but Steam may download them again:\n\n"
                              + "\n".join(steam_msgs[:15]) + "\n\nPress Save to update the game's mod list.")
            else:
                self.app.toast(f"Deleted {len(mods)} mod(s). Press Save to update the game's mod list.", "ok")

        self.app.run_bg(self.mgr.uninstall, [m.uid for m in mods], progress=True, busy="Deleting mods…",
                        on_done=done, on_error=lambda e: (self.app.emit("scanned"),
                                                          self.app.error("Delete failed", str(e))))

    def save(self) -> None:
        errs = [i for i in self._issues if i.level == "error"]
        if errs:
            lines = "\n".join(f"• {i.message}" for i in errs[:6]) + (f"\n…and {len(errs) - 6} more" if len(errs) > 6 else "")
            if not self.app.ask_yes_no("Save with errors?", f"The active list has {len(errs)} error(s):\n\n{lines}\n\n"
                                       "RimWorld may fail to load these mods. Save anyway?"):
                return
        self.app.save_active(on_saved=self._update_dirty)

    def revert(self) -> None:
        if getattr(self.mgr, "dirty", False) and not self.app.ask_yes_no(
                "Revert", "Discard your changes and reload the active list from RimWorld's ModsConfig.xml?"):
            return
        self.app.run_bg(self.mgr.load_active_from_game, busy="Reloading active list…",
                        on_done=lambda _: (self.app.emit("active_changed"), self.app.toast("Reloaded from RimWorld", "ok")))

    def auto_sort(self) -> None:
        def done(warnings):
            self.app.emit("active_changed")
            warnings = list(warnings or [])
            if warnings:
                self.app.info("Load order sorted with warnings", "\n".join(f"• {w}" for w in warnings[:20]) +
                              (f"\n…and {len(warnings) - 20} more" if len(warnings) > 20 else ""))
            else:
                self.app.toast("Load order sorted", "ok")
        self.app.run_bg(self.mgr.auto_sort, busy="Sorting load order…", on_done=done)

    def fix_dependencies(self) -> None:
        def done(res):
            added, to_dl, unres = res
            self.app.emit("active_changed")
            if added:
                self.app.toast(f"Activated {len(added)} installed dependenc{'y' if len(added) == 1 else 'ies'}", "ok")
            if unres:
                self.app.info("Unresolved dependencies", "\n".join(f"• {u}" for u in unres[:25]) +
                              (f"\n…and {len(unres) - 25} more" if len(unres) > 25 else ""))
            if to_dl:
                self._offer_download(list(to_dl))
            if not added and not to_dl and not unres:
                self.app.toast("All dependencies are satisfied", "ok")
        self.app.run_bg(self.mgr.resolve_dependencies, busy="Resolving dependencies…", on_done=done)

    def launch(self) -> None:
        def go():
            if getattr(self, "_launching", False):     # double-click: the first launch is still starting
                return
            self._launching = True

            def done(res):
                self._launching = False
                self.app.toast("RimWorld is already running" if res == "running" else "RimWorld is starting…",
                               "warn" if res == "running" else "ok")

            def failed(e):
                self._launching = False
                self.app.error("Launch failed", str(e) or type(e).__name__)
            # the running check lives in the worker: tasklist blocks
            self.app.run_bg(lambda: "running" if self.mgr.is_game_running() else self.mgr.launch_game(),
                            busy="Launching RimWorld…", on_done=done, on_error=failed)
        inst = self.mgr.installed
        missing = [u for u in self.mgr.active if u != CORE_ID and u not in DLCS
                   and ((m := inst.get(u)) is None or m.source not in LOADABLE_SOURCES)]
        if missing and not self.app.ask_yes_no(
                "Launch RimWorld", f"{len(missing)} active mod(s) are not installed where RimWorld loads them (missing, "
                "only in the SteamCMD cache, or in an extra folder). RimWorld removes them from its mod list at "
                "startup (and keeps that if the list is saved in game), so download or install them first.\n\n"
                "Launch anyway?"):
            return
        if getattr(self.mgr, "dirty", False):
            ans = self.app.ask_yes_no_cancel("Launch RimWorld", "Save the active mod list before launching?")
            if ans is None:
                return
            if ans:
                self.app.save_active(on_saved=go)
                return
        go()

    def focus_search(self) -> None:
        try:
            w = self.focus_get()
        except (KeyError, tk.TclError):         # focus inside a combobox popdown
            w = None
        target = self.search_act if w in (self.tree_act, self.search_act) else self.search_in
        target.focus_set()
        target.select_range(0, "end")

    # ================================================================ mouse: drag & drop, double-click, menu
    def _press(self, e, tree):
        self._drag = None
        if tree.identify_region(e.x, e.y) not in ("cell", "tree"):
            return None
        iid = tree.identify_row(e.y)
        if not iid:
            return None
        self._drag = {"tree": tree, "x": e.x, "y": e.y, "iid": iid, "active": False, "deferred": False}
        if iid in tree.selection() and len(tree.selection()) > 1 and not e.state & (SHIFT | CTRL):
            self._drag["deferred"] = True       # keep the multi-selection so it can be dragged as a block
            tree.focus(iid)
            tree.focus_set()
            return "break"
        return None

    def _motion(self, e, tree):
        d = self._drag
        if not d or d["tree"] is not tree:
            return None
        if not d["active"]:
            if abs(e.y - d["y"]) < self.p(6) and abs(e.x - d["x"]) < self.p(6):
                return "break"
            d["active"] = True
            if d["iid"] not in tree.selection():
                tree.selection_set(d["iid"])
            d["uids"] = self._sel_uids(tree)
            tree.configure(cursor="fleur")
        d["y_root"] = e.y_root
        self._update_drop(e.x_root, e.y_root)
        return "break"

    def _release(self, e, tree):
        d, self._drag = self._drag, None
        self._drop_line.place_forget()
        self._stop_autoscroll()
        tree.configure(cursor="")
        if not d or d["tree"] is not tree:
            return None
        if not d["active"]:
            if d["deferred"]:
                tree.selection_set(d["iid"])
            return None
        over = self._tree_at(e.x_root, e.y_root)
        uids = d.get("uids") or []
        if over is self.tree_act:
            target, after, _y = self._drop_target(e.y_root - self.tree_act.winfo_rooty())
            if tree is self.tree_act:
                self._move(uids, "to", target, after)
            else:
                act = self.mgr.active
                idx = act.index(target) + (1 if after else 0) if target in act else None
                self.mgr.activate(uids, idx)
                self.app.emit("active_changed")
                self._select(self.tree_act, uids)
        elif over is self.tree_in and tree is self.tree_act:
            self.deactivate_selected(uids)
        return "break"

    def _tree_at(self, x_root: int, y_root: int):
        w = self.winfo_containing(x_root, y_root)
        while w is not None and w not in (self.tree_act, self.tree_in):
            w = w.master
        return w

    def _update_drop(self, x_root: int, y_root: int) -> None:
        t = self.tree_act
        if self._tree_at(x_root, y_root) is not t:
            self._drop_line.place_forget()
            self._stop_autoscroll()
            return
        y = y_root - t.winfo_rooty()
        _target, _after, line_y = self._drop_target(y)
        self._drop_line.place(x=0, y=max(0, line_y - 1), relwidth=1.0)
        self._drop_line.lift()
        edge = self.p(22)
        self._autoscroll(-1 if y < edge + self.p(24) else 1 if y > t.winfo_height() - edge else 0)

    def _drop_target(self, y: int) -> tuple[str | None, bool, int]:
        """(uid to drop next to, after?, indicator y) for pointer y inside the active tree."""
        t = self.tree_act
        iid = t.identify_row(y)
        if not iid:                               # heading area / gaps: probe a little further down
            for dy in range(2, self.p(60), 3):
                iid = t.identify_row(y + dy)
                if iid:
                    bx = t.bbox(iid)
                    return self._uid.get(iid), False, bx[1] if bx else 0
            kids = t.get_children("")
            if not kids:
                return None, False, self.p(28)
            bx = t.bbox(kids[-1])
            return self._uid.get(kids[-1]), True, (bx[1] + bx[3]) if bx else t.winfo_height() - 2
        bx = t.bbox(iid)
        if not bx:
            return self._uid.get(iid), False, y
        after = y > bx[1] + bx[3] / 2
        return self._uid.get(iid), after, bx[1] + bx[3] if after else bx[1]

    def _autoscroll(self, direction: int) -> None:
        if direction == 0:
            self._stop_autoscroll()
            return
        if self._scroll_job:
            return

        def tick():
            if not self._drag or not self._drag.get("active"):
                self._scroll_job = None
                return
            self.tree_act.yview_scroll(direction, "units")
            y_root = self._drag.get("y_root", 0)
            self._scroll_job = None
            self._update_drop(self.tree_act.winfo_rootx() + 5, y_root)   # re-arms tick() while near an edge
        self._scroll_job = self.after(60, tick)

    def _stop_autoscroll(self) -> None:
        if self._scroll_job:
            self.after_cancel(self._scroll_job)
            self._scroll_job = None

    def _double(self, e, tree):
        if tree.identify_region(e.x, e.y) != "cell":
            return None
        iid = tree.identify_row(e.y)
        if not iid:
            return None
        tree.selection_set(iid)
        if tree is self.tree_in:
            self.activate_selected()
        else:
            self.deactivate_selected()
        return "break"

    def _popup(self, e, tree):
        iid = tree.identify_row(e.y)
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
            tree.focus(iid)
        uids = self._sel_uids(tree)
        if not uids:
            return
        inst = self.mgr.installed
        mods = [inst[u] for u in uids if u in inst]
        wids = [m.workshop_id for m in mods if m.workshop_id]
        menu = self._menu
        menu.delete(0, "end")
        if tree is self.tree_act:
            menu.add_command(label="Deactivate", accelerator="Del", command=self.deactivate_selected)
            menu.add_command(label="Move to top", accelerator="Ctrl+Home", command=lambda: self.move_selected("top"))
            menu.add_command(label="Move to bottom", accelerator="Ctrl+End", command=lambda: self.move_selected("bottom"))
        else:
            menu.add_command(label="Activate", accelerator="Enter", command=self.activate_selected)
            menu.add_command(label="Activate at top (after DLCs)", command=self._activate_top)
        menu.add_separator()
        st = lambda ok: "normal" if ok else "disabled"   # noqa: E731
        menu.add_command(label="Open folder", state=st(mods), command=lambda: self._open_path(mods[0].path))
        menu.add_command(label="Workshop page", state=st(wids), command=lambda: self._open_web(wids))
        menu.add_command(label="Open in Steam", state=st(wids), command=lambda: self._open_steam(wids))
        menu.add_command(label="Copy package id", command=lambda: self._copy(
            "\n".join((inst[u].package_id if u in inst else u) for u in uids)))
        menu.add_command(label="Copy workshop link", state=st(wids), command=lambda: self._copy(
            "\n".join(WORKSHOP_URL.format(w) for w in wids)))
        menu.add_command(label="Show in DB", command=lambda: self.show_in_db(uids[0]))
        menu.add_separator()
        menu.add_command(label="Delete from disk / unsubscribe…", state=st(mods),
                         command=lambda: self.delete_selected(tree))
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    def _activate_top(self) -> None:
        act = self.mgr.active
        idx = 0
        for n, u in enumerate(act):
            m = self.mgr.installed.get(u)
            if u == CORE_ID or u in DLCS or (m and m.is_official) or u == "brrainz.harmony":
                idx = n + 1
        self.activate_selected(idx)

    # ================================================================ helpers for links / menu
    def _open_path(self, path: str) -> None:
        from rmm import config
        if "://" not in path and not os.path.exists(path):
            self.app.toast("Folder not found (moved or deleted?) — press F5 to rescan", "warn")
            return
        self.app.run_bg(config.open_path, path)

    def _open_web(self, wids: list[str]) -> None:
        if len(wids) > 10 and not self.app.ask_yes_no("Open pages", f"Open {len(wids)} browser tabs?"):
            return
        from rmm import steam
        self.app.run_bg(steam.open_in_browser, wids)

    def _open_steam(self, wids: list[str]) -> None:
        from rmm import steam
        self.app.run_bg(steam.open_in_steam, wids[:10], on_done=lambda ok: ok or self.app.toast(
            "Steam isn't registered for steam:// links, so the pages opened in your browser", "warn"))

    def _open_url(self, url: str) -> None:
        import webbrowser
        self.app.run_bg(webbrowser.open_new_tab, url)

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast("Copied to clipboard")

    def show_in_db(self, uid: str) -> None:
        def done(dbm):
            if dbm is None:
                self.app.info("Not in database", f"“{uid}” was not found in the mod database.\n\n"
                              "(Local mods and brand-new workshop items are often missing; update the database in Tools.)")
            else:
                self._show_details_later(None, dbm, delay=0)
        self.app.run_bg(self.mgr.db_for_mod, uid, on_done=done, busy="Looking up database…")

    # ================================================================ selection -> details
    def _on_select(self, tree: ttk.Treeview) -> None:
        if self._drag and self._drag.get("active"):
            return
        sel = tree.selection()
        if not sel:
            return
        focus = tree.focus()
        iid = focus if focus in sel else sel[0]
        uid = self._uid.get(iid)
        if uid:
            self._show_details_later(uid, None)

    def show_mod(self, uid: str | None = None, db_mod: DbMod | None = None) -> None:
        """Select a mod in the lists and show its details (used by other tabs through app.show_mod_details)."""
        if db_mod is not None and (not uid or uid not in self.mgr.installed and uid not in self._active_set):
            uid = self.mgr.match_ref(db_mod.to_ref())
        if uid:
            active = uid in self._active_set
            tree, search = (self.tree_act, self.search_act) if active else (self.tree_in, self.search_in)
            iid = self._iid.get(uid)
            if not iid or not tree.exists(iid):
                search.clear()
                if not active:
                    self.filter_var.set("All")
                self.refresh()
                iid = self._iid.get(uid)
            if iid and tree.exists(iid):
                tree.selection_set(iid)
                tree.focus(iid)
                tree.see(iid)
            self._show_details_later(uid, None, delay=0)
        elif db_mod is not None:
            self._show_details_later(None, db_mod, delay=0)

    def _show_details_later(self, uid, db, delay: int = 80) -> None:
        self._detail = (uid, db)
        if self._detail_after:
            self.after_cancel(self._detail_after)
        self._detail_after = self.after(delay, self._render_details)

    def _render_details(self) -> None:
        self._detail_after = None
        uid, db = self._detail
        t = self.details
        same = self._rendered == (uid, db and db.workshop_id)     # re-render of the same mod keeps the scroll position
        self._rendered = (uid, db and db.workshop_id)
        top = t.yview()[0] if same else 0.0
        t.configure(state="normal")
        t.delete("1.0", "end")
        for tag in self._links:
            t.tag_delete(tag)
        self._links = []
        try:
            m = self.mgr.installed.get(uid) if uid else None
            if m:
                self._render_mod(m)
            elif uid:
                self._render_missing(uid)
            elif db is not None:
                self._render_db(db)
            else:
                self._put("Select a mod to see its details.\n\n", "muted")
                self._put("Tips\n", "sect")
                for tip in ("Double-click or Enter toggles a mod; Delete deactivates.",
                            "Drag mods to reorder, or drag between the two lists.",
                            "Ctrl+↑/↓ moves the selection; Ctrl+Z / Ctrl+Y undo and redo.",
                            "Auto-sort fixes the load order; Fix dependencies activates what is missing.",
                            "Nothing is written to RimWorld until you press Save (Ctrl+S)."):
                    self._put(f"• {tip}\n", "muted", "indent")
        finally:
            t.configure(state="disabled")
        t.yview_moveto(top)

    # text helpers
    def _put(self, text: str, *tags) -> None:
        self.details.insert("end", text, tuple(t for t in tags if t))

    def _link(self, text: str, fn, *tags) -> None:
        tag = f"lnk{len(self._links)}"
        self._links.append(tag)
        self.details.insert("end", text, ("link", tag, *tags))
        self.details.tag_bind(tag, "<Button-1>", lambda e: fn())

    def _kv(self, key: str, value: str = "", *tags) -> None:
        self._put(f"{key}  ", "key")
        if value:
            self._put(value, *tags)
        self._put("\n")

    def _preview(self, m: Mod) -> tk.PhotoImage | None | str:
        """Subsampled Preview.png/GIF (cached per uid), a reason string when not shown, or None (none / still
        loading: the file is read and checked on a worker, then _preview_ready re-renders the details)."""
        if m.uid in self._preview_cache:
            self._preview_cache.move_to_end(m.uid)
            return self._preview_cache[m.uid]
        if m.uid not in self._preview_loading:
            self._preview_loading.add(m.uid)
            path = m.preview_path or os.path.join(m.path, "About", "Preview.png")
            self.app.run_bg(read_preview, path, on_done=lambda res, u=m.uid: self._preview_ready(u, res),
                            on_error=lambda e, u=m.uid: self._preview_ready(u, "preview could not be read"))
        return None

    def _preview_ready(self, uid: str, res) -> None:
        """Tk thread: Tk images can only be made here, so the decode is bounded by PREVIEW_MAX_PIXELS."""
        self._preview_loading.discard(uid)
        if isinstance(res, bytes):
            try:
                full = tk.PhotoImage(master=self, data=res)
                max_w = min(self.p(PREVIEW_MAX_W), max(self.p(120), self.details.winfo_width() - self.p(40)))
                k = max(1, math.ceil(full.width() / max_w))
                res = full.subsample(k) if k > 1 else full
            except tk.TclError:
                res = "preview could not be decoded"
        self._preview_cache[uid] = res
        while len(self._preview_cache) > PREVIEW_CACHE:
            self._preview_cache.popitem(last=False)
        if res is not None and self._detail[0] == uid and not self._detail_after:
            self._render_details()          # same mod: keeps the scroll position

    def _dep_state(self, pid: str) -> str:
        uids = self._by_pid.get(pid, [])
        if pid in self._active_set or any(u in self._active_set for u in uids):
            return "active"
        return "inactive" if uids else "missing"

    def _mod_name(self, pid: str) -> str:
        uids = self._by_pid.get(pid)
        m = self.mgr.installed.get(uids[0]) if uids else None
        return m.name if m else pid

    def _render_patch_info(self, m: Mod) -> None:
        """Patch-level details: the exact game build, which per-version folders the mod ships, file and Steam dates."""
        day = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))  # noqa: E731
        vs = self.mgr.version_short
        if self.mgr.game_version:
            self._kv("Game build", self.mgr.game_version)
        try:
            dirs = sorted((d for d in os.listdir(m.path) if re.fullmatch(r"v?\d+\.\d+", d, re.I)
                           and os.path.isdir(os.path.join(m.path, d))),
                          key=lambda d: [int(x) for x in d.lstrip("vV").split(".")])
        except OSError:
            dirs = []
        if dirs:
            self._kv("Version folders", ", ".join(dirs))
            if vs and vs not in (d.lstrip("vV") for d in dirs):
                self._put(f"   no {vs} folder — the game loads the shared files or the closest older one\n", "small")
        if m.mtime:
            self._kv("Files dated", day(m.mtime))
        if m.workshop_id.isdigit():
            try:
                d = self.mgr.db.cached_details([m.workshop_id], max_age_s=float("inf")).get(m.workshop_id) or {}
                t = int(d.get("time_updated") or 0)
            except Exception:  # noqa: BLE001 - the panel must render without a database
                t = 0
            if t:
                newer = bool(m.mtime) and t > m.mtime + 60
                self._kv("Steam update", day(t), "warn" if newer else "")
                if newer:
                    self._put("   newer than your files — Check for updates confirms it\n", "warn", "small")

    def _render_mod(self, m: Mod) -> None:
        img = self._preview(m)
        if isinstance(img, tk.PhotoImage):
            self.details.image_create("end", image=img)
            self._put("\n")
        elif isinstance(img, str):
            self._put(f"[ {img} ]\n", "small")
        self._put(m.name + "\n", "h1")
        if m.authors:
            self._put("by " + ", ".join(m.authors) + "\n", "muted")
        act = self.mgr.active                   # the live list: _active_set lags behind background jobs
        pos = act.index(m.uid) + 1 if m.uid in act else 0
        active = pos > 0
        self._put(f"● Active (#{pos})\n" if active else "○ Inactive\n", "ok" if active else "muted")
        self._put("\n")
        self._kv("Package id", m.package_id or "(none)", "mono")
        if m.uid != m.package_id:
            self._kv("Load id", m.uid, "mono")
        if m.workshop_id:
            self._put("Workshop id  ", "key")
            self._link(m.workshop_id, lambda: self._open_web([m.workshop_id]))
            self._put("   ")
            self._link("open in Steam", lambda: self._open_steam([m.workshop_id]), "small")
            self._put("\n")
        self._kv("Source", SOURCE_LABEL.get(m.source, m.source))
        vs = self.mgr.version_short
        if m.supported_versions or not m.is_official:
            self._kv("Versions", ", ".join(m.supported_versions) or "(none listed)",
                     "err" if self._outdated(m) else "")
            if self._outdated(m):
                self._put(f"   does not list RimWorld {vs} — may still work\n", "err", "small")
        if m.mod_version:
            self._kv("Mod version", m.mod_version)
        if not m.is_official:
            self._render_patch_info(m)
        if m.url:
            self._put("Website  ", "key")
            self._link(m.url[:60] + ("…" if len(m.url) > 60 else ""), lambda: self._open_url(m.url))
            self._put("\n")
        self._put("Path  ", "key")
        self._link(m.path, lambda: self._open_path(m.path), "small")
        self._put("\n")
        if m.parse_error:
            self._put(f"⚠ {m.parse_error}\n", "warn")
        self._render_issues_for(m.uid)
        if m.dependencies:
            self._put("Dependencies\n", "sect")
            for dep in m.dependencies:      # an alternativePackageId satisfies it too (the primary id wins ties)
                pid = min(dep.ids(), key=lambda p: ("active", "inactive", "missing").index(self._dep_state(p)))
                self._render_dep(pid, dep.display_name, dep.workshop_id)
        inst = self.mgr.installed
        req = [u for u in self.mgr.active if u in inst and u != m.uid
               and any(m.package_id in d.ids() for d in inst[u].dependencies)]
        if req:
            self._put("Required by (active)\n", "sect")
            self._put(", ".join(inst[u].name for u in req[:30]) + ("…" if len(req) > 30 else "") + "\n",
                    "indent")
        for title, ids in (("Load after", m.load_after), ("Load before", m.load_before),
                           ("Incompatible with", m.incompatible_with)):
            if ids:
                self._put(title + "\n", "sect")
                for n, pid in enumerate(ids[:40]):
                    state = self._dep_state(pid)
                    tag = ("err" if state == "active" else "muted") if title.startswith("Incompatible") else \
                          ("" if state == "active" else "muted")
                    self._put(("" if n == 0 else ", ") + self._mod_name(pid), tag, "indent")
                self._put((" …" if len(ids) > 40 else "") + "\n", "indent")
        if m.description:
            self._put("Description\n", "sect")
            desc = _TAG_RE.sub("", m.description).replace("\\n", "\n").strip()
            self._put(desc[:20000] + ("…" if len(desc) > 20000 else "") + "\n", "desc")

    def _render_dep(self, pid: str, display: str, wid: str) -> None:
        state = self._dep_state(pid)
        name = self._mod_name(pid) if state != "missing" else (display or pid)
        sym, tag, note = {"active": ("✔", "ok", "active"), "inactive": ("⚠", "warn", "installed, not active"),
                          "missing": ("✖", "err", "not installed")}[state]
        if state == "missing" and pid.startswith("ludeon.rimworld"):
            note = "DLC not owned"
        self._put(f"{sym} ", tag, "indent")
        self._put(name, "indent")
        self._put(f"  ({pid}) — {note}", "small", "indent")
        if state == "missing" and wid:
            self._put("  ")
            self._link("download", lambda: self.app.queue_download([self.mgr.dep_ref(pid, display, wid)], activate=True),
                       "small")
        elif state == "inactive":
            self._put("  ")
            self._link("activate", lambda: self._activate_uids(self._by_pid.get(pid, [])[:1]), "small")
        self._put("\n")

    def _activate_uids(self, uids: list[str]) -> None:
        if uids:
            self.mgr.activate(uids, None)
            self.app.emit("active_changed")

    def _render_issues_for(self, uid: str) -> None:
        mine = [i for i in self._issues if i.uid == uid]
        if not mine:
            return
        self._put("Issues\n", "sect")
        for i in mine:
            tag = {"error": "err", "warning": "warn"}.get(i.level, "muted")
            self._put(f"{LEVEL_ICON.get(i.level, '•')} {i.message}", tag, "indent")
            if i.fix:
                self._put("  ")
                self._link("fix", lambda i=i: self._apply_fixes([i], safe_only=False), "small")
            self._put("\n")

    def _render_missing(self, uid: str) -> None:
        self._put(uid + "\n", "h1")
        self._put("✖ Not installed\n\n", "err")
        self._kv("Load id", uid, "mono")
        act = self.mgr.active
        pos = act.index(uid) + 1 if uid in act else 0
        if pos:
            self._kv("Position", f"#{pos} in the active list")
        self._render_issues_for(uid)
        refs = [r for i in self._issues if i.uid == uid and i.fix and i.fix.get("action") == "download"
                for r in i.fix.get("refs", [])]
        self._put("\n")
        if refs:
            self._link("⬇ Download this mod", lambda: self.app.queue_download(refs, activate=True))
            self._put("\n")
        self._put("This id is in the active list but no installed mod has it. Download it, or deactivate it. "
                "Use “Show in DB” from the right-click menu to look it up.\n", "muted")

    def _render_db(self, d: DbMod) -> None:
        self._put("Mod database entry\n", "small")
        self._put(d.display_name + "\n", "h1")
        if d.steam_name and d.steam_name != d.display_name:
            self._put(d.steam_name + "\n", "muted")
        if d.authors:
            self._put("by " + d.authors + "\n", "muted")
        uid = self._by_wid.get(d.workshop_id) or next(iter(self._by_pid.get(d.package_id, [])), None)
        installed = uid is not None
        self._put("● Installed\n" if installed else "○ Not installed\n", "ok" if installed else "muted")
        if d.unpublished:
            self._put("⚠ Removed from the Workshop (unpublished)\n", "err")
        self._put("\n")
        self._kv("Package id", d.package_id or "unknown until downloaded", "mono" if d.package_id else "muted")
        self._put("Workshop id  ", "key")
        self._link(d.workshop_id, lambda: self._open_web([d.workshop_id]))
        self._put("   ")
        self._link("open in Steam", lambda: self._open_steam([d.workshop_id]), "small")
        self._put("\n")
        vs = self.mgr.version_short
        self._kv("Versions", ", ".join(d.game_versions) or "(unknown)",
                 "err" if vs and d.game_versions and vs not in d.game_versions else "")
        if d.subscriptions:
            self._kv("Subscribers", f"{d.subscriptions:,}")
        if d.dependencies:
            self._put("Dependencies\n", "sect")
            app_ids = {RIMWORLD_APPID: CORE_ID, **DLC_BY_APPID}
            for wid, name in d.dependencies.items():
                have = app_ids[wid] in self._by_pid if wid in app_ids else wid in self._by_wid
                self._put("✔ " if have else "✖ ", "ok" if have else "err", "indent")
                self._put(f"{name or wid}", "indent")
                self._put(f"  ({wid})\n", "small")
        self._put("\n")
        if installed:
            self._link("Show installed mod", lambda: self.show_mod(uid))
        elif not d.is_dlc and not d.unpublished:
            self._link("⬇ Download", lambda: self.app.queue_download([d.to_ref()], activate=True))
        self._put("\n")
        if d.description:
            self._put("Description\n", "sect")
            self._put(_TAG_RE.sub("", d.description)[:20000].strip() + "\n", "desc")


def _selftest() -> None:
    """Build the tab against a tiny in-memory manager and exercise list editing (needs a display)."""
    import logging
    import time
    from rmm.gui.app import App

    mods = {u: Mod(uid=u, package_id=u, name=n, path=os.getcwd(), source=s, supported_versions=["1.6"])
            for u, n, s in ((CORE_ID, "Core", "core"), ("a.one", "Alpha", "workshop"), ("b.two", "Beta", "local"),
                            ("c.three", "Gamma", "steamcmd"))}

    class _S(dict):
        def get(self, k, d=None):
            return super().get(k, d)

        def set(self, k, v):
            self[k] = v

        def save(self):
            pass

    class _Db:
        def is_empty(self):
            return False

        def needs_update(self, _d):
            return False

    class _M:
        settings, db, log = _S(), _Db(), logging.getLogger("rmm.selftest")
        game_dir, config_dir, workshop_dir, game_version, version_short = "g", "c", "w", "1.6.1 rev1", "1.6"
        installed, active, dirty = mods, [], False

        def detect_paths(self):
            return {}

        def scan(self, progress=None):
            return self.installed

        def load_active_from_game(self):
            self.active = [CORE_ID, "missing.mod"]
            return self.active

        def set_active(self, uids):
            self.active, self.dirty = list(dict.fromkeys(uids)), True

        def activate(self, uids, index=None):
            new = [u for u in uids if u not in self.active]
            i = len(self.active) if index is None else index
            self.set_active(self.active[:i] + new + self.active[i:])

        def deactivate(self, uids):
            self.set_active([u for u in self.active if u not in uids])

        def check(self):
            return [Issue("error", "not_installed", "missing.mod", "missing.mod is not installed",
                          fix={"action": "remove", "uids": ["missing.mod"]})]

        def match_ref(self, ref):
            return ref.package_id if ref.package_id in self.installed else None

        def undo(self):
            return False

        redo = undo

    import rmm.gui.app as app_mod
    saved_tabs = app_mod.TABS
    app_mod.TABS = [t for t in saved_tabs if t[0] == "Mods"]      # only this tab: no other modules needed
    root = tk.Tk()
    try:
        app = App(_M(), root)
    finally:
        app_mod.TABS = saved_tabs
    app.error = app.info = lambda title, msg: (_ for _ in ()).throw(AssertionError(f"{title}: {msg}"))
    try:
        tab: ModsTab = app.tabs["Mods"]
        mgr = app.mgr
        end = time.time() + 3
        while time.time() < end and not tab._issues:
            root.update()
            time.sleep(0.01)
        assert mgr.active == [CORE_ID, "missing.mod"] and tab._issues, (mgr.active, tab._issues)
        assert len(tab.tree_in.get_children()) == 3 and len(tab.tree_act.get_children()) == 2
        assert tab.tree_act.item(tab._iid["missing.mod"], "tags") == ("missing",)
        tab._select(tab.tree_in, ["a.one", "c.three"])
        tab.activate_selected()
        assert mgr.active == [CORE_ID, "missing.mod", "a.one", "c.three"]
        tab._move(["c.three"], "to", CORE_ID, after=False)
        assert mgr.active[0] == "c.three"
        tab._move(["c.three", "a.one"], "bottom")
        assert mgr.active[-2:] == ["c.three", "a.one"]     # block keeps its relative order
        tab._select(tab.tree_act, ["a.one"])
        tab.move_selected("up")
        assert mgr.active.index("a.one") == 2
        order = [tab._uid[i] for i in tab.tree_act.get_children()]
        assert order == mgr.active, (order, mgr.active)
        tab.deactivate_selected(["a.one"])
        assert "a.one" not in mgr.active and tab.tree_in.exists(tab._iid["a.one"])
        tab.search_in._focus_in()
        tab.search_in.var.set("alp")
        tab._refresh_inactive()
        assert [tab._uid[i] for i in tab.tree_in.get_children()] == ["a.one"]
        tab.search_in.clear()
        tab.filter_var.set("Official")
        tab._refresh_inactive()
        assert tab.tree_in.get_children() == ()          # Core is active
        tab.filter_var.set("All")
        tab._refresh_inactive()
        assert "Tips" in tab.details.get("1.0", "end")          # shown before any selection
        root.minsize(1, 1)                          # a narrow window wraps the toolbar instead of hiding Check
        root.maxsize(5000, 5000)
        for width, wrapped in ((600, True), (3000, False), (600, True)):
            root.geometry(f"{width}x600")
            for _ in range(5):
                root.update()
            assert tab._wrapped is wrapped and tab._tools.winfo_ismapped(), (width, tab._wrapped)
            assert tab._tools.winfo_rootx() + tab._tools.winfo_width() <= root.winfo_rootx() + width
        asked = []                                  # launching with not-installed active mods asks first
        saved_ask, app.ask_yes_no = app.ask_yes_no, lambda t, m: asked.append(m) and False
        mgr.launch_game = lambda: asked.append("launched")
        tab.launch()
        app.ask_yes_no = saved_ask
        assert len(asked) == 1 and "not installed" in asked[0], asked
        assert asked[0].startswith(f"{len([u for u in mgr.active if u != CORE_ID])} "), asked   # steamcmd mod counts
        asked.clear()                               # a double-click launches once
        mgr.is_game_running = lambda: False
        app.ask_yes_no, mgr.dirty = (lambda t, m: True), False
        tab.launch()
        tab.launch()
        app.ask_yes_no = saved_ask
        end = time.time() + 3
        while time.time() < end and tab._launching:
            root.update()
            time.sleep(0.01)
        assert asked == ["launched"], asked
        tab._announce_check = True                  # a failed announced check doesn't leak the toast
        tab._check_failed(RuntimeError("x"))
        assert not tab._announce_check
        tab._active_set = set(mgr.active) | {"b.two", "gone.mod"}     # stale set (a background job changed the list)
        tab.details.configure(state="normal")
        tab._render_mod(mods["b.two"])              # used to raise ValueError from active.index()
        tab._render_missing("gone.mod")
        tab.show_mod("b.two")
        root.update()
        tab._render_details()
        assert "Beta" in tab.details.get("1.0", "end")
        # previews: read + size-checked on a worker, decoded on the Tk thread, then the details re-render
        import tempfile
        tmp = tempfile.mkdtemp()
        png = os.path.join(tmp, "p.png")
        img = tk.PhotoImage(width=640, height=40)
        img.put("#336699", to=(0, 0, 640, 40))
        img.write(png, format="png")
        huge = os.path.join(tmp, "huge.png")
        with open(png, "rb") as fh, open(huge, "wb") as out:
            data = bytearray(fh.read())
            data[16:24] = struct.pack(">II", 5000, 5000)          # IHDR claims 25 MP: never decoded
            out.write(data)
        with open(os.path.join(tmp, "x.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8\xff" + b"\0" * 40)
        assert isinstance(read_preview(png), bytes) and read_preview(huge) == "preview image too large to show"
        assert read_preview(os.path.join(tmp, "x.jpg")).startswith("preview is not") and read_preview(tmp + "/no.png") is None
        end = time.time() + 3
        while time.time() < end and tab._preview_loading:     # b.two's first (preview-less) read
            root.update()
        assert tab._preview_cache.get("b.two") is None and not tab.details.image_names()
        mods["b.two"].preview_path = png
        tab._preview_cache.clear()
        tab._render_details()
        while time.time() < end and not tab.details.image_names():
            root.update()
            time.sleep(0.01)
        assert tab.details.image_names() and tab._preview_cache["b.two"].width() <= tab.p(PREVIEW_MAX_W)
    finally:
        app.teardown()


if __name__ == "__main__":
    _selftest()
    print("mods_tab ok")
