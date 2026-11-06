"""Workshop tab: search the RimSort mod database (+ optional live Steam search), details, download/subscribe."""
from __future__ import annotations

import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, simpledialog

from rmm.gui.theme import PALETTES, ui_scale
from rmm.gui.widgets import VERSIONS, add_placeholder, ago, not_ready, style_text, track_ready
from rmm.models import DLC_BY_APPID, DLCS, LOADABLE_SOURCES, RIMWORLD_APPID, WORKSHOP_URL, DbMod, ModList, ModRef

PAGE = 50
ANY = "Any"
SORTS = {"Relevance": "relevance", "Popular": "popular", "Name": "name"}

# ---------------------------------------------------------------- helpers
def strip_bbcode(s: str) -> str:
    """Steam workshop descriptions use BBCode; show them as readable plain text."""
    s = re.sub(r"\[img\].*?\[/img\]|\[previewyoutube=[^\]]*\].*?\[/previewyoutube\]", "", s or "", flags=re.I | re.S)
    s = re.sub(r"\[url=([^\]]*)\](.*?)\[/url\]", r"\2 (\1)", s, flags=re.I | re.S)
    s = re.sub(r"\s*\[\*\]\s*", "\n• ", s)
    s = re.sub(r"\[/h[1-6]\]|\[/?(?:list|olist|table|tr|quote)\]|\[hr\]\[/hr\]|\[hr\]", "\n", s, flags=re.I)
    s = re.sub(r"\[/?(?:h[1-6]|b|i|u|s|strike|spoiler|noparse|hr|list|olist|quote(?:=[^\]]*)?|code|table|tr|th|td|url)\]",
               "", s, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def fmt_versions(vs: list[str]) -> str:
    def key(v):
        return tuple(int(x) for x in v.split(".") if x.isdigit())
    vs = sorted({v for v in vs if v}, key=key)
    return ("…" if len(vs) > 3 else "") + ", ".join(vs[-3:])


# ---------------------------------------------------------------- the tab
class WorkshopTab(ttk.Frame):
    COLS = (("name", "Name", 300, "w", True), ("author", "Author", 130, "w", False),
            ("versions", "Versions", 100, "w", False), ("inst", "Installed", 70, "center", False),
            ("act", "Active", 56, "center", False), ("subs", "Subs", 80, "e", False))

    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app, self.mgr = app, app.mgr
        self._gen = 0                               # search generation: stale bg results are dropped
        self._rows: dict[str, list] = {}            # workshop id -> [DbMod, installed uid | None]
        self._params: dict = {}
        self._source, self._offset, self._page, self._more = "db", 0, 1, False
        self._after = None
        self._shown = False
        self._ver_touched = False
        self._cur: list | None = None               # [DbMod, uid] shown in the details panel
        self._dep_ids: list[str] = []
        self._modlist_names: list[str] = []
        self._browser_on = False
        track_ready(self)
        self._build()
        self._apply_colors()
        for ev, cb in (("db_updated", self._on_db_updated), ("scanned", self._on_scanned),
                       ("active_changed", self._refresh_flags), ("modlists_changed", self._load_modlist_names),
                       ("theme_changed", self._apply_colors), ("workshop_pick", self._on_browser_pick)):
            app.on(ev, cb)
        self.bind("<Map>", self._on_map, add="+")
        self._refresh_db_status()
        self._load_modlist_names()

    # ------------------------------------------------------------ layout
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        head = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(head, text="Open Steam Workshop", style="Accent.TButton",     # packed first: never clipped
                   command=self.open_browser).pack(side="right")
        ttk.Label(head, text="Steam Workshop", style="PanelHeading.TLabel").pack(side="left")
        ttk.Label(head, text="  Search RimSort's Workshop database or live Steam; add installed mods or download missing ones.",
                  style="PanelMuted.TLabel").pack(side="left")

        bar = ttk.Frame(self, style="Card.TFrame", padding=(10, 8))
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        bar.columnconfigure(0, weight=1)
        self.q_var = tk.StringVar()
        self.q_entry = ttk.Entry(bar, textvariable=self.q_var)
        self.q_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8), ipady=2)
        ph = add_placeholder(self.q_entry, "Search by name, author or packageId…  (e.g. harmony, vanilla expanded, rimhud)",
                             self.app)
        self.q_var.trace_add("write", lambda *_: (ph(), self._schedule()))
        self.q_entry.bind("<Return>", self.search)
        self.q_entry.bind("<Down>", lambda e: self._focus_tree())
        ttk.Label(bar, text="Version", style="Panel.TLabel").grid(row=0, column=1, padx=(0, 4))
        cur = self.mgr.version_short or ""
        self.ver_var = tk.StringVar(value=cur or ANY)
        vals = ([cur] if cur and cur not in VERSIONS else []) + VERSIONS + [ANY]
        self.ver_box = ttk.Combobox(bar, textvariable=self.ver_var, values=vals, width=5, state="readonly")
        self.ver_box.grid(row=0, column=2, padx=(0, 8))
        self.ver_box.bind("<<ComboboxSelected>>", self._on_version_pick)
        ttk.Label(bar, text="Sort", style="Panel.TLabel").grid(row=0, column=3, padx=(0, 4))
        self.sort_var = tk.StringVar(value="Relevance")
        sb = ttk.Combobox(bar, textvariable=self.sort_var, values=list(SORTS), width=9, state="readonly")
        sb.grid(row=0, column=4, padx=(0, 8))
        sb.bind("<<ComboboxSelected>>", self.search)
        self.pid_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Only with packageId", variable=self.pid_var, command=self.search).grid(row=0, column=5, padx=(0, 8))
        self.live_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Live Steam search", variable=self.live_var, command=self.search).grid(row=0, column=6, padx=(0, 8))
        ttk.Button(bar, text="Search", style="Accent.TButton", command=self.search).grid(row=0, column=7)

        pw = ttk.Panedwindow(self, orient="horizontal")
        pw.grid(row=2, column=0, sticky="nsew")

        # results
        left = ttk.Frame(pw, style="Card.TFrame", padding=10)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended")
        s = ui_scale(self)                          # column widths are pixels, fonts scale with DPI
        for cid, text, width, anchor, stretch in self.COLS:
            self.tree.heading(cid, text=text, command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=round(width * s), minwidth=round(40 * s), anchor=anchor, stretch=stretch)
        ysb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        self.empty_lbl = ttk.Label(self.tree, style="Muted.TLabel", justify="center", wraplength=420)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._show_in_mods() if self._cur else None)
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Return>", lambda e: self._download())
        foot = ttk.Frame(left, style="Panel.TFrame")
        foot.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.count_lbl = ttk.Label(foot, text="", style="PanelMuted.TLabel")
        self.count_lbl.pack(side="left")
        self.more_btn = ttk.Button(foot, text="Load more", command=self._load_more, state="disabled")
        self.more_btn.pack(side="right")
        pw.add(left, weight=3)

        # details
        right = ttk.Frame(pw, style="Card.TFrame", padding=12)
        right.columnconfigure(1, weight=1)
        self.d_title = ttk.Label(right, text="Select a mod", style="PanelHeading.TLabel", wraplength=340)
        self.d_title.grid(row=0, column=0, columnspan=2, sticky="w")
        self.d_author = ttk.Label(right, text="Details, dependencies and description appear here.", style="PanelMuted.TLabel",
                                  wraplength=340)
        self.d_author.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.d_vals = {}
        for i, (key, label) in enumerate((("pid", "Package id"), ("wid", "Workshop id"), ("ver", "Game versions"),
                                          ("status", "Status"), ("subs", "Subscribers"), ("upd", "Updated")), start=3):
            ttk.Label(right, text=label, style="PanelMuted.TLabel").grid(row=i, column=0, sticky="nw", padx=(0, 12))
            self.d_vals[key] = ttk.Label(right, text="—", style="Panel.TLabel", wraplength=260)
            self.d_vals[key].grid(row=i, column=1, sticky="w")
        # buttons right under the title: a short window cuts the description, dependencies and facts, not the buttons
        btns = ttk.Frame(right, style="Panel.TFrame")
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        btns.columnconfigure((0, 1), weight=1, uniform="b")
        self.b_add = ttk.Button(btns, text="Add to Active", style="Accent.TButton", command=self._add_to_active)
        self.b_dl = ttk.Button(btns, text="Download", command=self._download)
        self.b_sub = ttk.Button(btns, text="Steam Subscribe", command=self._subscribe)
        self.b_page = ttk.Button(btns, text="Open Page", command=lambda: self._open("browser"))
        self.b_steam = ttk.Button(btns, text="Open in Steam", command=lambda: self._open("steam"))
        self.b_list = ttk.Button(btns, text="Add to Modlist ▾", command=self._modlist_menu)
        for i, b in enumerate((self.b_add, self.b_dl, self.b_sub, self.b_list, self.b_page, self.b_steam)):
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=(i % 2 * 4, 0), pady=(0 if i < 2 else 4, 0))
        self.d_hint = ttk.Label(right, text="Dependencies", style="PanelMuted.TLabel")   # + what Download does with them
        self.d_hint.grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 2))
        self.deps = tk.Listbox(right, height=4, activestyle="none", exportselection=False)
        self.deps.grid(row=11, column=0, columnspan=2, sticky="nsew")
        self.deps.bind("<Double-1>", self._open_dependency)
        ttk.Label(right, text="Description", style="Muted.TLabel").grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 2))
        dframe = ttk.Frame(right)
        dframe.grid(row=13, column=0, columnspan=2, sticky="nsew")
        dframe.columnconfigure(0, weight=1)
        dframe.rowconfigure(0, weight=1)
        right.rowconfigure(13, weight=1)             # the description shrinks first, then the bottom rows are cut
        self.desc = tk.Text(dframe, height=8, width=44, wrap="word", padx=8, pady=6, state="disabled")
        dsb = ttk.Scrollbar(dframe, orient="vertical", command=self.desc.yview)
        self.desc.configure(yscrollcommand=dsb.set)
        self.desc.grid(row=0, column=0, sticky="nsew")
        dsb.grid(row=0, column=1, sticky="ns")
        right.bind("<Configure>", self._on_details_resize)
        pw.add(right, weight=2)
        self._set_detail_buttons(False)

        status = ttk.Frame(self)
        status.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.db_btn = ttk.Button(status, text="Update database", command=self._update_db)
        self.db_btn.pack(side="right")               # packed first: the long status text is what gets cut
        self.db_lbl = ttk.Label(status, text="Database: …", style="Muted.TLabel")
        self.db_lbl.pack(side="left")

    def _apply_colors(self, *_):
        c = self.app.colors
        style_text(self.desc, c)
        style_text(self.deps, c)
        self.tree.tag_configure("installed", foreground=c["ok"])
        self.tree.tag_configure("dim", foreground=c["muted"])
        self.tree.tag_configure("error", foreground=c["err"])
        self.empty_lbl.configure(background=ttk.Style(self).lookup("Treeview", "background") or c["field"])
        if self._db_status is not None:
            self._show_db_status(self._db_status)

    def _on_details_resize(self, e):
        w = max(200, e.width - 24)
        self.d_title.configure(wraplength=w)
        self.d_author.configure(wraplength=w)
        for lbl in self.d_vals.values():
            lbl.configure(wraplength=max(120, w - round(110 * ui_scale(self))))

    def focus_search(self):
        self.q_entry.focus_set()
        self.q_entry.select_range(0, "end")

    def _focus_tree(self):
        kids = self.tree.get_children()
        if kids:
            self.tree.focus_set()
            self.tree.focus(kids[0])
            self.tree.selection_set(kids[0])

    # ------------------------------------------------------------ events
    def _on_map(self, _e=None):
        if not self._shown:
            self._shown = True
            self.search()

    def _on_db_updated(self, *_):
        self._refresh_db_status()
        if self._shown:
            self.search()

    def _on_scanned(self, *_):
        if not self._ver_touched and self.mgr.version_short:
            v = self.mgr.version_short
            if v not in self.ver_box.cget("values"):
                self.ver_box.configure(values=[v, *VERSIONS, ANY])
            if self.ver_var.get() != v:
                self.ver_var.set(v)
                if self._shown:
                    self.search()
                    return
        # installed state changed: re-match current rows off the Tk thread
        items = [(wid, row[0]) for wid, row in self._rows.items()]
        if items:
            self.app.run_bg(lambda: {w: self.mgr.match_ref(m.to_ref()) for w, m in items},
                            on_done=self._apply_matches, on_error=lambda e: None)

    def _apply_matches(self, matches: dict):
        for wid, uid in matches.items():
            if wid in self._rows:
                self._rows[wid][1] = uid
        self._refresh_flags()

    def _on_version_pick(self, *_):
        self._ver_touched = True
        self.search()

    # ------------------------------------------------------------ search / paging
    def _schedule(self):
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(300, self.search)

    def search(self, *_):
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        q = self.q_var.get().strip()
        ver = self.ver_var.get()
        sort = SORTS.get(self.sort_var.get(), "relevance")
        if not q and sort == "relevance":
            sort = "popular"
        self._params = {"query": q, "version": "" if ver == ANY else ver, "sort": sort,
                        "require_package_id": self.pid_var.get(), "live": self.live_var.get()}
        self._gen += 1
        self._rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.empty_lbl.place_forget()
        self._source, self._offset, self._page, self._more = "db", 0, 1, False
        self._fetch()
        return "break"

    def _load_more(self):
        if self._more:
            self._fetch()

    def _fetch(self):
        gen = self._gen
        self.more_btn.state(["disabled"])
        self.count_lbl.configure(text="Searching…")
        self.app.run_bg(self._fetch_job, dict(self._params), self._source, self._offset, self._page,
                        on_done=lambda r: self._on_page(gen, r), on_error=lambda e: self._on_fetch_error(gen, e))

    def _fetch_job(self, p: dict, source: str, offset: int, page: int):
        """Background: one page from the DB; falls through to live Steam search when enabled and the DB runs dry."""
        rows: list[DbMod] = []
        more = False
        if source == "db":
            rows = self.mgr.search_db(p["query"], version=p["version"], limit=PAGE, offset=offset,
                                      require_package_id=p["require_package_id"], sort=p["sort"])
            offset += len(rows)
            more = len(rows) >= PAGE
            if not more and p["live"] and p["query"]:
                source, page = "steam", 1
        if source == "steam" and len(rows) < PAGE:
            from rmm import steam
            items = steam.search_workshop(p["query"], page=page, api_key=self.mgr.settings.get_secret("steam_api_key")
                                          or os.environ.get("STEAM_API_KEY", ""))
            page += 1
            more = bool(items)
            known = self.mgr.db.get_many([str(i["workshop_id"]) for i in items]) if items else {}
            for it in items:
                wid = str(it["workshop_id"])
                m = known.get(wid) or DbMod(workshop_id=wid, steam_name=it.get("title", ""),
                                            authors=it.get("author", "") or "",
                                            subscriptions=int(it.get("subscriptions") or 0))
                if m.unpublished or (p["require_package_id"] and not m.package_id):
                    continue
                if p["version"] and m.game_versions and p["version"] not in m.game_versions:
                    continue
                rows.append(m)
        matched = [(m, self.mgr.match_ref(m.to_ref())) for m in rows]
        return matched, source, offset, page, more

    def _on_page(self, gen: int, result):
        if gen != self._gen:
            return
        matched, self._source, self._offset, self._page, self._more = result
        active = set(self.mgr.active)
        need_details = []
        for m, uid in matched:
            if m.workshop_id in self._rows:
                continue
            self._rows[m.workshop_id] = [m, uid]
            self.tree.insert("", "end", iid=m.workshop_id, values=self._values(m, uid, active), tags=self._tags(m, uid))
            if not m.subscriptions and not m.is_dlc:
                need_details.append(m.workshop_id)
        self._update_count()
        if not self._rows:
            self._show_empty()
        if need_details:
            self._enrich(need_details)

    def _on_fetch_error(self, gen, e):
        if gen != self._gen:
            return
        self._update_count()  # _source/_offset/_page are untouched, so "Load more" retries the same page
        self.count_lbl.configure(text=f"{self.count_lbl.cget('text')} · load failed: {e}")
        self.app.status(f"Search failed: {e}")

    def _show_empty(self):
        p = self._params
        if self._db_empty:
            msg = "The mod database is empty — click “Update database” below (or enable Live Steam search)."
        elif not p.get("query"):
            msg = "Type to search. (Popular mods appear here once download counts have been fetched.)"
        else:
            tips = [t for t, on in (("choose version “Any”", p.get("version")),
                                    ("untick “Only with packageId”", p.get("require_package_id")),
                                    ("enable Live Steam search", not p.get("live"))) if on]
            msg = "No matches. Try fewer words" + ("; " + ", ".join(tips) if tips else "") + "."
        self.empty_lbl.configure(text=msg)
        self.empty_lbl.place(relx=0.5, rely=0.2, anchor="n")

    def _update_count(self):
        n = len(self._rows)
        src = " · incl. live Steam results" if self._source == "steam" else ""
        self.count_lbl.configure(text=f"{n} result{'s' if n != 1 else ''}{src}" + (" · more available" if self._more else ""))
        self.more_btn.state(["!disabled"] if self._more else ["disabled"])

    def _values(self, m: DbMod, uid, active: set) -> tuple:
        return (m.display_name, m.authors, fmt_versions(m.game_versions) or "?", "✔" if uid else "", "✔" if uid and uid in active else "",
                f"{m.subscriptions:,}" if m.subscriptions else "")

    def _tags(self, m: DbMod, uid) -> tuple:
        if uid:
            return ("installed",)
        cur = self.mgr.version_short
        if m.unpublished or (cur and m.game_versions and cur not in m.game_versions):
            return ("dim",)
        return ()

    def _refresh_flags(self, *_):
        active = set(self.mgr.active)
        for wid, (m, uid) in self._rows.items():
            if self.tree.exists(wid):
                self.tree.item(wid, values=self._values(m, uid, active), tags=self._tags(m, uid))
        if self._cur:
            self._show_details(self._cur, fetch=False)

    def _sort_by(self, col: str):
        """Header click: sort the loaded rows locally (Subs starts descending); a second click reverses."""
        numeric = col == "subs"
        prev = getattr(self, "_sort_state", None)
        rev = (not prev[1]) if prev and prev[0] == col else numeric
        self._sort_state = (col, rev)
        items = [(self.tree.set(i, col), i) for i in self.tree.get_children()]
        items.sort(key=lambda t: int(t[0].replace(",", "") or 0) if numeric else t[0].lower(), reverse=rev)
        for idx, (_, iid) in enumerate(items):
            self.tree.move(iid, "", idx)

    # ------------------------------------------------------------ enrichment / details
    def _enrich(self, wids: list[str]):
        def job():
            from rmm import steam
            return steam.get_details(wids, db=self.mgr.db)
        self.app.run_bg(job, on_done=self._on_enriched, on_error=lambda e: None)

    def _on_enriched(self, details: dict):
        active = set(self.mgr.active)
        for wid, d in (details or {}).items():
            row = self._rows.get(str(wid))
            if not row or not isinstance(d, dict):
                continue
            m = row[0]
            m.subscriptions = int(d.get("subscriptions") or m.subscriptions or 0)
            m.time_updated = int(d.get("time_updated") or m.time_updated or 0)
            m.description = d.get("description") or m.description
            m.preview_url = d.get("preview_url") or m.preview_url
            m.steam_name = m.steam_name or d.get("title", "")
            if self.tree.exists(m.workshop_id):
                self.tree.item(m.workshop_id, values=self._values(m, row[1], active))
            if self._cur and self._cur[0] is m:
                self._show_details(self._cur, fetch=False)

    def _on_select(self, _e=None):
        sel = [i for i in self.tree.selection() if i in self._rows]
        if sel:
            self._show_details(self._rows[sel[0]])

    def _set_detail_buttons(self, on: bool):
        for b in (self.b_add, self.b_dl, self.b_sub, self.b_page, self.b_steam, self.b_list):
            b.state(["!disabled"] if on else ["disabled"])

    def _show_details(self, row: list, fetch: bool = True):
        self._cur = row
        m, uid = row
        self._set_detail_buttons(True)
        if not uid:
            self.b_add.state(["disabled"])
        if m.is_dlc:
            for b in (self.b_dl, self.b_sub, self.b_list):
                b.state(["disabled"])
        self.d_title.configure(text=m.display_name)
        sub = f"by {m.authors}" if m.authors else "Author unknown"
        if m.steam_name and m.steam_name != m.display_name:
            sub += f"\nWorkshop title: {m.steam_name}"
        self.d_author.configure(text=sub)
        cur = self.mgr.version_short
        active = uid and uid in set(self.mgr.active)
        vers = ", ".join(sorted(m.game_versions)) or "unknown"
        if cur and m.game_versions and cur not in m.game_versions:
            vers += f"   (no {cur} support listed)"
        self.d_vals["pid"].configure(text=m.package_id or "unknown until downloaded")
        self.d_vals["wid"].configure(text=m.workshop_id)
        self.d_vals["ver"].configure(text=vers)
        self.d_vals["status"].configure(text="Active" if active else ("Installed (inactive)" if uid else
                                        ("Removed from Workshop" if m.unpublished else "Not installed")))
        self.d_vals["subs"].configure(text=f"{m.subscriptions:,}" if m.subscriptions else "—")
        self.d_vals["upd"].configure(text=time.strftime("%Y-%m-%d", time.localtime(m.time_updated)) if m.time_updated else "—")
        self.d_hint.configure(text="Dependencies" if uid else "Dependencies (missing ones download too)")
        self._set_desc(strip_bbcode(m.description) if m.description else ("Loading description…" if fetch else "No description."))
        if fetch:
            self.deps.delete(0, "end")
            self._dep_ids = []
            self.app.run_bg(self._details_job, m, on_done=lambda r, row=row: self._on_details(row, r),
                            on_error=lambda e: self._set_desc("No description (offline?)"))

    def _set_desc(self, text: str):
        self.desc.configure(state="normal")
        self.desc.delete("1.0", "end")
        self.desc.insert("1.0", text)
        self.desc.configure(state="disabled")

    def _details_job(self, m: DbMod):
        det = {}
        if not m.description and not m.is_dlc:
            try:
                from rmm import steam
                det = steam.get_details([m.workshop_id], db=self.mgr.db).get(m.workshop_id) or {}
            except Exception:
                det = {}
        owned = self.mgr.owned_dlcs()
        active = set(self.mgr.active)
        deps = []
        for dwid, dname in (m.dependencies or {}).items():
            if dwid == RIMWORLD_APPID:
                continue
            if dwid in DLC_BY_APPID:
                pid = DLC_BY_APPID[dwid]
                ok = pid in owned
                deps.append((dwid, f"{'✔' if ok else '✖'} DLC: {DLCS[pid][1]}{'' if ok else ' (not owned)'}", "ok" if ok else "err"))
                continue
            uid = self.mgr.match_ref(ModRef(workshop_id=dwid, name=dname or ""))
            name = dname or dwid
            if uid and uid in active:
                deps.append((dwid, f"✔ {name}", "ok"))
            elif uid:
                deps.append((dwid, f"● {name}  (installed, inactive)", "warn"))
            else:
                deps.append((dwid, f"✖ {name}  (not installed)", "err"))
        return det, deps

    def _on_details(self, row: list, result):
        if self._cur is not row:
            return
        det, deps = result
        m = row[0]
        if det:
            m.description = det.get("description") or m.description
            m.subscriptions = int(det.get("subscriptions") or m.subscriptions or 0)
            m.time_updated = int(det.get("time_updated") or m.time_updated or 0)
            m.steam_name = m.steam_name or det.get("title", "")
        c = self.app.colors
        self.deps.delete(0, "end")
        self._dep_ids = [d[0] for d in deps]
        for i, (_, text, level) in enumerate(deps):
            self.deps.insert("end", text)
            self.deps.itemconfigure(i, foreground=c[level])
        if not deps:
            self.deps.insert("end", "No dependencies listed")
            self.deps.itemconfigure(0, foreground=c["muted"])
        self._show_details(row, fetch=False)

    def _open_dependency(self, _e=None):
        sel = self.deps.curselection()
        if not sel or sel[0] >= len(self._dep_ids):
            return
        wid = self._dep_ids[sel[0]]
        if wid in DLC_BY_APPID:
            return
        if wid in self._rows:
            self.tree.selection_set(wid)
            self.tree.see(wid)
            return
        label = self.deps.get(sel[0]).lstrip("✔✖● ").split("  (")[0]

        def job():
            m = self.mgr.db.get(wid) or DbMod(workshop_id=wid, steam_name=label)
            return [m, self.mgr.match_ref(m.to_ref())]
        self.app.run_bg(job, on_done=self._show_details)

    # ------------------------------------------------------------ actions
    def _targets(self) -> list[list]:
        sel = [self._rows[i] for i in self.tree.selection() if i in self._rows]
        if self._cur and all(r is not self._cur for r in sel):
            return [self._cur]
        return sel or ([self._cur] if self._cur else [])

    def _add_to_active(self):
        if not_ready(self):
            return
        rows = self._targets()
        active = set(self.mgr.active)
        uids = [uid for m, uid in rows if uid and uid not in active]
        missing = [m for m, uid in rows if not uid]
        if not uids:
            self.app.toast("Already active." if not missing else "Not installed yet — use Download first.", "warning")
            return
        self.mgr.activate(uids)
        self.app.emit("active_changed")

        def job():
            return self.mgr.resolve_dependencies()
        self.app.run_bg(job, busy="Checking dependencies…",
                        on_done=lambda r: self._after_activate(len(uids), len(missing), r))

    def _after_activate(self, n: int, n_missing: int, result):
        added, to_dl, unresolved = result
        if added:
            self.app.emit("active_changed")
        msg = f"Activated {n} mod{'s' if n != 1 else ''}" + (f" + {len(added)} installed dependencies" if added else "")
        if n_missing:
            msg += f" · {n_missing} not installed"
        self.app.toast(msg + " — Save in the Mods tab to apply.", "info")
        if to_dl and self.app.ask_yes_no("Missing dependencies",
                                         f"{len(to_dl)} required dependencies are not installed:\n\n"
                                         + "\n".join(f"• {r.name or r.package_id or r.workshop_id}" for r in to_dl[:15])
                                         + ("\n…" if len(to_dl) > 15 else "") + "\n\nDownload them now?"):
            self.app.queue_download(to_dl, activate=True)     # dependencies of active mods: active once installed
        if unresolved:
            self.app.status("Unresolvable: " + "; ".join(unresolved[:3]))

    def _download(self, method: str | None = None):
        rows = [r for r in self._targets() if not r[0].is_dlc]
        if not rows or not_ready(self):
            return
        n = len(rows)
        self.app.queue_download([m.to_ref() for m, _ in rows], method)   # Manager.download adds the missing deps
        self.app.toast(f"{'Subscribing to' if method == 'steamworks' else 'Queued'} {n} mod{'s' if n != 1 else ''} "
                       "+ missing dependencies — see Downloads.", "info")

    def _subscribe(self):
        rows = [r for r in self._targets() if not r[0].is_dlc]
        if rows and self.app.ask_yes_no(
                "Subscribe via Steam",
                f"Subscribe your Steam account to {len(rows)} mod{'s' if len(rows) != 1 else ''} (plus missing dependencies)?\n\n"
                "Steam downloads them into the Workshop folder. Steam must be running."):
            self._download("steamworks")

    def _open(self, where: str):
        wids = [m.workshop_id for m, _ in self._targets()][:20]
        if not wids:
            return

        def job():
            from rmm import steam
            return (steam.open_in_steam if where == "steam" else steam.open_in_browser)(wids)
        # open_in_steam returns False when it fell back to the browser (no steam:// handler)
        self.app.run_bg(job, busy="Opening in Steam…" if where == "steam" else "Opening in browser…",
                        on_done=lambda ok: ok is False and where == "steam" and self.app.toast(
                            "Steam isn't registered for steam:// links, so the pages opened in your browser", "warn"))

    def _show_in_mods(self):
        m, uid = self._cur
        self.app.show_mod_details(uid=uid) if uid else self.app.show_mod_details(db_mod=m)

    def _copy(self, text: str):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.app.toast(f"Copied {text}", "info")

    def _context_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid or iid not in self._rows:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._show_details(self._rows[iid])
        m, uid = self._rows[iid]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Add to active", command=self._add_to_active, state="normal" if uid else "disabled")
        menu.add_command(label="Download (+ dependencies)", command=self._download)
        menu.add_command(label="Subscribe via Steam", command=self._subscribe)
        menu.add_separator()
        menu.add_command(label="Show in Mods tab", command=self._show_in_mods)
        menu.add_command(label="Open workshop page", command=lambda: self._open("browser"))
        menu.add_command(label="Open in Steam", command=lambda: self._open("steam"))
        menu.add_separator()
        menu.add_command(label="Copy workshop id", command=lambda: self._copy(m.workshop_id))
        menu.add_command(label="Copy package id", command=lambda: self._copy(m.package_id),
                         state="normal" if m.package_id else "disabled")
        menu.tk_popup(e.x_root, e.y_root)

    # ------------------------------------------------------------ Steam Workshop browser
    def open_browser(self):
        """The real workshop pages in their own window (rmm.browser); each "+ Download" click lands in
        _on_browser_pick."""
        from rmm import browser, config
        if not_ready(self):
            return
        if self._browser_on:
            self.app.toast("The Steam Workshop window is already open", "info")
            return
        why = browser.available()
        if why:
            self.app.error("Steam Workshop", why)
            return
        wids = sorted({m.workshop_id for m in self.mgr.installed.values()      # copies RimWorld really loads
                       if m.source in LOADABLE_SOURCES and (m.workshop_id or "").isdigit()})
        self._browser_on = True

        def done(err):
            self._browser_on = False
            if err:
                self.app.error("Steam Workshop", f"{err}\n\nThe window needs the Microsoft Edge WebView2 Runtime (part of "
                                                 "Windows 11; a free download from Microsoft for Windows 10).")
        self.app.run_bg(browser.run, wids, lambda pick: self.app.emit("workshop_pick", pick),
                        storage=os.path.join(config.APP_DIR, "webview"), on_done=done, on_error=lambda e: done(str(e)))
        self.app.toast("Opening the Steam Workshop… “+ Download” sends a mod to the download queue", "info")

    def _on_browser_pick(self, pick: dict):
        """A "+ Download" click in the browser: queue the mod, or the collection's mods, that are not installed."""
        wid = str(pick.get("id") or "")
        label = str(pick.get("title") or wid)
        if not wid.isdigit() or not_ready(self):
            return

        def job():
            ml = self.mgr.import_text(WORKSHOP_URL.format(wid))       # a collection expands to its mods
            return ml, self.mgr.missing_refs(ml.mods)

        def done(res):
            ml, refs = res
            if pick.get("kind") == "collection" and ml.source != "import:collection":
                self.app.toast(f"Could not read the collection “{label}” from Steam — offline, or a private collection?",
                               "error")
                return
            name = ml.name if ml.source == "import:collection" else label
            if not refs:
                self.app.toast(f"Already installed: {name}", "info")
                return
            self.app.queue_download(refs)            # Manager.download adds the missing dependencies
            skipped = len(ml.mods) - len(refs)
            self.app.toast(f"Queued {len(refs)} mod{'s' if len(refs) != 1 else ''} from “{name}”"
                           + (f" ({skipped} already installed)" if skipped else ""), "info")
        self.app.run_bg(job, on_done=done, on_error=lambda e: self.app.toast(f"Could not queue {label}: {e}", "error"))

    # ------------------------------------------------------------ modlists
    def _load_modlist_names(self, *_):
        self.app.run_bg(lambda: [ml.name for ml in self.mgr.list_modlists()],
                        on_done=lambda names: setattr(self, "_modlist_names", names), on_error=lambda e: None)

    def _modlist_menu(self):
        menu = tk.Menu(self, tearoff=0)
        for name in self._modlist_names:
            menu.add_command(label=name, command=lambda n=name: self._add_to_modlist(n))
        if self._modlist_names:
            menu.add_separator()
        menu.add_command(label="New modlist…", command=lambda: self._add_to_modlist(None))
        b = self.b_list
        menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())

    def _add_to_modlist(self, name: str | None):
        refs = [m.to_ref() for m, _ in self._targets() if not m.is_dlc]
        if not refs:
            return
        if name is None:
            name = simpledialog.askstring("New modlist", "Name for the new modlist:", parent=self)
            if not name or not name.strip():
                return
            name = name.strip()

        def job():
            ml = next((x for x in self.mgr.list_modlists() if x.name == name), None)
            now = time.time()
            if ml is None:
                ml = ModList(name=name, game_version=self.mgr.game_version, created=now)
            pids = {r.package_id for r in ml.mods if r.package_id}
            wids = {r.workshop_id for r in ml.mods if r.workshop_id}
            added = [r for r in refs if not ((r.package_id and r.package_id in pids) or (r.workshop_id and r.workshop_id in wids))]
            ml.mods.extend(added)
            ml.updated = now
            self.mgr.save_modlist(ml)          # renames ml when the name clashes with a different saved list
            return len(added), ml.name

        def done(res):
            n, final = res
            self.app.emit("modlists_changed")
            self.app.toast(f"Added {n} mod{'s' if n != 1 else ''} to “{final}”" if n else f"Already in “{final}”", "info")
        self.app.run_bg(job, on_done=done)

    # ------------------------------------------------------------ database
    _db_empty = False
    _db_status: dict | None = None

    def _refresh_db_status(self, *_):
        self.app.run_bg(self.mgr.db.status, on_done=self._show_db_status, on_error=lambda e: None)

    def _show_db_status(self, st: dict):
        self._db_status = st
        n = int(st.get("steam_count") or 0)
        self._db_empty = n == 0
        c = self.app.colors
        if not n:
            self.db_lbl.configure(text="Mod database is empty — click “Update database” to download RimSort's databases.",
                                  foreground=c["warn"])
            return
        src = f" · source: {st['source']}" if st.get("source") else ""
        self.db_lbl.configure(foreground=c["muted"],
                              text=f"Database: {n:,} mods · {int(st.get('rules_count') or 0):,} load-order rules · "
                                   f"{int(st.get('uti_count') or 0):,} replacements · updated {ago(st.get('updated_at') or 0)}{src}")

    def _update_db(self):
        """App.update_db owns the job (one update, one Cancel), the toast, the error dialog and 'db_updated'."""
        self.db_btn.state(["disabled"])

        def restore(_=None):
            self.db_btn.state(["!disabled"])
            self._refresh_db_status()
        self.app.update_db(on_done=restore, on_error=restore)


