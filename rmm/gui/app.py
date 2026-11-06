"""Main window: tab notebook, background runner, events, status bar, dialog helpers, startup and shutdown."""
from __future__ import annotations

import importlib
import logging
import queue
import re
import threading
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

from rmm import supporter
from rmm.gui import theme
from rmm.models import APP_ID, APP_NAME, ModRef

# (tab title, module in rmm.gui, class name) — order is the notebook order
TABS = [("Mods", "mods_tab", "ModsTab"), ("Modlists", "modlists_tab", "ModlistsTab"),
        ("Workshop", "workshop_tab", "WorkshopTab"), ("AI Generator", "ai_tab", "AITab"),
        ("Downloads", "downloads_tab", "DownloadsTab"), ("Tools", "tools_tab", "ToolsTab"),
        ("Settings", "settings_tab", "SettingsTab")]
_TAB_ICONS: dict[str, str] = {}
PATH_KEYS = ("game_dir", "config_dir", "workshop_dir", "steamcmd_dir", "extra_mod_dirs")
_TEXT_CLASSES = {"Text", "Entry", "TEntry", "TCombobox", "TSpinbox", "Spinbox"}
_EXTERNAL_MSG = ("ModsConfig.xml was changed outside the manager since it was loaded (probably by RimWorld's own "
                 "mod menu). Saving now replaces those changes; a backup is kept.")


def _not_loaded(exc: BaseException) -> bool:
    """core.Manager.save_active_to_game refuses to write before ModsConfig.xml was first loaded."""
    return isinstance(exc, RuntimeError) and "not been loaded" in str(exc)


