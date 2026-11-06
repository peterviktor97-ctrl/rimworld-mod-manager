"""AI Generator tab: prompt → validated modlist. Every pick comes from the mod database (never from the model's memory)."""
from __future__ import annotations

import os
import queue
import re
import subprocess
import time
import tkinter as tk
from tkinter import ttk, simpledialog

from rmm.gui.theme import ui_scale
from rmm.gui.widgets import VERSIONS, add_placeholder, not_ready, style_text, track_ready
from rmm.models import ModList, ModRef

EXAMPLES = {
    "Medieval": "A medieval kingdom playthrough: castles, knights, swords & crossbows, feudal factions, no guns or "
                "high-tech. Keep it lore-friendly and stable.",
    "Sci-fi": "Hard sci-fi colony: more research and advanced tech, androids, better power and turrets, spaceship "
              "endgame. Vanilla-friendly.",
    "Vanilla+": "Vanilla-plus: only quality-of-life and UI improvements, better pawn management and work tabs, "
                    "nothing that changes the balance.",
    "Weak PC": "Performance-friendly light modpack for a weak laptop: performance fixes, QoL, a few small content "
               "mods. Avoid heavy mods.",
    "Hardcore": "Hardcore survival: tougher raids, deeper medical and food systems, harsher weather, combat "
                "extended style realism.",
}
SOURCES = {"ai": "◆ AI", "dependency": "↳ Dependency", "offline": "○ Offline", "replacement": "⟳ Replacement",
           "link": "⛓ Your link"}
# rejection reasons (rmm.ai _norm_picks/validate) that mean the model returned an id it was not given;
# everything else (duplicate, overlap, incompatible, removed from Steam, outdated) is a normal skip
HALLUCINATED = ("unknown id", "not offered", "malformed", "pick without", "reply was not", "no 'picks' list")