# ---------------------------------------------------------------- self-test
class _SmokeApp:
    """Minimal stand-in for App used by the tab self-tests: bg jobs run synchronously on the Tk thread."""

    def __init__(self, root, mgr):
        self.root, self.mgr, self.colors, self.fonts = root, mgr, dict(PALETTES["dark"]), {}
        self.handlers: dict = {}
        self.toasts: list = []
        self.queued: list = []

    def run_bg(self, fn, *args, on_done=None, on_error=None, busy="", progress=False, cancellable=False, **kw):
        ev = threading.Event()
        if progress:
            kw["progress"] = lambda f=None, t="": None
        if cancellable:
            kw["cancel"] = ev
        try:
            r = fn(*args, **kw)
        except Exception as e:  # mirrors App: route to on_error or re-raise for the test
            if on_error:
                on_error(e)
                return ev
            raise
        if on_done:
            on_done(r)
        return ev

    def on(self, ev, cb):
        self.handlers.setdefault(ev, []).append(cb)

    def emit(self, ev, *a):
        for cb in self.handlers.get(ev, []):
            cb(*a)

    def status(self, t): pass
    def toast(self, t, level="info"): self.toasts.append(t)
    def ask_yes_no(self, title, msg): return False
    def error(self, title, msg): raise AssertionError(f"{title}: {msg}")
    def info(self, title, msg): self.toasts.append(msg)
    def select_tab(self, name): pass
    def show_mod_details(self, uid=None, db_mod=None): pass
    def queue_download(self, refs, method=None, **opts): self.queued.append((refs, method, opts))