def _save_failed_msg(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return ("ModsConfig.xml could not be written: it is read-only or locked (RimWorld, antivirus, OneDrive?).\n"
                f"Clear its read-only flag or close the program holding it, then save again.\n\n{exc}")
    return str(exc) or type(exc).__name__


def _first_line(exc_or_text) -> str:
    return (str(exc_or_text).strip().splitlines() or [""])[0].rstrip(":")[:200]


def set_dpi_awareness() -> None:
    """Must run before tk.Tk(): crisp text on scaled displays instead of blurry bitmap stretching."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def make_icon(size: int, accent: str = "#5b8def") -> tk.PhotoImage:
    """Window icon drawn in code: rounded accent tile with a three-line 'mod list' glyph."""
    img = tk.PhotoImage(width=size, height=size)
    r = max(2, size // 5)
    for y in range(size):                       # rounded square, one horizontal span per row
        dy = max(r - y, y - (size - 1 - r), 0)
        inset = r - int((r * r - dy * dy) ** 0.5) if dy else 0
        img.put(accent, to=(inset, y, size - inset, y + 1))
    bar_h = max(1, size // 10)
    for i in range(3):
        y = size * (27 + i * 17) // 100
        img.put("#b9f6ca", to=(size * 22 // 100, y, size * 22 // 100 + bar_h + 1, y + bar_h + 1))
        img.put("#ffffff", to=(size * 38 // 100, y, size * 80 // 100, y + bar_h + 1))
    return img


class _Job:
    __slots__ = ("name", "busy", "cancel", "cancellable", "has_progress", "frac", "text")

    def __init__(self, name: str, busy: str, cancel: threading.Event, cancellable: bool, has_progress: bool):
        self.name, self.busy, self.cancel = name, busy, cancel
        self.cancellable, self.has_progress = cancellable, has_progress
        self.frac: float | None = None
        self.text = ""

    def progress(self, fraction: float | None = None, text: str = "") -> None:
        """Called from the worker thread: plain attribute stores, read by the Tk poll loop."""
        self.frac = fraction
        if text:
            self.text = str(text)


class App:
    def __init__(self, mgr, root: tk.Tk | None = None):
        self.mgr = mgr
        self.log = getattr(mgr, "log", None) or logging.getLogger("rmm")
        self.root = root or tk.Tk()
        self.root.withdraw()                     # no flash of an unstyled window
        self._tk_thread = threading.current_thread()
        self._handlers: dict[str, list] = {}
        self._q: queue.Queue = queue.Queue()
        self._jobs: list[_Job] = []
        self._busy_state = None
        self._toast_after = None
        self._scanning = False
        self._db_hooks: list | None = None      # (on_done, on_error, on_progress) of every caller of the running DB update
        self._shown_job: _Job | None = None
        self._closing = False
        self.tabs: dict[str, tk.Widget] = {}
        self._tab_titles: list[str] = []
        self._fix_tk_scaling()
        self.scale = theme.ui_scale(self.root)
        raw_theme = self._setting("theme", "dark")
        self.theme_name = raw_theme if supporter.is_theme_unlocked(raw_theme, getattr(self.mgr, "settings", None)) else "dark"
        self.colors: dict = theme.apply_theme(self.root, self.theme_name)
        self.fonts = theme.make_fonts(self.root)
        self.style = ttk.Style(self.root)
        self.root.report_callback_exception = self._tk_exception
        self._icons = [make_icon(n, self.colors["accent"]) for n in (64, 32, 16)]
        try:
            self.root.iconphoto(True, *self._icons)
        except tk.TclError:
            pass
        self._last_dirty = None
        self.refresh_title()
        self._normal_geom = ""
        zoomed = self._restore_geometry()
        self._build_chrome()
        self.root.deiconify()
        if zoomed:
            self.root.state("zoomed")
        self.root.update_idletasks()
        theme.titlebar_dark(self.root, self.theme_name != "light")   # the native frame exists only once mapped
        self.status("Loading…")
        self.root.update()                       # window visible before the tabs are built
        self._build_tabs()
        self._paths_snap = self._paths_snapshot()
        self.on("active_changed", self._refresh_summary)
        self.on("scanned", self._refresh_summary)
        self.on("settings_changed", self._on_settings_changed)
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._on_configure, add="+")
        self._poll_id = self.root.after(50, self._poll)
        self.root.after(30, self._startup)

    # ---------------------------------------------------------------- helpers
    def _setting(self, key: str, default=None):
        try:
            v = self.mgr.settings.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def px(self, n: float) -> int:
        """Scale a 96-dpi pixel size for the current display."""
        return max(1, round(n * self.scale))

    def _fix_tk_scaling(self) -> None:
        try:
            import ctypes
            dpi = ctypes.windll.user32.GetDpiForSystem()
            if dpi and abs(self.root.winfo_fpixels("1i") - dpi) > 2:
                self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

    # ---------------------------------------------------------------- layout
    def _build_chrome(self) -> None:
        hdr = ttk.Frame(self.root, style="Header.TFrame", padding=(self.px(14), self.px(8)))
        hdr.pack(side="top", fill="x")
        if getattr(self, "_icons", None):
            ttk.Label(hdr, image=self._icons[-1], style="HeaderTitle.TLabel").pack(side="left", padx=(0, self.px(8)))
        ttk.Label(hdr, text=APP_NAME, style="HeaderTitle.TLabel").pack(side="left", padx=(0, self.px(10)))
        self._hdr_badge = ttk.Label(hdr, text="Ready", style="Pill.TLabel")
        self._hdr_badge.pack(side="left", padx=(self.px(4), 0))
        self._supporter_pill = ttk.Label(hdr, text="", style="PillWarn.TLabel")
        self._theme_btn = ttk.Button(
            hdr, text="Light Theme" if self.theme_name != "light" else "Dark Theme",
            style="Header.TButton", command=self._toggle_theme,
        )
        self._theme_btn.pack(side="right", padx=(self.px(4), 0))
        ttk.Button(hdr, text="Rescan", style="Header.TButton", command=self.rescan).pack(side="right", padx=(self.px(4), 0))
        self._support_btn = ttk.Button(
            hdr, text="Support", style="Header.TButton",
            command=lambda: supporter.show_support_dialog(self),
        )
        self._support_btn.pack(side="right", padx=(self.px(4), 0))
        self.refresh_supporter_badge()

        sb = ttk.Frame(self.root, style="Status.TFrame")
        sb.pack(side="bottom", fill="x")
        ttk.Separator(self.root).pack(side="bottom", fill="x")
        self._status_lbl = ttk.Label(sb, style="Status.TLabel", anchor="w")
        self._status_lbl.pack(side="left")
        self._toast_lbl = ttk.Label(sb, style="ToastInfo.TLabel")
        self._busy_frame = ttk.Frame(sb, style="Status.TFrame")
        self._busy_lbl = ttk.Label(self._busy_frame, style="Status.TLabel")
        self._pbar = ttk.Progressbar(self._busy_frame, length=self.px(160), mode="determinate", maximum=100)
        self._cancel_btn = ttk.Button(self._busy_frame, text="Cancel", style="Small.TButton", command=self._cancel_current)
        # pack order = who gets space first on a narrow bar: busy frame > toast > status text; Cancel > bar > label
        self._pbar.pack(side="right", padx=(0, self.px(8)))
        self._busy_lbl.pack(side="left")
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

    def _toggle_theme(self) -> None:
        new_theme = "light" if self.theme_name != "light" else "dark"
        try:
            self.mgr.settings.set("theme", new_theme)
            self.mgr.settings.save()
        except Exception:
            pass
        self.set_theme(new_theme)

    def _build_tabs(self) -> None:
        for title, modname, clsname in TABS:
            self.status(f"Loading {title}…")
            try:
                tab = getattr(importlib.import_module(f"rmm.gui.{modname}"), clsname)(self.notebook, self)
            except Exception:
                tb = traceback.format_exc()
                self.log.error("tab %s failed to load:\n%s", title, tb)
                tab = self._placeholder(title, tb)
            icon = _TAB_ICONS.get(title, "")
            label = f"{icon}  {title}" if icon else title
            self.notebook.add(tab, text=label)
            self.tabs[title] = tab
            self._tab_titles.append(title)
        try:
            last = int(self._setting("last_tab", 0) or 0)
        except (TypeError, ValueError):
            last = 0
        if 0 <= last < len(self._tab_titles):
            self.notebook.select(last)
        self.status("")

    def _placeholder(self, title: str, tb: str) -> ttk.Frame:
        f = ttk.Frame(self.notebook, padding=self.px(20))
        ttk.Label(f, text=f"The {title} tab could not be loaded.", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(f, text="The rest of the app still works. Technical details:",
                  style="Muted.TLabel").pack(anchor="w", pady=(self.px(4), self.px(8)))
        t = tk.Text(f, height=14, wrap="none", font=self.fonts["mono"])
        t.insert("1.0", tb)
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)
        return f

    # ---------------------------------------------------------------- events
    def on(self, event: str, cb) -> None:
        self._handlers.setdefault(event, []).append(cb)

    def emit(self, event: str, *args) -> None:
        if threading.current_thread() is not self._tk_thread:
            self._q.put(("emit", event, args))
            return
        for cb in list(self._handlers.get(event, ())):
            try:
                cb(*args)
            except Exception:
                self.log.exception("handler for event %r failed", event)

    # ---------------------------------------------------------------- background runner
    def run_bg(self, fn, *args, on_done=None, on_error=None, busy: str = "", progress: bool = False,
               cancellable: bool = False, **kw) -> threading.Event:
        cancel = threading.Event()
        job = _Job(getattr(fn, "__name__", "task"), busy, cancel, cancellable, progress)
        if progress:
            kw["progress"] = job.progress
        if cancellable:
            kw["cancel"] = cancel

        def work():
            try:
                res = fn(*args, **kw)
            except BaseException as e:  # noqa: BLE001 — everything is reported on the Tk thread
                self._q.put(("error", job, on_error, e, traceback.format_exc()))
            else:
                self._q.put(("done", job, on_done, res))

        self._jobs.append(job)
        threading.Thread(target=work, daemon=True, name=f"bg-{job.name}").start()
        self._update_busy()
        return cancel

    def _poll(self) -> None:
        try:
            for _ in range(100):
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                self._handle(item)
            self._update_busy()
            if bool(getattr(self.mgr, "dirty", False)) != self._last_dirty:
                self.refresh_title()
                self.emit("dirty_changed", self._last_dirty)
        except Exception:
            self.log.exception("poll loop")
        finally:
            if not self._closing:
                self._poll_id = self.root.after(50, self._poll)

    def _handle(self, item) -> None:
        kind = item[0]
        if kind == "emit":
            self.emit(item[1], *item[2])
            return
        job = item[1]
        if job in self._jobs:
            self._jobs.remove(job)
        try:
            if kind == "done":
                if item[2]:
                    item[2](item[3])
            else:
                self._report_error(job, item[2], item[3], item[4])
        except Exception:
            tb = traceback.format_exc()
            self.log.error("callback of %s failed:\n%s", job.name, tb)
            self.error("Unexpected error", tb.strip().splitlines()[-1])

    @staticmethod
    def _expected(exc: BaseException) -> bool:
        """A failure the user caused or can fix (game running, file exists, no API key): no traceback, no crash dialog."""
        name = type(exc).__name__           # by name: the GUI does not import core / ai for this
        return (name in ("GameRunningError", "ExternalChangeError", "NotEnoughSpaceError") or isinstance(exc, FileExistsError)
                or (isinstance(exc, PermissionError) and bool(str(exc)))
                or (name == "LLMError" and getattr(exc, "kind", "") in ("auth", "config")))

    def _report_error(self, job: _Job, on_error, exc: BaseException, tb: str) -> None:
        what = job.busy or job.name
        cancelled = job.cancel.is_set() or isinstance(exc, InterruptedError)   # a dialog cancel raises this too
        expected = not cancelled and self._expected(exc)
        if cancelled:
            self.log.info("%s cancelled", what)
        elif expected:
            self.log.info("%s: %s: %s", what, type(exc).__name__, exc)
        elif on_error and (isinstance(exc, OSError) or type(exc).__name__ == "LLMError"):
            # expected and handled by the caller (offline, server error): no scary traceback;
            # anything else keeps its traceback even when handled - it may be a real bug
            self.log.warning("%s failed: %s: %s", what, type(exc).__name__, exc)
        else:
            self.log.error("%s failed:\n%s", what, tb)
        if on_error:
            on_error(exc)
        elif cancelled:
            self.toast("Cancelled", "warn")
        elif expected:
            self.toast(str(exc), "warn")
        else:
            title = (job.busy or "Task").rstrip(" .…") + " failed"
            self.error(title, str(exc) or type(exc).__name__)

    def _update_busy(self) -> None:
        jobs = [j for j in self._jobs if j.busy or j.has_progress or j.cancellable]
        if not jobs:
            self._shown_job = None
            if self._busy_state is not None:
                self._busy_state = None
                self._pbar.stop()
                self._busy_frame.pack_forget()
            return
        # newest job that can still be cancelled, else the newest with real progress: Cancel acts on what is shown
        j = self._shown_job = max(reversed(jobs), key=lambda j: (j.cancellable and not j.cancel.is_set(),
                                                                 j.frac is not None))
        frac, text = j.frac, j.text             # read once: the worker thread keeps updating them
        state = (id(j), frac, text, len(jobs), j.cancel.is_set())
        if state == self._busy_state:
            return
        if self._busy_state is None:
            self._busy_frame.pack(side="right", padx=(0, self.px(4)), before=self._toast_lbl
                                  if self._toast_lbl.winfo_manager() else self._status_lbl)
        self._busy_state = state
        label = j.busy or "Working…"
        if j.cancel.is_set():
            label += "  cancelling…"
        elif text and text != label:
            label = f"{label}  {text}"
        if len(label) > 60:
            label = label[:57] + "…"
        if len(jobs) > 1:
            label += f"  (+{len(jobs) - 1} more)"
        self._busy_lbl.configure(text=label)
        if frac is None:
            if str(self._pbar.cget("mode")) != "indeterminate":
                self._pbar.configure(mode="indeterminate")
                self._pbar.start(30)
        else:
            if str(self._pbar.cget("mode")) != "determinate":
                self._pbar.stop()
                self._pbar.configure(mode="determinate")
            self._pbar["value"] = max(0.0, min(1.0, float(frac))) * 100
        if j.cancellable and not j.cancel.is_set():
            self._cancel_btn.pack(side="right", padx=(0, self.px(4)), pady=self.px(2), before=self._pbar)
        else:
            self._cancel_btn.pack_forget()

    def _cancel_current(self) -> None:
        j = self._shown_job
        if j is not None and j in self._jobs and j.cancellable and not j.cancel.is_set():
            j.cancel.set()
            self.toast("Cancelling…", "warn")
        self._update_busy()

    def busy(self) -> bool:
        """True while any background job with a busy label is running."""
        return any(j.busy for j in self._jobs)

    # ---------------------------------------------------------------- status / dialogs
    def status(self, text: str) -> None:
        self._status_lbl.configure(text=text)

    def toast(self, text: str, level: str = "info") -> None:
        style, icon = {"ok": ("ToastOk", "✔"), "success": ("ToastOk", "✔"), "warn": ("ToastWarn", "⚠"),
                       "warning": ("ToastWarn", "⚠"), "error": ("ToastError", "✖")}.get(level, ("ToastInfo", "ℹ"))
        text = " ".join(str(text).split())
        if len(text) > 140:
            text = text[:137] + "…"
        self._toast_lbl.configure(text=f"{icon} {text}", style=style + ".TLabel")
        self._toast_lbl.pack(side="right", padx=(self.px(4), self.px(8)), before=self._status_lbl)
        if self._toast_after:
            self.root.after_cancel(self._toast_after)
        self._toast_after = self.root.after(7000 if style == "ToastError" else 4500, self._hide_toast)
        self.log.info("toast[%s]: %s", level, text)

    def _hide_toast(self) -> None:
        self._toast_after = None
        self._toast_lbl.pack_forget()

    def ask_yes_no(self, title: str, msg: str) -> bool:
        return bool(messagebox.askyesno(title, msg, parent=self.root))

    def ask_yes_no_cancel(self, title: str, msg: str) -> bool | None:
        return messagebox.askyesnocancel(title, msg, parent=self.root)

    def error(self, title: str, msg: str) -> None:
        messagebox.showerror(title, msg, parent=self.root)

    def info(self, title: str, msg: str) -> None:
        messagebox.showinfo(title, msg, parent=self.root)

    def _tk_exception(self, exc, val, tb) -> None:
        text = "".join(traceback.format_exception(exc, val, tb))
        self.log.error("Tk callback error:\n%s", text)
        self.toast(f"Error: {val or exc.__name__}", "error")

    # ---------------------------------------------------------------- navigation
    def select_tab(self, name) -> bool:
        if isinstance(name, int):
            if 0 <= name < len(self._tab_titles):
                self.notebook.select(name)
                return True
            return False
        key = str(name).lower()
        for exact in (True, False):
            for i, t in enumerate(self._tab_titles):
                if (t.lower() == key) if exact else t.lower().startswith(key):
                    self.notebook.select(i)
                    return True
        return False

    def current_tab(self):
        try:
            return self.tabs.get(self._tab_titles[self.notebook.index("current")])
        except (tk.TclError, IndexError):
            return None

    def show_mod_details(self, uid: str | None = None, db_mod=None) -> None:
        self.select_tab("Mods")
        tab = self.tabs.get("Mods")
        if hasattr(tab, "show_mod"):
            tab.show_mod(uid=uid, db_mod=db_mod)

    def queue_download(self, refs: list[ModRef], method: str | None = None, **opts) -> None:
        """Hands refs + Manager.download options (activate, with_deps, update) to the Downloads tab
        ('queue_download' handler); falls back to a direct background download."""
        refs = [r for r in (refs or []) if r]
        if not refs:
            self.toast("Nothing to download")
            return
        if self._handlers.get("queue_download"):
            self.emit("queue_download", refs, method, opts)
            return

        def done(results):
            ok = sum(1 for r in results if r.ok)
            self.toast(f"Downloads finished: {ok}/{len(results)} succeeded", "ok" if ok == len(results) else "warn")
            self.emit("downloads_done", results)
            self.emit("scanned")
            self.emit("active_changed")

        self.run_bg(self.mgr.download, refs, method, progress=True, cancellable=True,
                    busy=f"Downloading {len(refs)} mod(s)…", on_done=done, **opts)

    # ---------------------------------------------------------------- manager actions
    def refresh_title(self) -> None:
        self._last_dirty = bool(getattr(self.mgr, "dirty", False))
        gv = getattr(self.mgr, "game_version", "") or ""
        self.root.title(f"{'* ' if self._last_dirty else ''}{APP_NAME}" + (f" — RimWorld {gv}" if gv else ""))

    def _refresh_summary(self, *_a) -> None:
        """Title + the left status-bar text (game version, installed/active counts)."""
        self.refresh_title()
        m = self.mgr
        if not getattr(m, "game_dir", None):
            self.status("RimWorld not found — set the game folder in Settings")
            if hasattr(self, "_hdr_badge"):
                self._hdr_badge.configure(text="⚠ Game path not set", style="PillWarn.TLabel")
        else:
            self.status(f"RimWorld {m.game_version or '(version unknown)'} · {len(m.installed)} mods installed · "
                        f"{len(m.active)} active")
            if hasattr(self, "_hdr_badge"):
                gv = m.game_version or "RimWorld"
                self._hdr_badge.configure(
                    text=f"● {gv}  ·  {len(m.active)} active / {len(m.installed)} installed",
                    style="PillOk.TLabel" if len(m.active) > 0 else "Pill.TLabel",
                )

    def save_active(self, on_saved=None, force: bool = False, running_ok: bool = False) -> None:
        """Write the active list to ModsConfig.xml in the background (asks before overwriting while RimWorld runs).
        running_ok ('Save anyway') still asks when the game itself changed ModsConfig.xml; force overwrites."""
        def done(backup):
            self.refresh_title()
            self.toast("Saved to ModsConfig.xml" + (" (previous version backed up)" if backup else ""), "ok")
            if on_saved:
                on_saved()

        def failed(exc):
            kind = type(exc).__name__
            if kind == "GameRunningError":
                if self.ask_yes_no("RimWorld is running",
                                   "RimWorld is running and rewrites ModsConfig.xml when it closes, so your "
                                   "changes may be lost.\n\nSave anyway?"):
                    self.save_active(on_saved, running_ok=True)
            elif kind == "ExternalChangeError":
                ans = self.ask_yes_no_cancel("ModsConfig.xml was changed", _EXTERNAL_MSG +
                                             "\n\nYes: overwrite it with the list shown here\n"
                                             "No: discard your changes here and reload the game's list\n"
                                             "Cancel: do nothing")
                if ans:
                    self.save_active(on_saved, force=True)
                elif ans is not None:
                    self.run_bg(self.mgr.load_active_from_game, busy="Reloading active list…",
                                on_done=lambda _: (self.emit("active_changed"),
                                                   self.toast("Reloaded from RimWorld", "ok"),
                                                   on_saved and on_saved()))    # disk == app now: e.g. launch
            elif _not_loaded(exc):
                self.info("Not loaded yet", "Your active mod list has not been read from RimWorld yet, so there is "
                          "nothing to save.\n\nWait for loading to finish (or fix the paths in Settings and press F5).")
            else:
                self.error("Save failed", _save_failed_msg(exc))

        # one save at a time (Ctrl+S auto-repeat, double-click); _handle drops the job before done/failed run,
        # so the 'Save anyway' / 'Overwrite' retries below still get through
        if any(j.busy == "Saving mod list…" for j in self._jobs):
            self.toast("Already saving…", "warn")
            return
        self.run_bg(self.mgr.save_active_to_game, force=force, running_ok=running_ok, on_done=done, on_error=failed,
                    busy="Saving mod list…")

    def _save_sync(self) -> bool:
        """Used on close only (short synchronous write). True = go on closing."""
        kw: dict = {}
        while True:
            try:
                self.mgr.save_active_to_game(**kw)
                return True
            except Exception as e:
                kind = type(e).__name__
                if kind == "GameRunningError" and not kw:
                    if not self.ask_yes_no("RimWorld is running", "RimWorld is running. Save anyway?"):
                        return False
                    kw = {"running_ok": True}          # an edit made in the game's mod menu still asks
                elif kind == "ExternalChangeError" and not kw.get("force"):
                    ans = self.ask_yes_no_cancel("ModsConfig.xml was changed", _EXTERNAL_MSG +
                                                 "\n\nOverwrite it with your list?  (No closes without saving and "
                                                 "keeps the game's version.)")
                    if ans is None:
                        return False
                    if not ans:
                        return True
                    kw = {"force": True}
                else:
                    if isinstance(e, PermissionError):
                        self.log.info("save on close failed: %s", e)
                    else:
                        self.log.exception("save on close failed")
                    self.error("Save failed", _save_failed_msg(e))
                    return False

    def undo(self) -> None:
        if self.mgr.undo():
            self.emit("active_changed")
            self.toast("Undone")
        else:
            self.toast("Nothing to undo")

    def redo(self) -> None:
        if self.mgr.redo():
            self.emit("active_changed")
            self.toast("Redone")
        else:
            self.toast("Nothing to redo")

    def rescan(self) -> None:
        if self._scanning:
            return
        if not getattr(self.mgr, "_loaded", True):   # startup failed before ModsConfig.xml was read: redo all of it
            self._startup()
            return
        self._scanning = True

        def done(_):
            self._scanning = False
            self.emit("scanned")
            self.toast(f"Found {len(self.mgr.installed)} installed mods", "ok")

        def failed(exc):
            self._scanning = False
            self.error("Scan failed", str(exc))

        self.run_bg(self.mgr.scan, progress=True, busy="Scanning mods…", on_done=done, on_error=failed)

    def update_db(self, force: bool = False, was_empty: bool = False, on_done=None, on_error=None,
                  on_progress=None) -> None:
        """The one entry point for mod-database updates (startup, Workshop, Tools): one job, one Cancel.
        A request while an update runs joins it. The App toasts, reports errors and emits 'db_updated'; the hooks are
        only for the caller's own UI: on_done(summary) / on_error(exc) on the Tk thread when the running update ends,
        on_progress(fraction, text) on the worker thread."""
        if self._db_hooks is not None:
            self._db_hooks.append((on_done, on_error, on_progress))
            self.toast("The mod database is already updating")
            return
        hooks = self._db_hooks = [(on_done, on_error, on_progress)]

        def work(progress, cancel):
            def prog(fraction=None, text=""):
                progress(fraction, text)
                for h in list(hooks):
                    if h[2]:
                        h[2](fraction, text)
            return self.mgr.update_db(progress=prog, cancel=cancel, force=force)

        def finish(i, arg):
            self._db_hooks = None
            for h in hooks:
                if h[i]:
                    h[i](arg)

        def done(summary):
            first = _first_line(summary) or "Mod database updated"
            if cancel.is_set():
                self.toast("Database update cancelled", "warn")
            elif "failed" in first.lower():     # the old DB is kept when every download fails: not a success
                self.toast("Mod database not updated (no internet?): keeping the existing one, "
                           "it retries at the next start", "warn")
            else:
                self.toast(first, "ok")
            self.emit("db_updated")
            finish(0, summary)

        def failed(exc):
            try:
                empty = was_empty or self.mgr.db.is_empty()
            except Exception:
                empty = was_empty
            if cancel.is_set():
                self.toast("Database update cancelled", "warn")
            elif empty:      # the technical summary (URLs, urlopen errors) is in the log only
                self.error("Mod database", "Couldn't download the mod database (no internet?).\n\n"
                           "Managing your mods still works. Workshop search and the AI generator need the "
                           "database: it retries at the next start, or use Tools → Database to retry or import a "
                           "steamDB.json file.\n\nDetails are in the log file (Settings → App).")
            else:
                self.toast(f"Database update failed (keeping the old one): {_first_line(exc)}", "warn")
            finish(1, exc)

        cancel = self.run_bg(work, progress=True, cancellable=True, busy="Updating mod database…",
                             on_done=done, on_error=failed)

    def refresh_supporter_badge(self) -> None:
        lic = supporter.get_supporter_license(getattr(self.mgr, "settings", None))
        if hasattr(self, "_support_btn"):
            self._support_btn.configure(text="Supporter" if lic.valid else "Support")
        if hasattr(self, "_supporter_pill"):
            if lic.valid:
                self._supporter_pill.configure(text=f"★ {lic.tier_name} ({lic.holder})")
                if not self._supporter_pill.winfo_manager():
                    self._supporter_pill.pack(side="left", padx=(self.px(6), 0))
            else:
                self._supporter_pill.pack_forget()

    def set_theme(self, name: str) -> None:
        if not supporter.is_theme_unlocked(name, getattr(self.mgr, "settings", None)):
            name = "dark"
        if name == self.theme_name:
            return
        self.theme_name = name
        self.colors.clear()
        self.colors.update(theme.apply_theme(self.root, name))   # same dict object: tabs keep a live reference
        if hasattr(self, "_theme_btn"):
            self._theme_btn.configure(text="Light Theme" if name != "light" else "Dark Theme")
        self.emit("theme_changed", name)

    # ---------------------------------------------------------------- startup
    def _startup(self) -> None:
        self._scanning = True                     # F5 waits for it
        self.run_bg(self._startup_work, progress=True, busy="Loading mods…",
                    on_done=self._startup_done, on_error=self._startup_failed)

    def _startup_work(self, progress):
        progress(None, "Detecting RimWorld…")
        info = self.mgr.detect_paths()
        progress(None, "Scanning mods…")
        self.mgr.scan(progress=progress)
        progress(None, "Reading ModsConfig.xml…")
        self.mgr.load_active_from_game()
        try:
            from rmm import textures
            textures.recover()          # a texture optimization the app was closed during left half-written files
        except Exception:
            self.log.exception("texture cleanup failed")
        db_state = None
        try:
            days = float(self._setting("db_auto_update_days", 3) or 0)
            if self.mgr.db.is_empty():
                db_state = "empty"
            elif days > 0 and self.mgr.db.needs_update(days):
                db_state = "stale"
        except Exception:
            self.log.exception("database status check failed")
        return info, db_state

    def _startup_done(self, res) -> None:
        _info, db_state = res
        self._scanning = False
        self._paths_snap = self._paths_snapshot()
        self.emit("scanned")
        self.emit("active_changed")
        if not self.mgr.game_dir:
            self.info("RimWorld not found",
                      "The RimWorld game folder could not be found automatically.\n\n"
                      "Open Settings → Paths and choose the folder that contains RimWorldWin64.exe "
                      "(or press Auto-detect after installing the game).")
            self.select_tab("Settings")
        elif not self.mgr.config_dir:
            self.toast("RimWorld's config folder was not found — saving needs it (Settings → Paths)", "warn")
        if getattr(getattr(self.mgr, "settings", None), "_load_failed", False):
            self.toast("settings.json could not be read: using defaults, changes are not saved (restart the app)",
                       "warn")
        if db_state:
            self.update_db(was_empty=db_state == "empty")
        if self.mgr.game_dir and self._setting("check_updates_on_start", False):
            dl = self.tabs.get("Downloads")
            if hasattr(dl, "check_updates"):
                dl.check_updates(quiet=True)

    def _startup_failed(self, exc) -> None:
        self._scanning = False
        self.error("Startup problem", f"Loading your mods failed:\n{exc}\n\nCheck the paths in Settings, then press F5.")
        self.emit("scanned")
        self.emit("active_changed")
        self.status("Startup failed — see Settings")

    def _paths_snapshot(self) -> tuple:
        return tuple(str(self._setting(k, "")) for k in PATH_KEYS)

    def _on_settings_changed(self, *_a) -> None:
        self.refresh_supporter_badge()
        self.set_theme(self._setting("theme", "dark"))
        snap = self._paths_snapshot()
        if snap == self._paths_snap:
            return
        self._paths_snap = snap

        def work(progress):
            self.mgr.detect_paths()
            self.mgr.scan(progress=progress)
            if not self.mgr.dirty:
                self.mgr.load_active_from_game()

        def done(_):
            self.emit("scanned")
            self.emit("active_changed")
            self.toast("Paths changed — mods rescanned", "ok")

        self.run_bg(work, progress=True, busy="Re-detecting paths…", on_done=done)

    # ---------------------------------------------------------------- keyboard
    def _bind_keys(self) -> None:
        for seqs, fn in ((("<Control-s>", "<Control-S>"), self._key_save),
                         (("<Control-z>", "<Control-Z>"), self._key_undo),
                         (("<Control-y>", "<Control-Y>", "<Control-Shift-Z>"), self._key_redo),
                         (("<Control-f>", "<Control-F>"), self._key_find),
                         (("<F5>",), lambda e: self.rescan())):
            for s in seqs:
                self.root.bind(s, fn)

    def _in_text_field(self) -> bool:
        try:
            w = self.root.focus_get()
        except (KeyError, tk.TclError):         # focus inside a combobox popdown
            return True
        return w is not None and w.winfo_class() in _TEXT_CLASSES

    def _key_save(self, _e=None):
        """Ctrl+S saves what the current tab edits (tab.save_shortcut), else the active mod list."""
        tab = self.current_tab()
        if hasattr(tab, "save_shortcut"):
            tab.save_shortcut()
        else:
            mods = self.tabs.get("Mods")
            (mods.save if hasattr(mods, "save") else self.save_active)()
        return "break"

    def _undo_keys_apply(self) -> bool:
        """Ctrl+Z/Y undo the active list, which only the Mods tab edits (not text fields, not other tabs' lists)."""
        return self.current_tab() is self.tabs.get("Mods") and not self._in_text_field()

    def _key_undo(self, _e=None):
        if self._undo_keys_apply():
            self.undo()

    def _key_redo(self, _e=None):
        if self._undo_keys_apply():
            self.redo()

    def _key_find(self, _e=None):
        """Only tabs with a real search box (focus_search); elsewhere a no-op, never some path field."""
        tab = self.current_tab()
        if hasattr(tab, "focus_search"):
            tab.focus_search()
        return "break"

    # ---------------------------------------------------------------- window geometry / close
    def _restore_geometry(self) -> bool:
        raw = str(self._setting("window_geometry", "") or "")
        zoomed = raw.endswith(";zoomed")
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", raw.split(";")[0].strip())
        vx, vy, vw, vh = self._virtual_screen()
        if m:
            w, h, x, y = map(int, m.groups())
            on_screen = vx - w + 120 <= x <= vx + vw - 120 and vy <= y <= vy + vh - 80
            # fit the monitor it lands on (not all monitors together), or the status bar ends up off screen
            ax, ay, aw, ah = self._work_area((x, y, w, h) if on_screen else None)
            ah -= self.px(40)            # geometry is the client area: leave room for the title bar
            w, h = min(w, aw), min(h, ah)
            if on_screen:
                x, y = max(ax, min(x, ax + aw - w)), max(ay, min(y, ay + ah - h))
            else:
                x, y = ax + (aw - w) // 2, ay + max(0, (ah - h) // 3)
        else:
            ax, ay, aw, ah = self._work_area()
            ah -= self.px(40)
            w, h = min(self.px(1280), aw - 40), min(self.px(800), ah)
            x, y = ax + (aw - w) // 2, ay + max(0, (ah - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        # never taller than the work area, or the status bar (Cancel) ends up under the taskbar
        self.root.minsize(min(self.px(900), aw), min(self.px(520), ah))
        self._normal_geom = f"{w}x{h}+{x}+{y}"
        return zoomed

    def _virtual_screen(self) -> tuple[int, int, int, int]:
        try:
            import ctypes
            gsm = ctypes.windll.user32.GetSystemMetrics
            v = (gsm(76), gsm(77), gsm(78), gsm(79))
            if v[2] > 0 and v[3] > 0:
                return v
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _work_area(self, rect=None) -> tuple[int, int, int, int]:
        """Primary monitor (or the one nearest rect=(x, y, w, h)) minus the taskbar: (x, y, w, h)."""
        try:
            import ctypes
            import ctypes.wintypes as wt
            if rect:
                class _MI(ctypes.Structure):
                    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                                ("dwFlags", wt.DWORD)]
                u32 = ctypes.windll.user32
                u32.MonitorFromRect.restype = ctypes.c_void_p
                u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MI)]
                x, y, w, h = rect
                mon = u32.MonitorFromRect(ctypes.byref(wt.RECT(x, y, x + w, y + h)), 2)  # MONITOR_DEFAULTTONEAREST
                mi = _MI(cbSize=ctypes.sizeof(_MI))
                if mon and u32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                    r = mi.rcWork
                    return r.left, r.top, r.right - r.left, r.bottom - r.top
            r = wt.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
                return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight() - 40

    def _on_configure(self, e) -> None:
        if e.widget is self.root:
            try:
                if self.root.state() == "normal":
                    self._normal_geom = self.root.geometry()
            except tk.TclError:
                pass

    def _persist_window(self) -> None:
        try:
            s = self.mgr.settings
            zoomed = self.root.state() == "zoomed"
            s.set("window_geometry", (self._normal_geom or self.root.geometry()) + (";zoomed" if zoomed else ""))
            s.set("last_tab", self.notebook.index("current"))
            s.save()
        except Exception:
            self.log.exception("saving window state failed")

    def close(self) -> None:
        settings = self.tabs.get("Settings")
        store = False
        if getattr(settings, "is_dirty", lambda: False)():
            store = self.ask_yes_no_cancel("Unsaved settings", "You changed settings without saving them.\n\n"
                                           "Save them before closing?")
            if store is None:
                return
        if getattr(self.mgr, "dirty", False):
            ans = self.ask_yes_no_cancel("Unsaved changes", "Your active mod list has unsaved changes.\n\n"
                                         "Save them to RimWorld before closing?")
            if ans is None or (ans and not self._save_sync()):
                return
        running = [j for j in self._jobs if j.busy]
        if running and not self.ask_yes_no("Tasks running", f"“{running[-1].busy.rstrip('…')}” is still running.\n\n"
                                           "Quit anyway? It will be stopped."):
            return
        if store:                         # only now: a Cancel above must leave the settings page untouched
            settings.store()
            try:                          # saved here, not in _persist_window: a failure must keep the app open
                self.mgr.settings.save()
            except Exception as e:
                self.log.exception("saving settings failed")
                self.error("Settings not saved", f"Could not save settings:\n{e}")
                return
        for title, tab in self.tabs.items():      # pending autosaves (Modlists) must land before teardown
            try:
                getattr(tab, "flush", lambda: None)()
            except Exception as e:
                self.log.exception("flushing the %s tab failed", title)
                if not self.ask_yes_no("Save failed", f"{title}: {e}\n\nClose anyway and lose these changes?"):
                    return                # before the jobs are cancelled: everything keeps running
        for j in self._jobs:
            j.cancel.set()
        self._persist_window()
        try:
            getattr(self.mgr, "close", lambda: None)()    # kills steamcmd / Steamworks helpers, closes the DB
        except Exception:
            self.log.exception("closing the manager failed")
        self.teardown()

    def teardown(self) -> None:
        """Stop the poll loop and destroy the window (no confirmation)."""
        self._closing = True
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):   # every tab's pending after()
            self.root.tk.call("after", "cancel", job)    # (their Tcl commands go away with their widgets)
        self.root.destroy()