class AITab(ttk.Frame):
    COLS = (("check", "", 30, "center", False), ("name", "Mod", 150, "w", True), ("source", "Source", 104, "w", False),
            ("reason", "Why", 150, "w", True), ("inst", "Installed", 84, "w", False))

    def __init__(self, parent, app):
        super().__init__(parent, padding=8)
        self.app, self.mgr = app, app.mgr
        self._result = None
        self._picks: list = []                 # Pick objects of the shown result, row iid = index
        self._checked: list[bool] = []
        self._uids: dict[str, str | None] = {}  # workshop id -> installed uid
        self._cancel = None                    # threading.Event of the running job
        self._running = False
        self._logq: queue.SimpleQueue = queue.SimpleQueue()
        self._logged: set[str] = set()
        self._prompt_used = ""
        self._chat_history: list[tuple[str, str]] = []
        self._drafted_prompt = ""
        self._chat_busy = False
        self._tts_proc: subprocess.Popen | None = None
        self._last_spoken_text = ""
        track_ready(self)
        self._build()
        self._apply_colors()
        for ev, cb in (("settings_changed", self._update_provider), ("scanned", self._on_scanned),
                       ("active_changed", self._render_rows), ("theme_changed", self._apply_colors)):
            app.on(ev, cb)
        self._update_provider()

    # ------------------------------------------------------------ layout
    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        hero = ttk.Frame(self, style="Card.TFrame", padding=(14, 10))
        hero.grid(row=0, column=0, columnspan=1, sticky="ew", pady=(0, 8))
        head = ttk.Frame(hero, style="Panel.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="AI Modlist Generator", style="PanelHeading.TLabel").pack(side="left")
        link = ttk.Label(head, text="Configure Model", cursor="hand2", style="Link.TLabel",
                         background=self.app.colors.get("panel", "#171a24"))
        link.pack(side="right")
        link.bind("<Button-1>", lambda e: self.app.select_tab("Settings"))
        self.provider_lbl = ttk.Label(head, text="", style="Pill.TLabel")
        self.provider_lbl.pack(side="right", padx=(0, 12))
        intro = ttk.Label(hero, style="PanelMuted.TLabel", justify="left",
                          text="Describe the playthrough you want. The generator searches the RimSort Workshop database "
                               "and GitHub repositories, checking every pick so fake or outdated mods never reach your list.")
        intro.pack(fill="x", pady=(4, 0))
        intro.bind("<Configure>", lambda e: intro.configure(wraplength=max(300, e.width)))

        pw = ttk.Panedwindow(self, orient="horizontal")
        pw.grid(row=2, column=0, sticky="nsew")

        # left: prompt + options + log
        left = ttk.Frame(pw, style="Card.TFrame", padding=12)
        left.columnconfigure(0, weight=1)
        ttk.Label(left, text="Prompt — What kind of colony do you want to play?", style="PanelHeading.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        self.prompt = tk.Text(left, height=6, width=40, wrap="word", padx=10, pady=8, undo=True)
        self.prompt.grid(row=1, column=0, sticky="nsew")
        px = ui_scale(self)
        left.rowconfigure(1, weight=1, minsize=round(60 * px))     # on a short window prompt + log shrink together
        self._ph = add_placeholder(self.prompt, "e.g. “Medieval colony with better castles, more\nfactions and QoL, "
                                                "nothing too heavy for my laptop.”\nSteam Workshop, collection, and "
                                                "GitHub repo links (https://github.com/owner/repo) are added automatically.\n\n"
                                                "Enter = generate · Shift+Enter = new line",
                                   self.app)
        self.prompt.bind("<Return>", self.generate)
        self.prompt.bind("<Control-Return>", self.generate)
        self.prompt.bind("<Shift-Return>", lambda e: None)
        ex = ttk.Frame(left, style="Panel.TFrame")
        ex.grid(row=2, column=0, sticky="w", pady=(8, 10))
        ttk.Label(ex, text="Presets:", style="PanelMuted.TLabel").pack(side="left", padx=(0, 6))
        for name, text in EXAMPLES.items():
            ttk.Button(ex, text=name, width=0, style="Small.TButton",
                       command=lambda t=text: self._set_prompt(t)).pack(side="left", padx=(0, 4))

        opts = ttk.Frame(left, style="Panel.TFrame")  # below Generate: a short window clips options, never the button
        opts.grid(row=4, column=0, sticky="ew")
        s = self.mgr.settings
        ttk.Label(opts, text="Target mod count", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.count_var = tk.StringVar(value="40")
        self.count_spin = ttk.Spinbox(opts, from_=5, to=500, increment=5, textvariable=self.count_var, width=6)
        self.count_spin.grid(row=0, column=1, sticky="w")
        self.auto_count_var = tk.BooleanVar(value=bool(s.get("ai_auto_count", True)))
        ttk.Checkbutton(opts, text="Let AI decide", variable=self.auto_count_var,
                        command=self._sync_count_state).grid(row=0, column=2, sticky="w", padx=(10, 0))
        ttk.Label(opts, text="Game version", style="Panel.TLabel").grid(row=0, column=3, sticky="w", padx=(14, 8))
        self.ver_var = tk.StringVar(value=self.mgr.version_short or VERSIONS[0])
        self.ver_box = ttk.Combobox(opts, textvariable=self.ver_var, width=5, state="readonly",
                                    values=self._versions())
        self.ver_box.grid(row=0, column=4, sticky="w")
        self.no_lazy_var = tk.BooleanVar(value=bool(s.get("ai_no_lazy", True)))
        self.verify_var = tk.BooleanVar(value=bool(s.get("ai_live_verify", True)))
        self.pid_var = tk.BooleanVar(value=False)
        self.qol_var = tk.BooleanVar(value=True)
        for i, (var, text) in enumerate((
            (self.no_lazy_var, "No being lazy — tell the AI to try its best & deliver a full, thorough modlist"),
            (self.verify_var, "Use live Steam Workshop (finds new mods & verifies picks)"),
            (self.pid_var, "Only mods with a known packageId"),
            (self.qol_var, "Include QoL / performance essentials"),
        ), start=1):
            ttk.Checkbutton(opts, text=text, variable=var).grid(row=i, column=0, columnspan=5, sticky="w", pady=(6 if i == 1 else 2, 0))
        self._sync_count_state()

        run = ttk.Frame(left, style="Panel.TFrame")
        run.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.gen_btn = ttk.Button(run, text="Generate Modlist", style="Accent.TButton", command=self.generate)
        self.gen_btn.pack(side="left")
        self.cancel_btn = ttk.Button(run, text="Cancel", command=self._do_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        self.run_lbl = ttk.Label(run, text="", style="PanelMuted.TLabel")
        self.run_lbl.pack(side="left", padx=(12, 0))

        self.bottom_nb = ttk.Notebook(left)
        self.bottom_nb.grid(row=5, column=0, rowspan=2, sticky="nsew", pady=(10, 0))
        left.rowconfigure(6, weight=1, minsize=round(130 * px))

        # Tab 1: Randy's Idea Lab (Chat)
        chat_tab = ttk.Frame(self.bottom_nb, style="Panel.TFrame", padding=(6, 6))
        chat_tab.columnconfigure(0, weight=1)
        chat_tab.rowconfigure(1, weight=1)

        from rmm import supporter
        from rmm.ai import RANDY_PERSONALITIES
        self._persona_by_label = {v["label"]: k for k, v in RANDY_PERSONALITIES.items()}
        self._label_by_persona = {k: v["label"] for k, v in RANDY_PERSONALITIES.items()}
        saved_persona = str(self.mgr.settings.get("ai_chat_persona") or "efficient")
        if saved_persona not in self._label_by_persona or not supporter.is_persona_unlocked(saved_persona, self.mgr.settings):
            saved_persona = "efficient"

        p_bar = ttk.Frame(chat_tab, style="Panel.TFrame")
        p_bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(p_bar, text="Personality:", style="PanelMuted.TLabel").pack(side="left", padx=(0, 6))
        self.persona_var = tk.StringVar(value=self._label_by_persona[saved_persona])
        self.persona_box = ttk.Combobox(p_bar, textvariable=self.persona_var, state="readonly", width=24,
                                        values=list(self._persona_by_label.keys()))
        self.persona_box.pack(side="left")
        self.persona_box.bind("<<ComboboxSelected>>", self._on_persona_changed)
        ttk.Button(p_bar, text="Clear", width=0, style="Small.TButton",
                   command=self._clear_chat_memory).pack(side="left", padx=(6, 2))

        self.tts_var = tk.BooleanVar(value=bool(self.mgr.settings.get("ai_tts_enabled", True)))
        ttk.Checkbutton(p_bar, text="Auto-TTS", variable=self.tts_var,
                        command=self._on_tts_toggled).pack(side="left", padx=(8, 4))
        ttk.Button(p_bar, text="Read", width=0, style="Small.TButton",
                   command=self._read_last_aloud).pack(side="left", padx=(2, 2))
        ttk.Button(p_bar, text="Stop", width=0, style="Small.TButton",
                   command=self.stop_tts).pack(side="left", padx=(2, 0))

        cf = ttk.Frame(chat_tab, style="Panel.TFrame")
        cf.grid(row=1, column=0, sticky="nsew")
        cf.columnconfigure(0, weight=1)
        cf.rowconfigure(0, weight=1)
        self.chat_view = tk.Text(cf, height=7, width=40, wrap="word", padx=8, pady=6, state="disabled")
        csb = ttk.Scrollbar(cf, orient="vertical", command=self.chat_view.yview)
        self.chat_view.configure(yscrollcommand=csb.set)
        self.chat_view.grid(row=0, column=0, sticky="nsew")
        csb.grid(row=0, column=1, sticky="ns")

        c_inp = ttk.Frame(chat_tab, style="Panel.TFrame")
        c_inp.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        c_inp.columnconfigure(0, weight=1)
        self.chat_var = tk.StringVar()
        self.chat_entry = ttk.Entry(c_inp, textvariable=self.chat_var)
        self.chat_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.chat_entry.bind("<Return>", self._send_chat)
        self.chat_send_btn = ttk.Button(c_inp, text="Ask Randy", style="Small.TButton", command=self._send_chat)
        self.chat_send_btn.grid(row=0, column=1, sticky="e")

        c_btns = ttk.Frame(chat_tab, style="Panel.TFrame")
        c_btns.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(c_btns, text="Suggest Mods", style="Small.TButton",
                   command=self._suggest_more_mods).pack(side="left", padx=(0, 4))
        ttk.Button(c_btns, text="Surprise Me", style="Small.TButton",
                   command=lambda: self._quick_chat("Pitch me a hilariously chaotic, unforgettable RimWorld colony idea with 5 must-have mods!")).pack(side="left", padx=(0, 4))
        ttk.Button(c_btns, text="Spice Up Prompt", style="Small.TButton",
                   command=self._spice_up_prompt).pack(side="left", padx=(0, 4))
        ttk.Button(c_btns, text="Use Draft", style="Small.TButton",
                   command=self._apply_draft_to_prompt).pack(side="left", padx=(0, 4))
        ttk.Button(c_btns, text="Clear", style="Small.TButton",
                   command=self._clear_chat_memory).pack(side="left", padx=(0, 4))
        ttk.Button(c_btns, text="Build Modlist", style="Accent.TButton",
                   command=self._engineer_and_build).pack(side="right")

        # Tab 2: Live AI Reasoning & Verification Log
        lf = ttk.Frame(self.bottom_nb, style="Panel.TFrame", padding=(4, 4))
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.log = tk.Text(lf, height=8, width=40, wrap="word", padx=8, pady=6, state="disabled",
                           font=self.app.fonts.get("mono", ("Consolas", 9)))
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")

        self.bottom_nb.add(chat_tab, text="Idea Chat")
        self.bottom_nb.add(lf, text="Output Log")
        self._init_chat_greeting()
        pw.add(left, weight=2)

        # right: results
        right = ttk.Frame(pw, style="Card.TFrame", padding=12)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.badge = ttk.Label(right, text="No results yet", style="PanelHeading.TLabel")
        self.badge.grid(row=0, column=0, sticky="w")
        self.summary = ttk.Label(right, text="Picks show up here. Click ☑ to include/exclude a mod.", style="PanelMuted.TLabel",
                                 wraplength=520, justify="left")
        self.summary.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        tf = ttk.Frame(right, style="Panel.TFrame")
        tf.grid(row=2, column=0, sticky="nsew")
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended")
        sc = ui_scale(self)                          # column widths are pixels, fonts scale with DPI
        for cid, text, width, anchor, stretch in self.COLS:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=round(width * sc), minwidth=round(30 * sc), anchor=anchor, stretch=stretch)
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.selection()))
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._context_menu)
        right.bind("<Configure>", lambda e: self.summary.configure(wraplength=max(200, e.width - 24)))

        sel = ttk.Frame(right, style="Panel.TFrame")  # All/None beside the count: the action row gets the full width
        sel.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.b_none = ttk.Button(sel, text="None", width=5, style="Small.TButton", command=lambda: self._set_all(False))
        self.b_all = ttk.Button(sel, text="All", width=5, style="Small.TButton", command=lambda: self._set_all(True))
        self.b_none.pack(side="right")
        self.b_all.pack(side="right", padx=(0, 4))
        self.sel_lbl = ttk.Label(sel, text="", style="PanelMuted.TLabel")
        self.sel_lbl.pack(side="left")
        btns = ttk.Frame(right, style="Panel.TFrame")
        btns.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.b_save = ttk.Button(btns, text="Save as Modlist…", command=self._save_modlist)
        self.b_apply = ttk.Button(btns, text="Add to Active + Download Rest", style="Accent.TButton", command=self._add_to_active)
        self.b_dl = ttk.Button(btns, text="Download Missing", command=self._download_missing)
        for b in (self.b_apply, self.b_dl, self.b_save):
            b.pack(side="right", padx=(4, 0))

        self.rej_btn = ttk.Button(right, text="▸ Rejected ids (0)", command=self._toggle_rejected)  # gridded with a result
        self.rej_text = tk.Text(right, height=6, wrap="word", padx=8, pady=6, state="disabled",
                                font=self.app.fonts.get("mono", ("Consolas", 9)))
        pw.add(right, weight=3)
        self._set_result_buttons()

    def _apply_colors(self, *_):
        c = self.app.colors
        for w in (self.prompt, self.log, self.rej_text, getattr(self, "chat_view", None)):
            if w is not None:
                style_text(w, c)
        if hasattr(self, "chat_view"):
            self.chat_view.tag_configure("bot_name", foreground=c.get("accent", "#5b8def"), font=self.app.fonts.get("bold"))
            self.chat_view.tag_configure("user_name", foreground=c.get("ok", "#4caf50"), font=self.app.fonts.get("bold"))
            self.chat_view.tag_configure("sug_lbl", foreground=c.get("ok", "#4caf50"), font=self.app.fonts.get("bold"))
            self.chat_view.tag_configure("sug_txt", foreground=c.get("fg", "#e6e8ee"))
            self.chat_view.tag_configure("draft_lbl", foreground=c.get("warn", "#e0a526"), font=self.app.fonts.get("bold"))
            self.chat_view.tag_configure("draft_txt", foreground=c.get("muted", "#9aa0a6"))
        self.tree.tag_configure("dim", foreground=c["muted"])
        self.tree.tag_configure("installed", foreground=c["ok"])
        self._color_badge()
        if hasattr(self, "provider_lbl") and self.provider_lbl.cget("text"):
            self._update_provider()

    def _versions(self) -> list[str]:
        cur = self.mgr.version_short
        return ([cur] if cur and cur not in VERSIONS else []) + VERSIONS

    def _set_prompt(self, text: str):
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", text)
        self._ph()
        self.prompt.focus_set()

    def focus_search(self):
        self.prompt.focus_set()

    def _sync_count_state(self, *_):
        if hasattr(self, "count_spin") and hasattr(self, "auto_count_var"):
            self.count_spin.configure(state="disabled" if self.auto_count_var.get() else "normal")

    # ------------------------------------------------------------ Randy's Idea Lab chat & TTS
    def _current_persona(self) -> tuple[str, dict]:
        from rmm import supporter
        from rmm.ai import RANDY_PERSONALITIES
        lbl = self.persona_var.get() if hasattr(self, "persona_var") else ""
        key = getattr(self, "_persona_by_label", {}).get(lbl, "efficient")
        if not supporter.is_persona_unlocked(key, self.mgr.settings):
            key = "efficient"
        return key, RANDY_PERSONALITIES.get(key) or RANDY_PERSONALITIES["efficient"]

    def _on_tts_toggled(self, *_):
        on = bool(self.tts_var.get())
        try:
            self.mgr.settings.set("ai_tts_enabled", on)
        except Exception:
            pass
        if not on:
            self.stop_tts()

    def stop_tts(self, *_):
        proc = getattr(self, "_tts_proc", None)
        self._tts_proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def _speak_tts(self, text: str, *, force: bool = False):
        if os.name != "nt" or not text or not text.strip():
            return
        if not force and not (hasattr(self, "tts_var") and self.tts_var.get()):
            return
        try:
            if str(self.winfo_toplevel().state()) == "withdrawn":
                return
        except Exception:
            return
        self.stop_tts()
        # Clean markdown, URLs, and non-speech symbols so Windows SAPI reads natural sentences
        clean = re.sub(r"https?://\S+", " link ", text)
        clean = re.sub(r"[*_`#~>|•✦▸▾✔⚠✖🎲💀📐🌻⚙️🤡☭📺🎖⚡🚀🌶⬆️💬🔊🗣️🔇🤖🧑]+", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            return
        self._last_spoken_text = clean
        persona_key, _ = self._current_persona()
        female = persona_key in ("cassandra", "phoebe")
        rate = {"eighties": 2, "brainrot": 2, "efficient": 2, "warcrime": 1, "iron_commander": 1, "phoebe": 0, "archotech": -1}.get(persona_key, 1)
        gender = "Female" if female else "Male"
        ps_cmd = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::{gender}); "
            f"$s.Rate = {rate}; "
            "$t = [Console]::In.ReadToEnd(); "
            "if ($t) { $s.Speak($t) }"
        )
        try:
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._tts_proc = proc
            if proc.stdin:
                proc.stdin.write(clean.encode("utf-8", "replace"))
                proc.stdin.close()
        except Exception:
            pass

    def _read_last_aloud(self):
        txt = self._last_spoken_text
        if not txt and self._chat_history:
            txt = next((m for r, m in reversed(self._chat_history) if r == "assistant"), "")
        if not txt and self._result and self._result.summary:
            txt = self._result.summary
        if not txt:
            _, info = self._current_persona()
            txt = info["greeting"]
        self._speak_tts(txt, force=True)

    def _reset_chat_session(self, *, clear_prompt: bool = False, speak: bool = False):
        self.stop_tts()
        self._chat_history.clear()
        self._drafted_prompt = ""
        self.chat_var.set("")
        if clear_prompt and hasattr(self, "prompt"):
            self.prompt.delete("1.0", "end")
            self._ph()
        _, info = self._current_persona()
        greeting = info["greeting"]
        self._last_spoken_text = greeting
        w = self.chat_view
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.configure(state="disabled")
        self._append_chat(info["speaker"], greeting, is_bot=True)
        if speak:
            self._speak_tts(greeting)

    def _clear_chat_memory(self, *_):
        self._reset_chat_session(clear_prompt=True, speak=False)
        self.app.toast("Cleared AI memory — fresh chat session started!", "ok")

    def _on_persona_changed(self, *_):
        from rmm import supporter
        lbl = self.persona_var.get() if hasattr(self, "persona_var") else ""
        picked = getattr(self, "_persona_by_label", {}).get(lbl, "efficient")
        if not supporter.is_persona_unlocked(picked, self.mgr.settings):
            self.persona_var.set(self._label_by_persona["efficient"])
            self.app.toast("★ Storyteller personalities are a Supporter perk — enter a Supporter Key to unlock!", "warn")
            supporter.show_support_dialog(self.app)
            return
        try:
            self.mgr.settings.set("ai_chat_persona", picked)
        except Exception:
            pass
        self._reset_chat_session(clear_prompt=False, speak=True)

    def _init_chat_greeting(self):
        self._reset_chat_session(clear_prompt=False, speak=False)

    def _append_chat(self, speaker: str, text: str, *, is_bot: bool = True, draft: str = ""):
        w = self.chat_view
        w.configure(state="normal")
        if w.index("end-1c") != "1.0":
            w.insert("end", "\n\n")
        w.insert("end", f"{speaker}: ", "bot_name" if is_bot else "user_name")
        w.insert("end", text.strip())
        if draft:
            w.insert("end", "\nDrafted Prompt: ", "draft_lbl")
            w.insert("end", draft.strip(), "draft_txt")
        w.see("end")
        w.configure(state="disabled")

    def _quick_chat(self, msg: str):
        self.chat_var.set(msg)
        self._send_chat()

    def _suggest_more_mods(self):
        cur = self.prompt.get("1.0", "end-1c").strip() or self._drafted_prompt
        if cur:
            self._quick_chat(f"Suggest 5 more awesome, specific RimWorld mods, hidden gems, and gameplay twists that would pair well with: {cur}")
        else:
            self._quick_chat("Suggest 5 awesome, high-synergy RimWorld mods, overhauls, and hidden gems I should try in my next colony!")

    def _spice_up_prompt(self):
        cur = self.prompt.get("1.0", "end-1c").strip()
        if cur:
            self._quick_chat(f"Take my current prompt and make it 10x more detailed, exciting, and complete with specific mod series: {cur}")
        else:
            self._quick_chat("Give me an epic, massive RimWorld colony concept with Rimefeller, Rimatomics, and Biotech!")

    def _apply_draft_to_prompt(self):
        if not self._drafted_prompt:
            self.app.toast("Ask Randy in the chat first so he drafts a prompt!", "warning")
            self.chat_entry.focus_set()
            return
        self._set_prompt(self._drafted_prompt)
        self.app.toast("Copied Randy's engineered prompt into the Prompt box!", "ok")

    def _engineer_and_build(self):
        if self._running or self._chat_busy:
            return
        typed = self.chat_var.get().strip()
        if typed:
            self._send_chat(force_build=True)
            return
        if self._drafted_prompt:
            self._set_prompt(self._drafted_prompt)
            self.generate()
            return
        cur = self.prompt.get("1.0", "end-1c").strip()
        if cur:
            self.generate()
        else:
            self._quick_chat("Engineer an awesome, comprehensive modlist for my owned DLCs and build it now!")

    def _send_chat(self, event=None, *, force_build: bool = False):
        if self._chat_busy:
            return "break"
        msg = self.chat_var.get().strip()
        if not msg:
            return "break"
        self.chat_var.set("")
        if force_build and "build" not in msg.lower():
            msg += " (build it now!)"
        persona_key, persona_info = self._current_persona()
        speaker = persona_info["speaker"]
        self._chat_history.append(("user", msg))
        self._append_chat("You", msg, is_bot=False)
        cur_prompt = self.prompt.get("1.0", "end-1c").strip() or self._drafted_prompt
        ver = self.ver_var.get()
        self._chat_busy = True
        self.chat_send_btn.state(["disabled"])
        self.chat_entry.state(["disabled"])
        hist = list(self._chat_history)

        def done(out: dict):
            self._chat_busy = False
            self.chat_send_btn.state(["!disabled"])
            self.chat_entry.state(["!disabled"])
            reply = str(out.get("reply") or "").strip()
            eng = str(out.get("engineered_prompt") or "").strip()
            bnow = bool(out.get("build_now")) or force_build
            if eng:
                self._drafted_prompt = eng
                self._set_prompt(eng)
            if reply:
                self._chat_history.append(("assistant", reply))
                self._append_chat(speaker, reply, is_bot=True, draft=eng)
                self._speak_tts(reply)
            if bnow and (eng or self.prompt.get("1.0", "end-1c").strip()):
                self.generate()

        def err(e):
            self._chat_busy = False
            self.chat_send_btn.state(["!disabled"])
            self.chat_entry.state(["!disabled"])
            self._append_chat(speaker, f"Drop pod landed on the comms console ({e}). Try again!", is_bot=True)

        self.app.run_bg(self.mgr.chat_idea, hist, cur_prompt, ver, persona_key,
                        busy=f"{speaker} is cooking up your colony idea…", on_done=done, on_error=err)
        return "break"

    # ------------------------------------------------------------ provider line
    def _update_provider(self, *_):
        s = self.mgr.settings
        if hasattr(self, "auto_count_var"):
            self.auto_count_var.set(bool(s.get("ai_auto_count", True)))
            self._sync_count_state()
        if hasattr(self, "no_lazy_var"):
            self.no_lazy_var.set(bool(s.get("ai_no_lazy", True)))
        preset, provider, model = s.get("ai_preset", ""), s.get("ai_provider", "anthropic"), s.get("ai_model", "")
        if provider == "offline":
            text, warn = "Offline generator (no AI): keyword search + popularity ranking", False
        else:
            needs_key = True
            try:
                from rmm import ai
                needs_key = bool(ai.PRESETS.get(preset, {}).get("needs_key", True))
            except Exception:
                pass
            has_key = bool(s.get_secret("ai_api_key"))
            key = "API key ✔" if has_key else ("no key needed" if not needs_key else "no API key set!")
            text, warn = f"{preset or provider} · {model or 'default model'} · {key}", needs_key and not has_key
        self.provider_lbl.configure(text=text, foreground=self.app.colors["warn" if warn else "muted"])
        vals = self._versions()
        self.ver_box.configure(values=vals)
        if self.ver_var.get() not in vals:
            self.ver_var.set(vals[0])

    def _on_scanned(self, *_):
        cur = self.mgr.version_short
        if cur:
            self.ver_box.configure(values=self._versions())
            if not self._picks and not self._running:
                self.ver_var.set(cur)
        if self._picks:
            picks = list(self._picks)
            self.app.run_bg(lambda: self._match(picks), on_done=self._on_matched, on_error=lambda e: None)

    def _match(self, picks) -> dict:
        return {p.workshop_id: self.mgr.match_ref(ModRef(p.package_id, p.name, p.workshop_id)) for p in picks}

    def _on_matched(self, uids: dict):
        self._uids = uids
        self._render_rows()

    # ------------------------------------------------------------ generate
    def generate(self, *_):
        if self._running:
            return "break"
        prompt = self.prompt.get("1.0", "end-1c").strip()
        if not prompt:
            self.app.toast("Describe what you want first (or click an example).", "warning")
            self.prompt.focus_set()
            return "break"
        try:
            count = max(5, min(500, int(float(self.count_var.get()))))
        except ValueError:
            count = 40
        self.count_var.set(str(count))
        auto_count = bool(self.auto_count_var.get())
        no_lazy = bool(self.no_lazy_var.get())
        try:
            self.mgr.settings.set("ai_auto_count", auto_count)
            self.mgr.settings.set("ai_no_lazy", no_lazy)
        except Exception:
            pass
        eff_count = 0 if auto_count else count
        kw = {"version_short": self.ver_var.get(), "live_verify": self.verify_var.get(),
              "require_package_id": self.pid_var.get(), "include_popular_qol": self.qol_var.get(),
              "auto_count": auto_count, "no_lazy": no_lazy}
        self._prompt_used = prompt
        if hasattr(self, "bottom_nb"):
            self.bottom_nb.select(1)      # show Live AI Log while generating
        self._clear_log()
        self._logged.clear()
        size_txt = "AI-chosen mod count" if auto_count else f"~{count} mods"
        if no_lazy:
            size_txt += " · no-lazy mode ON"
        self._log(f"Generating ({size_txt}) for RimWorld {kw['version_short']}…")
        self._set_running(True)
        ev = self.app.run_bg(self._job, prompt, eff_count, kw, progress=True, cancellable=True,
                             busy="AI: generating modlist…", on_done=self._on_done, on_error=self._on_error)
        if self._running:  # on_done may already have run if the runner was synchronous
            self._cancel = ev
            self.after(100, self._poll)
        return "break"

    def _job(self, prompt: str, count: int, kw: dict, progress, cancel):
        """Background thread: run the generator, mirror progress lines into our log queue, match picks to installed mods."""
        def prog(f=None, text=""):
            progress(f, text)
            if text:
                self._logq.put(text)
        res = self.mgr.generate_ai(prompt, target_count=count, progress=prog, cancel=cancel, **kw)
        return res, self._match(res.picks)

    def _poll(self):
        self._drain()
        if self._running:
            self.after(100, self._poll)

    def _drain(self):
        while True:
            try:
                line = self._logq.get_nowait()
            except queue.Empty:
                return
            if line not in self._logged:
                self._logged.add(line)
                self._log(line)

    def _do_cancel(self):
        if self._running and self._cancel is not None:
            self._cancel.set()
            self.cancel_btn.state(["disabled"])
            self.run_lbl.configure(text="Cancelling…")

    def _set_running(self, on: bool):
        self._running = on
        self.gen_btn.state(["disabled"] if on else ["!disabled"])
        self.cancel_btn.state(["!disabled"] if on else ["disabled"])
        self.run_lbl.configure(text="Working… this can take a minute." if on else "")

    def _on_done(self, result):
        cancelled = self._cancel is not None and self._cancel.is_set()
        self._cancel = None
        self._set_running(False)
        self._drain()
        res, uids = result
        for line in getattr(res, "log", None) or []:
            if line not in self._logged:
                self._logged.add(line)
                self._log(line)
        if getattr(res, "queries", None):
            self._log(f"Database searches ({len(res.queries)}): " + ", ".join(res.queries))
        self._log("Cancelled — showing partial results." if cancelled else f"Done: {len(res.picks)} picks.")
        self._show_result(res, uids)

    def _on_error(self, e):
        cancelled = self._cancel is not None and self._cancel.is_set()
        self._cancel = None
        self._set_running(False)
        self._drain()
        if cancelled:
            self._log("Cancelled.")
            return
        self._log(f"✖ {e}")
        from rmm.ai import LLMError
        msg = str(e) or type(e).__name__
        if isinstance(e, LLMError):
            msg += ("\n\nTip: check the provider/API key in Settings, or pick “Offline (no AI)” "
                    "to build a list from the database without an AI.")
        else:                               # e.g. no mod database (offline first run): details stay in the log
            first = msg.splitlines()[0].rstrip(" :.") + "."
            if "database" in first.lower():
                first += "\n\nCheck your internet connection, or import steamDB.json in Tools → Database."
            msg = first + ("\n\nDetails are in the progress log." if "\n" in msg.strip() else "")
        self.app.error("AI generator", msg)

    # ------------------------------------------------------------ log
    def _log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------ results
    def _show_result(self, res, uids: dict):
        self._result, self._uids = res, uids
        self._picks = list(res.picks)
        self._checked = [True] * len(self._picks)
        n_rej = len(res.rejected)
        self.summary.configure(text=(res.summary or "").strip() or "Review the picks, untick what you don't want, "
                                                                   "then save or apply.")
        self.rej_btn.configure(text=f"{'▾' if self.rej_text.winfo_manager() else '▸'} Rejected ids ({n_rej})")
        self.rej_btn.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.rej_text.configure(state="normal")
        self.rej_text.delete("1.0", "end")
        self.rej_text.insert("1.0", "\n".join(f"{i}  —  {why}" for i, why in res.rejected) or
                             "Nothing rejected: every id the model returned exists in the database.")
        self.rej_text.configure(state="disabled")
        self._color_badge()
        self._render_rows()
        self._set_result_buttons()
        if res.summary and res.picks:
            self._speak_tts(res.summary.splitlines()[0])

    def _color_badge(self):
        c = self.app.colors
        if self._result is None or not self._picks:      # also a cancelled/empty run: nothing was verified
            self.badge.configure(text="No results yet" if self._result is None else "No picks", foreground=c["text"])
            return
        n = len(self._picks)
        n_hal = sum(1 for _, why in self._result.rejected if str(why).startswith(HALLUCINATED))
        n_skip = len(self._result.rejected) - n_hal
        text = f"✔ {n} picks verified against the database · " + (
            f"⚠ {n_hal} hallucinated/invalid ids rejected" if n_hal else "0 hallucinated ids")
        if n_skip:
            text += f" · {n_skip} skipped (duplicates/unavailable/outdated)"
        self.badge.configure(text=text, foreground=c["warn" if n_hal else "ok"])

    def _render_rows(self, *_):
        self.tree.delete(*self.tree.get_children())
        active = set(self.mgr.active)
        for i, p in enumerate(self._picks):
            uid = self._uids.get(p.workshop_id)
            inst = ("✔ active" if uid in active else "✔ installed") if uid else "download"
            tags = ("dim",) if not self._checked[i] else (("installed",) if uid else ("missing",))
            is_gh = str(p.workshop_id or "").startswith("gh:")
            src_lbl = ("🐙 GitHub link" if p.source == "link" else "🐙 GitHub") if is_gh else SOURCES.get(p.source, p.source)
            self.tree.insert("", "end", iid=str(i), tags=tags,
                             values=("☑" if self._checked[i] else "☐", p.name or p.package_id or p.workshop_id,
                                     src_lbl, p.reason, inst))
        self._update_sel_label()

    def _update_sel_label(self):
        chosen = [p for p, on in zip(self._picks, self._checked) if on]
        inst = sum(1 for p in chosen if self._uids.get(p.workshop_id))
        self.sel_lbl.configure(text=f"{len(chosen)} of {len(self._picks)} selected · {inst} installed · "
                                    f"{len(chosen) - inst} to download" if self._picks else "")
        self._set_result_buttons()

    def _set_result_buttons(self):
        any_on = any(self._checked)
        for b in (self.b_save, self.b_apply, self.b_dl):
            b.state(["!disabled"] if any_on else ["disabled"])
        for b in (self.b_all, self.b_none):
            b.state(["!disabled"] if self._picks else ["disabled"])

    def _toggle(self, iids):
        for iid in iids:
            i = int(iid)
            self._checked[i] = not self._checked[i]
            p = self._picks[i]
            uid = self._uids.get(p.workshop_id)
            self.tree.set(iid, "check", "☑" if self._checked[i] else "☐")
            self.tree.item(iid, tags=("dim",) if not self._checked[i] else (("installed",) if uid else ("missing",)))
        self._update_sel_label()
        return "break"

    def _set_all(self, on: bool):
        self._checked = [on] * len(self._picks)
        self._render_rows()

    def _on_click(self, e):
        if self.tree.identify_region(e.x, e.y) == "cell" and self.tree.identify_column(e.x) == "#1":
            iid = self.tree.identify_row(e.y)
            if iid:
                return self._toggle([iid])

    def _on_double(self, e):
        if self.tree.identify_column(e.x) == "#1":
            return "break"
        iid = self.tree.identify_row(e.y)
        if iid:
            self._open_pages([iid])

    def _open_pages(self, iids):
        wids = [self._picks[int(i)].workshop_id for i in iids][:20]

        def job():
            from rmm import steam
            steam.open_in_browser(wids)
        self.app.run_bg(job)

    def _context_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        sel = self.tree.selection()
        p = self._picks[int(iid)]
        uid = self._uids.get(p.workshop_id)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Include / exclude", command=lambda: self._toggle(sel))
        page_lbl = "Open GitHub page" if str(p.workshop_id or "").startswith("gh:") else "Open workshop page"
        menu.add_command(label=page_lbl, command=lambda: self._open_pages(sel))
        menu.add_command(label="Show in Mods tab", command=lambda: self.app.show_mod_details(uid=uid),
                         state="normal" if uid else "disabled")
        menu.tk_popup(e.x_root, e.y_root)

    def _toggle_rejected(self):
        n = len(self._result.rejected) if self._result else 0
        if self.rej_text.winfo_manager():
            self.rej_text.grid_remove()
            self.rej_btn.configure(text=f"▸ Rejected ids ({n})")
        else:
            self.rej_text.grid(row=6, column=0, sticky="ew", pady=(4, 0))
            self.rej_btn.configure(text=f"▾ Rejected ids ({n})")

    # ------------------------------------------------------------ actions
    def _chosen(self):
        return [p for p, on in zip(self._picks, self._checked) if on]

    def _save_modlist(self):
        picks = self._chosen()
        if not picks:
            return
        default = self._prompt_used.splitlines()[0][:40].strip() if self._prompt_used else "AI modlist"
        name = simpledialog.askstring("Save as modlist", "Name for the modlist:", initialvalue=default, parent=self)
        if not name or not name.strip():
            return
        now = time.time()
        ml = ModList(name=name.strip(), mods=[ModRef(p.package_id, p.name, p.workshop_id) for p in picks],
                     game_version=self.mgr.game_version or self.ver_var.get(), source="ai", created=now, updated=now,
                     notes=f"AI prompt: {self._prompt_used}\n\n{(self._result.summary or '').strip()}".strip())

        def job():
            ml.name = self.mgr.unique_modlist_name(ml.name)   # never over another list (case/illegal chars count)
            self.mgr.save_modlist(ml)
            return ml.name

        def done(final):
            self.app.emit("modlists_changed")
            self.app.toast(f"Saved modlist “{final}” ({len(ml.mods)} mods)", "ok")
        self.app.run_bg(job, on_done=done)

    def _split(self):
        picks = self._chosen()
        uids = [u for p in picks if (u := self._uids.get(p.workshop_id))]
        missing = [ModRef(p.package_id, p.name, p.workshop_id) for p in picks if not self._uids.get(p.workshop_id)]
        return uids, missing

    def _add_to_active(self):
        """Activate the installed picks and sort the list (Harmony/libraries before their dependents); the rest is
        downloaded with its dependencies and activated + sorted by Manager.download(activate=True) once installed."""
        if not_ready(self):
            return
        uids, missing = self._split()
        active = set(self.mgr.active)
        new = [u for u in uids if u not in active]
        if new:
            self.mgr.activate(new)
            self.app.emit("active_changed")
            self.app.run_bg(self.mgr.auto_sort, busy="Sorting the active list…",
                            on_done=lambda _w: self.app.emit("active_changed"))
        parts = [f"Activated and sorted {len(new)} installed mod{'s' if len(new) != 1 else ''}"] if new else []
        if missing:
            self.app.queue_download(missing, activate=True)
            parts.append(f"Queued {len(missing)} downloads + missing dependencies (activated when installed)")
        self.app.toast(" · ".join(parts or ["Already active"]) + " — Save in the Mods tab.", "ok")

    def _download_missing(self):
        if not_ready(self):
            return
        _, missing = self._split()
        if not missing:
            self.app.toast("All selected mods are already installed.", "info")
            return
        self.app.queue_download(missing)             # Manager.download adds the missing dependencies
        self.app.toast(f"Queued {len(missing)} downloads + missing dependencies — see the Downloads tab.", "ok")