class _SmokeDb:
    def __init__(self, rows: list[DbMod]):
        self.rows = rows

    def status(self): return {"steam_count": len(self.rows), "rules_count": 1, "updated_at": time.time() - 7300}
    def get(self, wid): return next((r for r in self.rows if r.workshop_id == wid), None)
    def get_many(self, ids): return {r.workshop_id: r for r in self.rows if r.workshop_id in ids}


class _SmokeMgr:
    """Just enough Manager for the three tab self-tests (rows carry subs + description so no Steam calls happen)."""

    def __init__(self):
        from rmm.models import Mod
        self.version_short, self.game_version = "1.6", "1.6.4871 rev590"
        self.installed = {"brrainz.harmony": Mod("brrainz.harmony", "brrainz.harmony", "Harmony", "", "workshop", "2009463077")}
        self.active = ["brrainz.harmony"]
        self.rows = [DbMod("2009463077", "brrainz.harmony", "Harmony", "Harmony", "Brrainz", ["1.5", "1.6"],
                           subscriptions=900000, description="Patch library"),
                     DbMod("1111", "a.b", "Alpha", "Alpha", "Ann", ["1.6"], {"2009463077": "Harmony", "1149640": "Royalty"},
                           subscriptions=10, description="[b]Alpha[/b] mod")]
        self.db = _SmokeDb(self.rows)
        self.lists = {}
        self.settings = type("S", (), {"get": lambda s, k, d=None: d, "get_secret": lambda s, k: ""})()

    def search_db(self, query, **kw):
        return [r for r in self.rows if query.lower() in r.display_name.lower()][kw.get("offset", 0):][:kw.get("limit", 50)]

    def match_ref(self, ref):
        return next((u for u, m in self.installed.items() if (ref.package_id and m.package_id == ref.package_id)
                     or (ref.workshop_id and m.workshop_id == ref.workshop_id)), None)

    def owned_dlcs(self): return {"ludeon.rimworld.royalty"}
    def activate(self, uids, index=None): self.active += [u for u in uids if u not in self.active]
    def resolve_dependencies(self): return [], [], []
    def list_modlists(self): return list(self.lists.values())

    def unique_modlist_name(self, name):
        base = out = name.strip() or "Modlist"
        n = 2
        while out.casefold() in {k.casefold() for k in self.lists}:
            out, n = f"{base} ({n})", n + 1
        return out

    def save_modlist(self, ml, overwrite=False):   # like Manager: never clobbers a list with a different name
        if not overwrite and any(k != ml.name and k.casefold() == ml.name.casefold() for k in self.lists):
            ml.name = self.unique_modlist_name(ml.name)
        self.lists[ml.name] = ml
    def update_db(self, progress=None, cancel=None, force=False): return "ok"

    def import_text(self, text, progress=None):      # workshop id 77 is a collection of Harmony + Alpha
        wid = text.rsplit("=", 1)[-1]
        if wid == "77":
            return ModList("Pack", [ModRef(workshop_id="2009463077"), ModRef(workshop_id="1111")], source="import:collection")
        if wid == "78":                              # a collection Steam would not expand (offline, private)
            return ModList("Imported list", [ModRef(workshop_id="78")], source="import:text")
        return ModList("Imported list", [ModRef(workshop_id=wid)], source="import:text")

    def missing_refs(self, refs): return [r for r in refs if not self.match_ref(r)]