def main() -> int:
    set_dpi_awareness()
    try:    # own taskbar group + our icon instead of python(w).exe's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_ID}.{APP_ID}")
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    try:
        from rmm.core import Manager
        mgr = Manager()
    except Exception as e:
        tb = traceback.format_exc()
        try:        # the configured logger: 'rmm' has no file handler yet if Manager() failed early
            from rmm.config import get_logger
            get_logger().error("startup failed:\n%s", tb)
        except Exception:
            pass
        messagebox.showerror(APP_NAME, f"{APP_NAME} failed to start:\n\n{e}\n\n{tb[-1500:]}", parent=root)
        root.destroy()
        return 1
    app = App(mgr, root)
    try:    # a hidden parent shell (SW_HIDE startupinfo) swallows Tk's first ShowWindow in deiconify()
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        ctypes.windll.user32.ShowWindow(hwnd, 3 if str(app._setting("window_geometry", "")).endswith(";zoomed") else 5)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    root.mainloop()
    return 0


def _selftest() -> None:
    """Headless-ish check of the runner, events, icon and tab placeholders with a stub manager."""
    import time

    class _S(dict):
        def get(self, k, d=None):
            return super().get(k, d)

        def set(self, k, v):
            self[k] = v

        def save(self):
            self["saved"] = True

    class _Db:
        def is_empty(self):
            return False

        def needs_update(self, _d):
            return False

    quiet = logging.getLogger("rmm.selftest")        # expected errors below must not reach the app log
    quiet.propagate = False
    quiet.addHandler(logging.NullHandler())

    class _M:
        settings, db, dirty, installed, active = _S(), _Db(), False, {}, []
        game_dir = config_dir = workshop_dir = None
        game_version = version_short = ""
        log = quiet

        def detect_paths(self):
            return {}

        def scan(self, progress=None):
            return {}

        def load_active_from_game(self):
            return []

        def undo(self):
            return False

        redo = undo

    global TABS
    saved, TABS = TABS, [("Mods", "no_such_module", "X"), ("AI Generator", "no_such_module", "Y"),
                         ("Settings", "no_such_module", "Z")]          # placeholders only, independent of other tabs
    root = tk.Tk()
    mgr = _M()
    mgr.game_dir = mgr.config_dir = "x"         # suppress the "not found" dialog and config toast during async tests
    try:
        app = App(mgr, root)
    finally:
        TABS = saved
    app.set_theme("archotech")                  # locked supporter theme must fall back to dark without a valid key
    assert app.theme_name == "dark"
    app.error = app.info = lambda title, msg: (_ for _ in ()).throw(AssertionError(f"{title}: {msg}"))
    try:
        got = []
        app.on("x", lambda *a: got.append(a))
        app.emit("x", 1, 2)
        threading.Thread(target=app.emit, args=("x", 3)).start()
        ev = app.run_bg(lambda a, progress, cancel: (progress(0.5, "half"), a * 2)[1], 21,
                        on_done=got.append, busy="t", progress=True, cancellable=True)
        errs, recs = [], []
        rec_h = logging.Handler()
        rec_h.emit = lambda r: r.msg.startswith("toast") or recs.append(r)
        quiet.addHandler(rec_h)
        quiet.setLevel(logging.INFO)
        app.run_bg(lambda: 1 / 0, on_error=errs.append)
        end = time.time() + 3
        while time.time() < end and (len(got) < 3 or not errs):
            root.update()
            time.sleep(0.01)
        assert (1, 2) in got and (3,) in got and 42 in got, got
        assert isinstance(errs[0], ZeroDivisionError) and isinstance(ev, threading.Event)
        # a real bug keeps its ERROR traceback even when handled; a cancel / handled name clash does not
        assert [r.levelno for r in recs] == [logging.ERROR], recs
        recs.clear()

        def boom(e):
            raise e
        app.run_bg(boom, InterruptedError("cancelled"), on_error=errs.append)
        app.run_bg(boom, FileExistsError("exists"), on_error=errs.append)
        end = time.time() + 3
        while time.time() < end and len(errs) < 3:
            root.update()
            time.sleep(0.01)
        assert len(errs) == 3 and [r.levelno for r in recs] == [logging.INFO, logging.INFO], recs
        recs.clear()
        GameRunningError = type("GameRunningError", (RuntimeError,), {})
        toasts, real_toast = [], app.toast
        app.toast = lambda t, k="info": toasts.append((t, k))
        app.run_bg(boom, GameRunningError("close it"))                  # no on_error: a toast, not a crash dialog
        end = time.time() + 3
        while time.time() < end and not toasts:
            root.update()
            time.sleep(0.01)
        app.toast = real_toast
        assert toasts == [("close it", "warn")] and [r.levelno for r in recs] == [logging.INFO], (toasts, recs)
        assert "read-only" in _save_failed_msg(PermissionError("denied")) and _save_failed_msg(OSError("x")) == "x"
        quiet.removeHandler(rec_h)
        assert app.select_tab("settings") and app.select_tab("ai") and not app.select_tab("nope")
        assert len(app.tabs) == 3 and "could not be loaded" in str(app.tabs["Mods"].winfo_children()[0].cget("text"))
        app.toast("hello", "ok")
        mgr.dirty = True
        root.update()
        root.after(60, lambda: None)
        time.sleep(0.07)
        root.update()
        assert root.title().startswith("* ")
        mgr.dirty = False
        app._persist_window()
        assert mgr.settings.get("saved") and "x" in mgr.settings["window_geometry"]

        def pump(cond):
            end = time.time() + 3
            while time.time() < end and not cond():
                root.update()
                time.sleep(0.01)

        # Ctrl+S saves what the current tab edits; Ctrl+Z only undoes the active list on the Mods tab
        calls = []
        app.tabs["Settings"].save_shortcut = lambda: calls.append("settings")
        app.tabs["Mods"].save = lambda: calls.append("mods")
        mgr.undo = lambda: calls.append("undo") or False
        for tab, keys in (("Settings", "su"), ("AI Generator", "s"), ("Mods", "u")):
            app.select_tab(tab)
            root.update()
            for k in keys:
                (app._key_save if k == "s" else app._key_undo)()
        assert calls == ["settings", "mods", "undo"], calls

        # one DB update at a time: a second request joins the running job and gets its hooks called
        gate, seen = threading.Event(), []

        def upd(progress, cancel, force):
            progress(0.5, "half")
            gate.wait(3)
            return mgr.summary
        mgr.update_db, mgr.summary = upd, "Steam Workshop DB: 5 mods\nFinished in 1s"
        app.update_db(on_done=lambda s: seen.append("a"), on_progress=lambda f, t: seen.append(f))
        app.update_db(force=True, on_done=lambda s: seen.append("b"))
        assert sum(j.cancellable for j in app._jobs) == 1
        gate.set()
        pump(lambda: "b" in seen)
        assert seen == [0.5, "a", "b"] and "5 mods" in app._toast_lbl.cget("text"), seen
        mgr.summary = "Steam Workshop DB: network failed (codeload zip: <urlopen error x>)\nFinished in 1s"
        app.update_db(on_done=seen.append)
        pump(lambda: mgr.summary in seen)
        assert str(app._toast_lbl.cget("style")) == "ToastWarn.TLabel"       # offline is not a green success

        # a narrow status bar with long texts still shows Cancel: the busy frame and Cancel get space first
        root.minsize(1, 1)
        root.geometry("420x300")
        gate = threading.Event()
        app.run_bg(lambda progress, cancel: (progress(0.3, "x" * 300), gate.wait(3)), progress=True,
                   cancellable=True, busy="Downloading…")
        app.status("s" * 200)
        app.toast("t" * 200)
        pump(lambda: app._cancel_btn.winfo_ismapped())
        assert app._cancel_btn.winfo_ismapped()
        gate.set()
        pump(lambda: not app._jobs)

        # the Downloads tab gets Manager.download's options with the refs
        queued = []
        app.on("queue_download", lambda refs, method, opts: queued.append((len(refs), method, opts)))
        app.queue_download([ModRef(workshop_id="1")], activate=True)
        assert queued == [(1, None, {"activate": True})], queued

        # saving: ModsConfig.xml changed outside the app / not loaded yet
        class ExternalChangeError(Exception):
            pass
        writes, infos = [], []

        class GameRunningError(Exception):
            pass

        def sat(force=False, running_ok=False):
            writes.append(force)
            if not force:
                raise ExternalChangeError("changed")
        mgr.save_active_to_game = sat
        app.ask_yes_no_cancel = lambda t, m: True                      # overwrite
        assert app._save_sync() and writes == [False, True]
        app.ask_yes_no_cancel = lambda t, m: False                     # keep the game's file, still close
        writes.clear()
        assert app._save_sync() and writes == [False]
        # RimWorld running + 'Save anyway' must still ask about the game's own edit (only Overwrite forces)
        calls, asked = [], []

        def sat2(force=False, running_ok=False):
            calls.append((force, running_ok))
            if not (force or running_ok):
                raise GameRunningError("running")
            if not force:
                raise ExternalChangeError("changed")
        mgr.save_active_to_game, app.ask_yes_no = sat2, lambda t, m: True
        app.ask_yes_no_cancel = lambda t, m: asked.append(t)            # None = Cancel
        assert not app._save_sync() and calls == [(False, False), (False, True)] and asked, (calls, asked)
        calls.clear()
        app.save_active()
        pump(lambda: len(asked) > 1)
        assert calls == [(False, False), (False, True)], calls
        app.ask_yes_no_cancel = lambda t, m: False
        app.info = lambda t, m: infos.append(t)
        mgr.save_active_to_game = lambda force=False, running_ok=False: (_ for _ in ()).throw(RuntimeError("not been loaded yet"))
        app.save_active()
        pump(lambda: infos)
        assert infos == ["Not loaded yet"]
        # 'No: reload the game's list' still runs what the save was for (e.g. launching the game)
        steps = []
        mgr.save_active_to_game = sat
        mgr.load_active_from_game = lambda: steps.append("reload") or []
        app.save_active(on_saved=lambda: steps.append("launch"))
        pump(lambda: "launch" in steps)
        assert steps == ["reload", "launch"], steps
        # one save at a time: a repeated Ctrl+S while saving does not start a second write
        gate, writes = threading.Event(), []
        mgr.save_active_to_game = lambda force=False, running_ok=False: (writes.append(1), gate.wait(3))
        app.save_active()
        app.save_active()
        gate.set()
        pump(lambda: not app._jobs)
        assert writes == [1] and "Saved" in app._toast_lbl.cget("text"), writes

        # F5 after a startup that never read ModsConfig.xml reruns the whole startup
        mgr._loaded, steps[:] = False, []
        mgr.detect_paths = lambda: steps.append("detect") or {}
        app.rescan()
        pump(lambda: not app._scanning)
        assert steps == ["detect", "reload"] and not app._scanning, steps
        del mgr._loaded

        # close: unsaved settings are stored, tab autosaves flushed, then the manager stops its helpers
        order, real_teardown = [], app.teardown
        app.tabs["Settings"].is_dirty = lambda: True
        app.tabs["Settings"].store = lambda: order.append("store")
        app.tabs["AI Generator"].flush = lambda: order.append("flush")
        mgr.close = lambda: order.append("mgr.close")
        mgr.dirty, app.ask_yes_no_cancel = True, lambda t, m: None if t == "Unsaved changes" else True
        app.close()                               # Yes to settings, Cancel to the mod list: settings untouched
        assert order == [], order
        mgr.dirty = False
        app.ask_yes_no_cancel = lambda t, m: True
        app.teardown = lambda: order.append("teardown")
        # settings that cannot be written, or a failed autosave the user will not lose: the app stays open
        real_save, shown = mgr.settings.save, []
        mgr.settings.save = lambda: (_ for _ in ()).throw(PermissionError("read-only"))
        app.error = lambda t, m: shown.append(t)
        app.close()
        assert order == ["store"] and shown == ["Settings not saved"], (order, shown)
        mgr.settings.save, order[:] = real_save, []
        app.tabs["Mods"].flush = lambda: (_ for _ in ()).throw(OSError("locked"))
        app.ask_yes_no = lambda t, m: shown.append(t) and False
        app.close()
        assert order == ["store"] and shown[-1] == "Save failed", (order, shown)
        del app.tabs["Mods"].flush
        order[:] = []
        app.close()
        app.teardown = real_teardown
        assert order == ["store", "flush", "mgr.close", "teardown"], order
    finally:
        app.teardown()


if __name__ == "__main__":
    _selftest()
    print("app ok")
