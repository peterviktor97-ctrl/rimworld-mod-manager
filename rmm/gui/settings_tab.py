"""Settings tab: paths (+ auto-detect), extra mod folders, AI provider, Steam key, downloads, sorting, theme,
database, launch options. Save persists and emits 'settings_changed' (the App re-themes and rescans on it)."""
from __future__ import annotations

import glob
import os
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk

PATH_FIELDS = [  # (settings key, label, what it is)
    ("game_dir", "Game folder", "has RimWorldWin64.exe"),
    ("config_dir", "Config folder", "RimWorld by Ludeon Studios\\Config (has ModsConfig.xml)"),
    ("workshop_dir", "Workshop folder", "steamapps\\workshop\\content\\294100"),
    ("steamcmd_dir", "SteamCMD folder", "SteamCMD is downloaded here automatically when missing"),
]
METHOD_LABELS = {"steamcmd": "SteamCMD (anonymous or your account)", "steamworks": "Steam subscribe (Steamworks)",
                 "steam_client": "Steam client pages", "browser": "Browser pages"}
INSTALL_LABELS = {"move": "Move into Mods", "copy": "Copy into Mods", "link": "Link (junction)"}
TIEBREAK_LABELS = {"current": "Keep current order", "alphabetical": "Alphabetical"}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
PROVIDERS = ["antigravity", "anthropic", "openai_compat", "claude_code", "offline"]
STEAM_KEY_URL = "https://steamcommunity.com/dev/apikey"
TEST_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
               "additionalProperties": False}
_STR_KEYS = ("game_dir", "config_dir", "workshop_dir", "steamcmd_dir", "steamcmd_user", "ai_preset", "ai_provider", "ai_base_url",
             "ai_model", "ai_effort", "launch_args", "theme")
_BOOL_KEYS = ("ai_live_verify", "ai_auto_count", "ai_no_lazy", "launch_via_steam", "steamcmd_fallback_steamworks",
              "check_updates_on_start")
_INT_KEYS = {"steamcmd_retries": (0, 10, 3), "browser_batch": (1, 50, 10), "db_auto_update_days": (0, 365, 3)}
_NO_WHEEL = {"Listbox", "Text", "TCombobox", "TSpinbox", "Treeview"}


def find_steamcmd_dir() -> str:
    """An existing SteamCMD install (ours, RimSort's, common folders) or our default folder."""
    from rmm import config
    default = config.default_steamcmd_dir()
    cands = [default]
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cands += sorted(glob.glob(os.path.join(local, "RimSort", "instances", "*", "steamcmd")))
    cands += [r"C:\steamcmd", os.path.expanduser(r"~\steamcmd"), r"C:\Program Files\steamcmd",
              r"C:\Program Files (x86)\steamcmd"]
    for d in cands:
        if os.path.isfile(os.path.join(d, "steamcmd.exe")):
            return os.path.normpath(d)
    return os.path.normpath(default)


def detect_all(game_dir: str = "") -> dict:
    """Fresh auto-detection (ignores saved settings). Runs on a worker thread."""
    from rmm import paths
    found = paths.find_game_dir() or ""
    gd = game_dir if game_dir and paths.is_game_dir(game_dir) else found
    from rmm import config
    return {
        "game_dir": found,
        "config_dir": paths.find_config_dir() or "",
        "workshop_dir": paths.find_workshop_dir(gd or None) or "",
        "steamcmd_dir": find_steamcmd_dir(),
        "steamcmd_default": os.path.normpath(config.default_steamcmd_dir()),
        "steam": paths.find_steam() or "",
        "version": paths.game_version(gd) if gd else "",
    }


def _same_path(a: str, b: str) -> bool:
    return bool(a and b) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _clamp_int(text, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(float(str(text).strip()))))
    except (TypeError, ValueError):
        return default


class SettingsTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=(8, 8, 0, 0))
        from rmm import ai, config
        self.app = app
        self.mgr = app.mgr
        self.presets: dict[str, dict] = ai.PRESETS
        self.antigravity_models: list[str] = list(ai.ANTIGRAVITY_MODELS)
        self.claude_models: list[str] = list(ai.CLAUDE_MODELS)
        self._fetched: dict[str, list[str]] = {}   # preset -> fetched model ids
        self._detected: dict = {}
        self._steamcmd_default = config.default_steamcmd_dir()
        self._loading = False
        self._dirty = False
        self._saving = False
        self._save_running = False   # a Save is writing on a worker thread (Ctrl+S ignores the disabled button)
        self._login_job = None       # cancel Event of the running SteamCMD login (one console at a time)
        self._provider = ""

        self.v: dict[str, tk.Variable] = {k: tk.StringVar() for k in _STR_KEYS}
        self.v.update({k: tk.BooleanVar() for k in _BOOL_KEYS})
        self.v.update({k: tk.StringVar() for k in _INT_KEYS})
        for k in ("download_method", "install_mode", "sort_tiebreak"):
            self.v[k] = tk.StringVar()
        self.api_key = tk.StringVar()
        self.steam_key = tk.StringVar()
        self.supporter_key_var = tk.StringVar()

        self._build()
        self._load()
        # theme applies (and saves) instantly, so it never makes the page dirty
        for var in (*(v for k, v in self.v.items() if k != "theme"), self.api_key, self.steam_key):
            var.trace_add("write", lambda *a: self._set_dirty(True))
        self._apply_colors()

        app.on("settings_changed", lambda *a: self._external_change())
        app.on("scanned", lambda *a: self._refresh_status())
        app.on("db_updated", lambda *a: self._refresh_status())
        app.on("theme_changed", lambda *a: self.after_idle(self._apply_colors))
        self.after(200, self._detect_labels)
        self.after(250, self._refresh_status)

    # ------------------------------------------------------------------ layout helpers
    def _section(self, parent, title: str, subtitle: str = "") -> ttk.Frame:
        f = ttk.Frame(parent, style="Card.TFrame", padding=14)
        f.pack(fill="x", pady=(0, 14))
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text=title, style="PanelHeading.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        if subtitle:
            ttk.Label(f, text=subtitle, style="PanelMuted.TLabel", wraplength=460, justify="left").grid(
                row=1, column=0, columnspan=4, sticky="w", pady=(2, 0))
        ttk.Separator(f).grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 8))
        return f

    @staticmethod
    def _row(sec, label: str, widget, *extra, sticky: str = "ew", hint: str = "") -> ttk.Label | None:
        r = sec.grid_size()[1]
        if label:
            ttk.Label(sec, text=label, style="Panel.TLabel").grid(row=r, column=0, sticky="w", padx=(0, 12), pady=3)
        widget.grid(row=r, column=1, sticky=sticky, pady=3)
        for i, w in enumerate(extra):
            w.grid(row=r, column=2 + i, sticky="w", padx=(6, 0), pady=3)
        if not hint:
            return None
        lbl = ttk.Label(sec, text=hint, style="PanelMuted.TLabel")
        lbl.grid(row=r + 1, column=1, columnspan=3, sticky="w", pady=(0, 4))
        return lbl

    def _combo(self, parent, key: str, values, width: int = 28, readonly: bool = True) -> ttk.Combobox:
        return ttk.Combobox(parent, textvariable=self.v[key], values=list(values), width=width,
                            state="readonly" if readonly else "normal")

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        top = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        top.pack(fill="x", padx=(0, 8), pady=(0, 10))
        ttk.Label(top, text="Settings", style="PanelHeading.TLabel").pack(side="left")
        self.save_btn = ttk.Button(top, text="Save", style="Accent.TButton", command=self.save)
        self.save_btn.pack(side="right")
        ttk.Button(top, text="Revert", command=self._load).pack(side="right", padx=6)
        self.dirty_label = ttk.Label(top, text="", style="PanelMuted.TLabel")
        self.dirty_label.pack(side="right", padx=6)

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(self.canvas, padding=(0, 0, 16, 16))
        win = self.canvas.create_window(0, 0, window=body, anchor="nw")
        body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(win, width=e.width))
        body.columnconfigure(0, weight=11, uniform="col")   # paths need a bit more room than the AI fields
        body.columnconfigure(1, weight=9, uniform="col")

        status = ttk.Frame(body)
        status.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 14))
        self.st_game = ttk.Label(status, text="Game: detecting…")
        self.st_game.pack(anchor="w")
        self.st_db = ttk.Label(status, text="Mod database: …", style="Muted.TLabel")
        self.st_db.pack(anchor="w")

        left = ttk.Frame(body)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 24))
        right = ttk.Frame(body)
        right.grid(row=1, column=1, sticky="nsew")
        self._build_paths(left)
        self._build_extra(left)
        self._build_launch(left)
        self._build_supporter(left)
        self._build_about(left)
        self._build_ai(right)
        self._build_steam(right)
        self._build_downloads(right)
        self._build_misc(right)
        self._bind_wheel(body)

    def _bind_wheel(self, widget) -> None:
        """Mouse wheel over the settings page scrolls it (except widgets that scroll themselves)."""
        if widget.winfo_class() not in _NO_WHEEL:
            widget.bind("<MouseWheel>", self._wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _wheel(self, event) -> None:
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), "units")

    def _build_paths(self, parent) -> None:
        sec = self._section(parent, "Game & folders", "Leave a field empty to auto-detect it at every start.")
        head = sec.grid_slaves(row=0, column=0)[0]
        head.grid_configure(columnspan=2)
        ttk.Button(sec, text="Auto-detect all", command=self._autodetect_all).grid(row=0, column=2, columnspan=2,
                                                                                  sticky="e")
        self.detect_labels: dict[str, ttk.Label] = {}
        for key, label, what in PATH_FIELDS:
            entry = ttk.Entry(sec, textvariable=self.v[key])
            browse = ttk.Button(sec, text="Browse…", command=lambda k=key: self._browse(k))
            auto = ttk.Button(sec, text="Detect", command=lambda k=key: self._autodetect(k))
            self.detect_labels[key] = self._row(sec, label, entry, browse, auto, hint=what)
            self.detect_labels[key].configure(wraplength=420, justify="left")

    def _build_extra(self, parent) -> None:
        sec = self._section(parent, "Extra mod folders", "Additional folders that contain mod folders "
                                                         "(e.g. a second library or a dev folder).")
        box = ttk.Frame(sec)
        self.extra = tk.Listbox(box, height=4, activestyle="none", relief="flat", borderwidth=0,
                                highlightthickness=1, selectmode="extended")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.extra.yview)
        self.extra.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.extra.pack(side="left", fill="both", expand=True)
        btns = ttk.Frame(sec)
        ttk.Button(btns, text="Add…", command=self._add_extra).pack(fill="x")
        ttk.Button(btns, text="Remove", command=self._remove_extra).pack(fill="x", pady=(4, 0))
        r = sec.grid_size()[1]
        box.grid(row=r, column=0, columnspan=2, sticky="ew")
        btns.grid(row=r, column=2, sticky="n", padx=(6, 0))

    def _build_launch(self, parent) -> None:
        sec = self._section(parent, "Launch")
        self._row(sec, "", ttk.Checkbutton(sec, text="Launch through Steam (overlay, playtime, cloud saves)",
                                           variable=self.v["launch_via_steam"]), sticky="w")
        self._row(sec, "Arguments", ttk.Entry(sec, textvariable=self.v["launch_args"]),
                  hint="e.g. -popupwindow -logfile \"C:\\rw.log\"  (used when not launching through Steam)")

    def _build_supporter(self, parent) -> None:
        from rmm import supporter
        sec = self._section(
            parent,
            "Supporter Edition",
            "Supporting on Itch.io or Ko-fi ($5+) gives you an RMM1-... key that unlocks 4 extra dark themes "
            "(Archotech Gold, Glitterworld, Blood Moon, Void OLED) and a supporter badge.",
        )
        self.supporter_status_lbl = ttk.Label(sec, text="", style="Panel.TLabel", wraplength=420, justify="left")
        self._row(sec, "Status", self.supporter_status_lbl,
                  ttk.Button(sec, text="Get a Key", command=lambda: supporter.show_support_dialog(self.app)),
                  sticky="w")
        self.supporter_entry = ttk.Entry(sec, textvariable=self.supporter_key_var)
        self._row(sec, "Supporter Key", self.supporter_entry,
                  ttk.Button(sec, text="Activate", style="Accent.TButton", command=self._activate_supporter_key),
                  hint="Verified offline and saved with Windows DPAPI")

    def _activate_supporter_key(self) -> None:
        from rmm import supporter
        raw = self.supporter_key_var.get().strip()
        if not raw:
            supporter.show_support_dialog(self.app)
            return
        lic = supporter.activate_supporter_key(self.mgr.settings, raw)
        if not lic.valid:
            self.app.error("Supporter Key", f"Could not activate key:\n\n{lic.error}")
            self._refresh_supporter_ui()
            return
        self._refresh_supporter_ui()
        if hasattr(self.app, "refresh_supporter_badge"):
            self.app.refresh_supporter_badge()
        self.app.toast(f"★ Supporter Edition unlocked! Welcome, {lic.holder} ({lic.tier_name})", level="ok")

    def _refresh_supporter_ui(self) -> None:
        from rmm import supporter
        lic = supporter.get_supporter_license(self.mgr.settings)
        c = getattr(self.app, "colors", None) or {}
        if lic.valid:
            self.supporter_status_lbl.configure(
                text=f"★ {lic.tier_name}  ·  Licensed to {lic.holder} (Key #{lic.key_id:04d})",
                foreground=c.get("ok", "#22c55e"),
            )
        else:
            self.supporter_status_lbl.configure(
                text="Free Edition (all core mod manager & AI features included)",
                foreground=c.get("muted", "#8b95a7"),
            )

    def _build_about(self, parent) -> None:
        from rmm import config
        from rmm.models import APP_NAME, APP_VERSION
        sec = self._section(parent, "App")
        self._row(sec, "Version", ttk.Label(sec, text=f"{APP_NAME} {APP_VERSION}"), sticky="w")
        self._row(sec, "Data folder", ttk.Label(sec, text=config.APP_DIR, style="Muted.TLabel"),
                  ttk.Button(sec, text="Open", command=lambda: self._open(config.APP_DIR)), sticky="w")
        self._row(sec, "Log file", ttk.Label(sec, text="logs\\app.log", style="Muted.TLabel"),
                  ttk.Button(sec, text="Open", command=lambda: self._open(os.path.join(config.APP_DIR, "logs", "app.log"))),
                  sticky="w")

    def _build_ai(self, parent) -> None:
        sec = self._section(parent, "AI modlist generator",
                            "The AI only picks from mods found in the database; every pick is verified, so it cannot "
                            "invent mods or package ids. 'Offline' works without any AI.")
        preset = self._combo(sec, "ai_preset", self.presets, width=30)
        preset.bind("<<ComboboxSelected>>", lambda e: self._preset_changed())
        self.key_link = ttk.Button(sec, text="Get a key", command=self._get_ai_key)
        self._row(sec, "Preset", preset, self.key_link, sticky="w")
        prov = self._combo(sec, "ai_provider", PROVIDERS, width=16)
        prov.bind("<<ComboboxSelected>>", lambda e: self._provider_changed())
        self._row(sec, "Provider", prov, sticky="w")
        self.base_entry = ttk.Entry(sec, textvariable=self.v["ai_base_url"])
        self._row(sec, "Base URL", self.base_entry)
        self.model_cb = self._combo(sec, "ai_model", [], readonly=False)
        self.fetch_btn = ttk.Button(sec, text="Fetch models", command=self._fetch_models)
        self._row(sec, "Model", self.model_cb, self.fetch_btn)
        self.key_entry = ttk.Entry(sec, textvariable=self.api_key, show="•")
        self.show_key = tk.BooleanVar(value=False)
        self._row(sec, "API key", self.key_entry,
                  ttk.Checkbutton(sec, text="Show", variable=self.show_key,
                                  command=lambda: self.key_entry.configure(show="" if self.show_key.get() else "•")))
        self.effort_cb = self._combo(sec, "ai_effort", EFFORTS, width=10)
        self._row(sec, "Effort", self.effort_cb, sticky="w",
                  hint="Antigravity & Claude: higher = smarter picks, slower")
        self._row(sec, "", ttk.Checkbutton(sec, text="Let AI decide the mod count needed for each prompt",
                                           variable=self.v["ai_auto_count"]), sticky="w")
        self._row(sec, "", ttk.Checkbutton(sec, text="No being lazy — tell the AI to try its best and deliver a full modlist",
                                           variable=self.v["ai_no_lazy"]), sticky="w")
        self._row(sec, "", ttk.Checkbutton(sec, text="AI uses the live Steam Workshop: finds mods the database lacks, checks picks",
                                           variable=self.v["ai_live_verify"]), sticky="w")
        self.test_btn = ttk.Button(sec, text="Test connection", command=self._test_connection)
        self.test_result = ttk.Label(sec, text="", style="Muted.TLabel", wraplength=340, justify="left")
        self._row(sec, "", self.test_btn, self.test_result, sticky="w")
        ttk.Label(sec, text="Keys are encrypted with your Windows account (DPAPI).", style="Muted.TLabel").grid(
            row=sec.grid_size()[1], column=1, columnspan=3, sticky="w")

    def _build_steam(self, parent) -> None:
        sec = self._section(parent, "Steam Web API key (optional)",
                            "Optional. With a key, the AI generator also searches the live Steam Workshop (mods "
                            "the database lacks, whole author pages), and workshop search uses Steam's API "
                            "instead of reading the workshop web pages.")
        self.steam_entry = ttk.Entry(sec, textvariable=self.steam_key, show="•")
        self.show_steam = tk.BooleanVar(value=False)
        self._row(sec, "Key", self.steam_entry,
                  ttk.Checkbutton(sec, text="Show", variable=self.show_steam,
                                  command=lambda: self.steam_entry.configure(show="" if self.show_steam.get() else "•")),
                  ttk.Button(sec, text="Get a key", command=lambda: webbrowser.open(STEAM_KEY_URL)))

    def _build_downloads(self, parent) -> None:
        sec = self._section(parent, "Downloads")
        self._row(sec, "Method", self._combo(sec, "download_method", METHOD_LABELS.values(), width=34), sticky="w")
        self._row(sec, "SteamCMD install", self._combo(sec, "install_mode", INSTALL_LABELS.values(), width=18), sticky="w",
                  hint="how SteamCMD downloads get into the game's Mods folder")
        self._row(sec, "SteamCMD account", ttk.Entry(sec, textvariable=self.v["steamcmd_user"], width=24),
                  ttk.Button(sec, text="Log in…", command=self._steamcmd_login), sticky="w",
                  hint="optional; empty = anonymous. Your password goes into SteamCMD's own window, never this app")
        self._row(sec, "SteamCMD retries", ttk.Spinbox(sec, from_=0, to=10, width=6,
                                                        textvariable=self.v["steamcmd_retries"]), sticky="w")
        self._row(sec, "", ttk.Checkbutton(sec, text="If SteamCMD fails, subscribe the rest through Steam",
                                           variable=self.v["steamcmd_fallback_steamworks"]), sticky="w",
                  hint="needs the Steam client running; those mods go to Steam's workshop folder")
        self._row(sec, "Browser batch", ttk.Spinbox(sec, from_=1, to=50, width=6, textvariable=self.v["browser_batch"]),
                  sticky="w", hint="pages opened at once by the browser method")

    def _steamcmd_login(self) -> None:
        user = self.v["steamcmd_user"].get().strip()
        if not user:
            self.app.error("SteamCMD login", "Enter your Steam account name (not the profile name) first.")
            return
        if not _same_path(self.v["steamcmd_dir"].get().strip() or self._steamcmd_default,
                          self.mgr.settings.get("steamcmd_dir")):
            self.app.error("SteamCMD login", "Save settings first: the SteamCMD folder changed, "
                                              "and the login is stored in that folder.")
            return
        if self._login_job is not None:
            self.app.toast("A SteamCMD login window is already open.", "warn")
            return
        def done(_r):
            self._login_job = None
            s = self.mgr.settings           # saved right away: the account is useless if Save is forgotten
            s.set("steamcmd_user", user)
            s.save()
            self.app.toast("SteamCMD window closed. If it said 'Logged in OK', downloads now use this account.", "ok")

        self.app.toast("A SteamCMD window opens: type your password, then the Steam Guard code, then close it.")
        def failed(e):
            self._login_job = None
            if isinstance(e, InterruptedError):
                self.app.toast("SteamCMD login cancelled", "warn")
            else:
                self.app.error("SteamCMD login failed", str(e))

        self._login_job = self.app.run_bg(self.mgr.steamcmd_login, user, progress=True, cancellable=True,
                                          busy="SteamCMD login…", on_done=done, on_error=failed)

    def _build_misc(self, parent) -> None:
        from rmm.gui.theme import THEME_LABELS
        sec = self._section(parent, "Sorting, look & database")
        self._row(sec, "Sort ties", self._combo(sec, "sort_tiebreak", TIEBREAK_LABELS.values(), width=20), sticky="w",
                  hint="order of mods that no rule orders")
        themes = ttk.Frame(sec)
        row1 = ttk.Frame(themes)
        row1.pack(anchor="w")
        row2 = ttk.Frame(themes)
        row2.pack(anchor="w", pady=(4, 0))
        for i, (val, text) in enumerate(THEME_LABELS.items()):
            rframe = row1 if i < 2 else row2
            ttk.Radiobutton(rframe, text=text, value=val, variable=self.v["theme"],
                            command=self._theme_changed).pack(side="left", padx=(0, 10))
        self._row(sec, "Theme", themes, sticky="w",
                  hint="★ themes unlock with a Supporter Key")
        days = ttk.Frame(sec)
        ttk.Spinbox(days, from_=0, to=365, width=6, textvariable=self.v["db_auto_update_days"]).pack(side="left")
        ttk.Label(days, text="days  (0 = never)", style="Muted.TLabel").pack(side="left", padx=(8, 0))
        self._row(sec, "DB auto-update", days, sticky="w")
        self._row(sec, "", ttk.Checkbutton(sec, text="Check for mod updates at startup",
                                           variable=self.v["check_updates_on_start"]), sticky="w",
                  hint="results appear in the Downloads tab")

    def _apply_colors(self) -> None:
        c = getattr(self.app, "colors", None) or {}
        self.canvas.configure(background=c.get("bg", "#1e1f22"))
        self.extra.configure(background=c.get("field", "#232428"), foreground=c.get("text", "#e6e6e6"),
                             selectbackground=c.get("select", "#35507a"), selectforeground=c.get("text", "#e6e6e6"),
                             highlightbackground=c.get("border", "#3a3c42"), highlightcolor=c.get("accent", "#5b8def"))
        self._set_dirty(self._dirty)

    # ------------------------------------------------------------------ load / save
    def _load(self) -> None:
        s = self.mgr.settings
        self._loading = True
        try:
            for k in _STR_KEYS:
                self.v[k].set(str(s.get(k) or ""))
            if _same_path(self.v["steamcmd_dir"].get(), self._steamcmd_default):
                self.v["steamcmd_dir"].set("")   # Settings.get fills in the default; empty keeps it portable
            for k in _BOOL_KEYS:
                self.v[k].set(bool(s.get(k)))
            for k, (lo, hi, d) in _INT_KEYS.items():
                self.v[k].set(str(_clamp_int(s.get(k, d), lo, hi, d)))
            for key, labels in (("download_method", METHOD_LABELS), ("install_mode", INSTALL_LABELS),
                                ("sort_tiebreak", TIEBREAK_LABELS)):
                val = s.get(key)
                self.v[key].set(labels.get(val, next(iter(labels.values()))))
            self.api_key.set(s.get_secret("ai_api_key") or "")
            self.steam_key.set(s.get_secret("steam_api_key") or "")
            self.supporter_key_var.set(s.get_secret("supporter_key") or "")
            self._refresh_supporter_ui()
            self.extra.delete(0, "end")
            for d in s.get("extra_mod_dirs") or []:
                self.extra.insert("end", d)
            self._update_ai_state()
        finally:
            self._loading = False
        self._set_dirty(False)

    def _set_dirty(self, dirty: bool) -> None:
        if self._loading:
            return
        self._dirty = dirty
        c = getattr(self.app, "colors", None) or {}
        self.dirty_label.configure(text="● Unsaved changes" if dirty else "",
                                   foreground=c.get("warn", "#e0a526"))

    def _collect(self) -> dict:
        v = self.v
        out = {k: v[k].get().strip() for k in _STR_KEYS}
        for k, _l, _h in PATH_FIELDS:
            if out[k]:
                out[k] = os.path.normpath(out[k])
        out.update({k: bool(v[k].get()) for k in _BOOL_KEYS})
        out.update({k: _clamp_int(v[k].get(), lo, hi, d) for k, (lo, hi, d) in _INT_KEYS.items()})
        for key, labels in (("download_method", METHOD_LABELS), ("install_mode", INSTALL_LABELS),
                            ("sort_tiebreak", TIEBREAK_LABELS)):
            inv = {lab: val for val, lab in labels.items()}
            out[key] = inv.get(v[key].get(), next(iter(labels)))
        out["extra_mod_dirs"] = [os.path.normpath(d) for d in self.extra.get(0, "end")]
        if out["ai_effort"] not in EFFORTS:
            out["ai_effort"] = "medium"
        from rmm import supporter
        from rmm.gui.theme import PALETTES
        if out["theme"] not in PALETTES or not supporter.is_theme_unlocked(out["theme"], self.mgr.settings):
            out["theme"] = "dark"
        return out

    def is_dirty(self) -> bool:
        return self._dirty

    def store(self) -> dict:
        """Put the page's values into the Settings object (the caller writes it to disk)."""
        s = self.mgr.settings
        vals = self._collect()
        for k, val in vals.items():
            s.set(k, val)
        key = self.api_key.get().strip()
        s.set_secret("ai_api_key", key)
        s.set_secret(f"ai_api_key.{vals['ai_preset']}", key)
        s.set_secret("steam_api_key", self.steam_key.get().strip())
        return vals

    def save(self) -> None:
        if self._save_running:
            return
        if self.v["launch_args"].get().count('"') % 2:    # launch_game would refuse them anyway
            self.app.error("Settings", "Launch arguments have an unbalanced quote (odd number of \").\n"
                                       "Fix them before saving.")
            return
        s = self.mgr.settings
        vals = self.store()
        self._save_running = True
        self._set_dirty(False)   # edits made while the write runs set it again and are kept below
        self.save_btn.configure(state="disabled")

        def work():
            s.save()
            return self._validate(vals)

        def done(warnings):
            self._save_running = False
            self.save_btn.configure(state="normal")
            if not self._dirty:
                self._load()   # show normalized values
            self._emit_changed()   # App re-themes and, when a path changed, re-detects + rescans
            if warnings:
                self.app.toast("Saved. Check: " + " ".join(warnings), level="warning")
            else:
                self.app.toast("Settings saved", level="ok")

        def failed(exc):
            self._save_running = False
            self._set_dirty(True)
            self.save_btn.configure(state="normal")
            self.app.error("Settings", f"Could not save settings:\n{exc}")

        self.app.run_bg(work, on_done=done, on_error=failed, busy="Saving settings…")

    save_shortcut = save      # Ctrl+S on this tab (App._key_save)

    @staticmethod
    def _validate(vals: dict) -> list[str]:
        from rmm import paths
        warn = []
        if vals["game_dir"] and not paths.is_game_dir(vals["game_dir"]):
            warn.append("The game folder doesn't look like a RimWorld install.")
        if vals["config_dir"] and not os.path.isfile(os.path.join(vals["config_dir"], "ModsConfig.xml")):
            warn.append("The config folder has no ModsConfig.xml (start the game once).")
        if vals["workshop_dir"] and not os.path.isdir(vals["workshop_dir"]):
            warn.append("The workshop folder does not exist.")
        missing = [d for d in vals["extra_mod_dirs"] if not os.path.isdir(d)]
        if missing:
            warn.append(f"Missing extra folder(s): {', '.join(missing)}.")
        return warn

    def _external_change(self) -> None:
        """Another tab changed settings (e.g. download method): sync without losing unsaved edits here."""
        if self._saving:
            return
        if not self._dirty:
            self._load()
            return
        s = self.mgr.settings
        self._loading = True
        try:
            self.v["download_method"].set(METHOD_LABELS.get(s.get("download_method"), METHOD_LABELS["steamcmd"]))
            self.v["install_mode"].set(INSTALL_LABELS.get(s.get("install_mode"), INSTALL_LABELS["move"]))
        finally:
            self._loading = False

    def _theme_changed(self) -> None:
        from rmm import supporter
        s = self.mgr.settings
        theme = self.v["theme"].get()
        if not supporter.is_theme_unlocked(theme, s):
            self.v["theme"].set(s.get("theme") if supporter.is_theme_unlocked(s.get("theme"), s) else "dark")
            self.app.toast("★ Supporter theme — enter a Supporter Key to unlock!", level="warn")
            supporter.show_support_dialog(self.app)
            return
        if s.get("theme") == theme:
            return
        s.set("theme", theme)
        self._emit_changed()     # App.set_theme runs on 'settings_changed' and then emits 'theme_changed'
        self.app.run_bg(s.save)

    def _emit_changed(self) -> None:
        self._saving = True
        try:
            self.app.emit("settings_changed")
        finally:
            self._saving = False

    # ------------------------------------------------------------------ paths
    def _browse(self, key: str) -> None:
        cur = self.v[key].get().strip() or self._detected.get(key, "")
        d = filedialog.askdirectory(parent=self, initialdir=cur if os.path.isdir(cur) else os.path.expanduser("~"),
                                    title=f"Choose {dict((k, l) for k, l, _h in PATH_FIELDS)[key].lower()}")
        if d:
            self.v[key].set(os.path.normpath(d))

    def _autodetect(self, key: str) -> None:
        self._run_detect(lambda det: self._fill_detected([key], det))

    def _autodetect_all(self) -> None:
        self._run_detect(lambda det: self._fill_detected([k for k, _l, _h in PATH_FIELDS], det))

    def _fill_detected(self, keys: list[str], det: dict) -> None:
        missing = []
        for k in keys:
            if det.get(k):
                self.v[k].set(det[k])
            else:
                missing.append(dict((kk, l) for kk, l, _h in PATH_FIELDS)[k])
        if missing:
            self.app.toast(f"Not found: {', '.join(missing)}. Use Browse…", level="warning")
        else:
            self.app.toast("Detected. Press Save to keep it")

    def _run_detect(self, then) -> None:
        def done(det):
            self._show_detected(det)
            then(det)
        self.app.run_bg(detect_all, self.v["game_dir"].get().strip(), on_done=done, busy="Detecting folders…")

    def _detect_labels(self) -> None:
        self.app.run_bg(detect_all, self.v["game_dir"].get().strip(), on_done=self._show_detected,
                        on_error=lambda e: None)

    def _show_detected(self, det: dict) -> None:
        self._detected = det
        for key, _label, what in PATH_FIELDS:
            val = det.get(key, "")
            text = f"Detected: {val}" if val else f"Not detected ({what})"
            if key == "game_dir" and det.get("version"):
                text += f"  ·  RimWorld {det['version']}"
            if key == "steamcmd_dir":
                default = det.get("steamcmd_default", "")
                text = f"Empty = {default}"
                if val and not _same_path(val, default):
                    text += f"\nExisting SteamCMD found: {val} (press Detect to reuse it)"
            self.detect_labels[key].configure(text=text)

    def _add_extra(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Add a folder that contains mods")
        if d and os.path.normpath(d) not in self.extra.get(0, "end"):
            self.extra.insert("end", os.path.normpath(d))
            self._set_dirty(True)

    def _remove_extra(self) -> None:
        for i in reversed(self.extra.curselection()):
            self.extra.delete(i)
            self._set_dirty(True)

    # ------------------------------------------------------------------ AI
    def _preset(self) -> dict:
        return self.presets.get(self.v["ai_preset"].get(), {})

    def _preset_changed(self) -> None:
        name = self.v["ai_preset"].get()
        p = self._preset()
        if not p:
            return
        self.v["ai_provider"].set(p.get("provider", "openai_compat"))
        self.v["ai_base_url"].set(p.get("base_url", ""))
        self.v["ai_model"].set(p.get("model", ""))   # another provider's model name would fail here
        s = self.mgr.settings
        key = s.get_secret(f"ai_api_key.{name}")
        if not key and name == s.get("ai_preset"):
            key = s.get_secret("ai_api_key")
        self.api_key.set(key or "")
        self.test_result.configure(text="")
        self._update_ai_state()

    def _provider_changed(self) -> None:
        """A key typed for one provider must never be sent to another one's host."""
        if self.v["ai_provider"].get() != self._provider:
            self.api_key.set("")
            self.test_result.configure(text="")
        self._update_ai_state()

    def _update_ai_state(self) -> None:
        provider = self._provider = self.v["ai_provider"].get()
        if provider != "openai_compat" and self.v["ai_base_url"].get():
            self.v["ai_base_url"].set("")     # greyed out = unused; a stale URL would get the Anthropic key
        p = self._preset()
        offline = provider == "offline"
        needs_key = not offline and p.get("needs_key", provider == "anthropic")
        self.base_entry.configure(state="normal" if provider == "openai_compat" else "disabled")
        self.model_cb.configure(state="disabled" if offline else "normal",
                                values=self.claude_models if provider == "anthropic"
                                else self._fetched.get(self.v["ai_preset"].get(), self.antigravity_models) if provider == "antigravity"
                                else ["fable", "opus", "sonnet", "haiku"] if provider == "claude_code"
                                else self._fetched.get(self.v["ai_preset"].get(), []))
        self.fetch_btn.configure(state="disabled" if offline else "normal")
        self.key_entry.configure(state="normal" if needs_key or self.api_key.get() else "disabled")
        self.effort_cb.configure(state="readonly" if provider in ("antigravity", "anthropic", "claude_code") else "disabled")
        self.test_btn.configure(state="disabled" if offline else "normal")
        self.key_link.configure(state="normal" if p.get("key_url") else "disabled",
                                text="Get a key" if p.get("needs_key") else "Website")

    def _llm_args(self, effort: str | None = None) -> dict:
        return {"provider": self.v["ai_provider"].get(), "model": self.v["ai_model"].get().strip(),
                "api_key": self.api_key.get().strip(), "base_url": self.v["ai_base_url"].get().strip(),
                "effort": effort or self.v["ai_effort"].get() or "medium"}

    @staticmethod
    def _ai_blocker(provider: str, needs_key: bool, key: str, base_url: str, cli=None) -> str:
        """What stops an AI call before it starts, or ''. cli: claude_code_exe / antigravity_exe stand-in for the self-test."""
        if provider == "antigravity":
            if cli is None:
                from rmm.ai import antigravity_exe as cli
            return "" if cli() else "Antigravity CLI not found: make sure Antigravity is installed"
        if provider == "claude_code":
            if cli is None:
                from rmm.ai import claude_code_exe as cli
            return "" if cli() else "Claude Code CLI not found: install it and run claude once to log in"
        if provider == "openai_compat" and not base_url:
            return "Enter a base URL first (e.g. http://localhost:11434/v1)"
        # the Anthropic SDK falls back to ANTHROPIC_API_KEY; OpenAI-compatible calls only send the typed key
        if needs_key and not key and not (provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY")):
            return "Enter an API key first"
        return ""

    def _check_ai(self, args: dict) -> bool:
        """Tk thread: show the blocker in the result label; True = ok to start the job."""
        p = self._preset()
        needs_key = args["provider"] != "offline" and p.get("needs_key", args["provider"] == "anthropic")
        msg = self._ai_blocker(args["provider"], needs_key, args["api_key"], args["base_url"])
        if msg:
            self.test_result.configure(text=msg, foreground=(getattr(self.app, "colors", None) or {}).get("warn", "#e0a526"))
        return not msg

    def _get_ai_key(self) -> None:
        url = self._preset().get("key_url")
        if url:
            webbrowser.open(url)

    def _fetch_models(self) -> None:
        args = self._llm_args()
        preset = self.v["ai_preset"].get()
        if not self._check_ai(args):
            return

        def work():
            from rmm import ai
            return ai.LLM(**args).list_models()

        def done(models):
            models = (list(dict.fromkeys(models or [])) if args["provider"] == "antigravity"
                      else sorted(set(models or [])))
            if not models:
                self.test_result.configure(text="No models returned. Check the key / base URL (is the server running?).")
                return
            self._fetched[preset] = models
            self.model_cb.configure(values=models)
            if not self.v["ai_model"].get().strip():
                self.v["ai_model"].set(models[0])
            self.test_result.configure(text=f"{len(models)} models available; pick one from the Model list.")

        self.test_result.configure(text="Fetching models…", foreground=(getattr(self.app, "colors", None) or {}).get("muted", "#9aa0a6"))
        self.app.run_bg(work, on_done=done, on_error=lambda e: self.test_result.configure(text=f"✖ {e}"),
                        busy="Fetching model list…")

    def _test_connection(self) -> None:
        args = self._llm_args(effort="low")
        c = getattr(self.app, "colors", None) or {}

        def work():
            from rmm import ai
            t0 = time.monotonic()
            out = ai.LLM(**args).complete_json(
                "You are a connection test for a RimWorld mod manager. Answer with JSON only.",
                'Reply with {"ok": true}.', TEST_SCHEMA, max_tokens=2048)
            return out, time.monotonic() - t0

        def done(res):
            out, secs = res
            self.test_btn.configure(state="normal")
            if isinstance(out, dict) and out.get("ok") is True:
                self.test_result.configure(text=f"✔ Connected: {args['model']} answered in {secs:.1f} s",
                                           foreground=c.get("ok", "#3ba55d"))
            else:
                self.test_result.configure(text=f"⚠ Connected, but got an unexpected reply: {str(out)[:120]}",
                                           foreground=c.get("warn", "#e0a526"))

        def failed(exc):
            self.test_btn.configure(state="normal")
            self.test_result.configure(text=f"✖ {exc}", foreground=c.get("err", "#e5484d"))

        if not self._check_ai(args):
            return
        if not args["model"]:
            self.test_result.configure(text="Pick a model first.", foreground=c.get("warn", "#e0a526"))
            return
        self.test_btn.configure(state="disabled")
        self.test_result.configure(text="Testing…", foreground=c.get("muted", "#9aa0a6"))
        self.app.run_bg(work, on_done=done, on_error=failed, busy="Testing AI connection…")

    # ------------------------------------------------------------------ status
    def _refresh_status(self) -> None:
        mgr = self.mgr

        def work():
            return mgr.db.status()

        gd, ver = mgr.game_dir, mgr.game_version
        steam = self._detected.get("steam", "")
        if gd:
            text = f"Game: RimWorld {ver or '(version unknown)'}  ·  {gd}"
        else:
            text = "Game: not found. Set the game folder below and press Save."
        if steam:
            text += f"  ·  Steam: {steam}"
        c = getattr(self.app, "colors", None) or {}
        self.st_game.configure(text=text, foreground="" if gd else c.get("err", "#e5484d"))   # "" = theme default
        self.app.run_bg(work, on_done=self._show_db, on_error=lambda e: self.st_db.configure(text=f"Mod database: {e}"))

    def _show_db(self, st: dict) -> None:
        n = int(st.get("steam_count") or 0)
        if not n:
            self.st_db.configure(text="Mod database: empty (it downloads automatically; see Tools › Database)")
            return
        upd = st.get("updated_at") or 0
        age = ""
        if upd:
            h = (time.time() - float(upd)) / 3600
            age = f" · updated {int(h / 24)} day(s) ago" if h >= 48 else f" · updated {max(0, int(h))} hour(s) ago"
        self.st_db.configure(text=f"Mod database: {n:,} mods · rules for {int(st.get('rules_count') or 0):,} mods{age}")

    def _open(self, path: str) -> None:
        if os.path.exists(path):
            os.startfile(path)
        else:
            self.app.toast("Not found: " + path, level="warning")


def _selftest() -> None:
    assert _clamp_int("7", 0, 10, 3) == 7 and _clamp_int("99", 0, 10, 3) == 10 and _clamp_int("x", 0, 10, 3) == 3
    assert _clamp_int("2.0", 1, 50, 10) == 2 and _clamp_int(None, 1, 50, 10) == 10
    assert _same_path("C:/A/b", "c:\\a\\B\\") and not _same_path("", "") and not _same_path("C:/a", "C:/b")
    assert set(METHOD_LABELS) == {"steamcmd", "steamworks", "steam_client", "browser"}
    assert set(INSTALL_LABELS) == {"move", "copy", "link"} and set(TIEBREAK_LABELS) == {"current", "alphabetical"}
    assert len(set(METHOD_LABELS.values())) == len(METHOD_LABELS)   # labels map back uniquely
    base = {"game_dir": "", "config_dir": "", "workshop_dir": "", "extra_mod_dirs": []}
    assert SettingsTab._validate(base) == []
    blk = SettingsTab._ai_blocker
    assert blk("openai_compat", True, "", "http://x/v1") == "Enter an API key first"
    assert blk("openai_compat", False, "", "http://x/v1") == "" and "base URL" in blk("openai_compat", False, "", "")
    assert "CLI not found" in blk("claude_code", False, "", "", cli=lambda: "") and not blk("claude_code", False, "", "", cli=lambda: "c")
    assert "CLI not found" in blk("antigravity", False, "", "", cli=lambda: "") and not blk("antigravity", False, "", "", cli=lambda: "a")