def _selftest() -> None:
    assert strip_bbcode("[h1]Title[/h1][b]bold[/b] [url=https://x]link[/url][img]a.png[/img] [1.6] [WIP]") == \
        "Title\nbold link (https://x) [1.6] [WIP]"
    assert strip_bbcode("Intro[list][*]One[*]Two[/list]End") == "Intro\n\n• One\n• Two\nEnd"
    assert fmt_versions(["1.10", "1.4", "1.6", "1.5", "1.6"]) == "…1.5, 1.6, 1.10"
    mgr = _SmokeMgr()
    try:
        root = tk.Tk()
    except tk.TclError:  # no display: helpers are tested, skip widgets
        return
    root.withdraw()
    try:
        app = _SmokeApp(root, mgr)
        tab = WorkshopTab(root, app)
        tab._on_map()
        tab.tree.selection_set(tab.tree.get_children()[0])
        tab.update()
        tab._download()                          # startup scan still running: nothing is queued (R2-02)
        assert not app.queued and "Still loading" in app.toasts[-1], app.toasts
        app.emit("scanned")
        assert len(tab._rows) == 2, tab._rows
        tab.q_var.set("alp")
        tab.search()
        assert list(tab._rows) == ["1111"] and tab.tree.set("1111", "inst") == ""
        tab._more = True                         # a failed "Load more" leaves the button usable for a retry
        tab._on_fetch_error(tab._gen, OSError("database is locked"))
        assert tab.more_btn.instate(["!disabled"]) and "1 result" in tab.count_lbl.cget("text") \
            and "load failed" in tab.count_lbl.cget("text"), tab.count_lbl.cget("text")
        tab._more = False
        tab.tree.selection_set("1111")
        tab.update()
        assert tab.deps.size() == 2, tab.deps.get(0, "end")
        tab._download()                          # Manager.download adds the dependencies (with_deps default)
        assert app.queued[-1] == ([ModRef("a.b", "Alpha", "1111")], None, {}), app.queued[-1]
        assert "Queued 1 mod + missing dependencies" in app.toasts[-1], app.toasts[-1]
        tab._add_to_modlist("Test")
        assert [r.workshop_id for r in mgr.lists["Test"].mods] == ["1111"]
        mgr.lists["Favs"] = ModList("Favs", [ModRef("brrainz.harmony", "Harmony", "2009463077")])
        tab._add_to_modlist("favs")              # case clash with another list: saved as a new list, reported by name
        assert len(mgr.lists["Favs"].mods) == 1 and "favs (2)" in mgr.lists and "“favs (2)”" in app.toasts[-1]
        app.emit("workshop_pick", {"kind": "collection", "id": "77", "title": "My pack"})   # browser clicks
        assert app.queued[-1] == ([ModRef(workshop_id="1111")], None, {}), app.queued[-1]
        assert "Queued 1 mod from “Pack” (1 already installed)" in app.toasts[-1], app.toasts[-1]
        n = len(app.queued)
        app.emit("workshop_pick", {"kind": "item", "id": "2009463077", "title": "Harmony"})
        assert len(app.queued) == n and app.toasts[-1] == "Already installed: Harmony", app.toasts[-1]
        app.emit("workshop_pick", {"kind": "collection", "id": "78", "title": "Private pack"})
        assert len(app.queued) == n and "Could not read the collection “Private pack”" in app.toasts[-1], app.toasts[-1]
        tab.q_var.set("zzz")
        tab.search()
        assert not tab._rows and tab.empty_lbl.winfo_manager() == "place"
    finally:
        root.destroy()


if __name__ == "__main__":
    _selftest()
    print("workshop_tab selftest ok")