# ---------------------------------------------------------------- self-test
def _selftest() -> None:
    from types import SimpleNamespace as NS
    from rmm.gui.workshop_tab import _SmokeApp, _SmokeMgr
    try:
        root = tk.Tk()
    except tk.TclError:
        return
    root.withdraw()
    try:
        mgr = _SmokeMgr()
        picks = [NS(workshop_id="2009463077", package_id="brrainz.harmony", name="Harmony", reason="needed", source="dependency"),
                 NS(workshop_id="1111", package_id="a.b", name="Alpha", reason="fits", source="ai")]
        mgr.generate_ai = lambda prompt, target_count=40, progress=None, cancel=None, **kw: (
            progress(0.5, "planning"), NS(picks=picks, rejected=[("999", "unknown id"), ("5", "duplicate pick")],
                                          queries=["alpha"], summary="ok", log=["x"]))[1]
        app = _SmokeApp(root, mgr)
        tab = AITab(root, app)
        app.emit("scanned")
        tab._set_prompt("alpha colony")
        tab.generate()
        assert not tab._running and len(tab._picks) == 2, tab._picks
        assert "1 hallucinated" in tab.badge.cget("text") and "1 skipped" in tab.badge.cget("text"), tab.badge.cget("text")
        assert tab.tree.set("0", "inst") == "✔ active" and tab.tree.set("1", "inst") == "download"
        tab._toggle(["0"])
        assert tab._checked == [False, True]
        tab._download_missing()
        assert [r.workshop_id for r in app.queued[-1][0]] == ["1111"] and app.queued[-1][2] == {}
        tab._set_all(True)
        sorts = []
        mgr.auto_sort = lambda: sorts.append(list(mgr.active)) or []
        tab._add_to_active()                     # nothing new installed: no sort; the rest is activated on install
        assert mgr.active == ["brrainz.harmony"] and not sorts and app.queued[-1][2] == {"activate": True}, app.queued
        from rmm.models import Mod
        mgr.installed["a.b"] = Mod("a.b", "a.b", "Alpha", "", "local", "1111")      # installed meanwhile
        app.emit("scanned")
        tab._add_to_active()
        assert mgr.active == ["brrainz.harmony", "a.b"] and sorts == [mgr.active], (mgr.active, sorts)
        from rmm.ai import LLMError
        shown = []
        app.error = lambda title, msg: shown.append(msg)
        tab._on_error(RuntimeError("The mod database is still empty after trying every source:\nSteam DB: <urlopen …>"))
        tab._on_error(LLMError("Anthropic rejected the API key (401).", "auth"))
        assert "urlopen" not in shown[0] and "API key" not in shown[0] and "internet" in shown[0], shown[0]
        assert "Tip: check the provider/API key" in shown[1], shown[1]
        log = tab.log.get("1.0", "end")
        assert "planning" in log and "Done: 2 picks." in log, log
    finally:
        root.destroy()


if __name__ == "__main__":
    _selftest()
    print("ai_tab selftest ok")
