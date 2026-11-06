"""Small GUI helpers shared by the tabs (startup guard, text styling, placeholders, sortable trees, relative times)."""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from rmm.gui.theme import ui_scale

VERSIONS = ["1.7", "1.6", "1.5", "1.4", "1.3", "1.2", "1.1", "1.0"]


def track_ready(tab) -> None:
    """tab._ready turns True at the first 'scanned' (startup scan + ModsConfig load done, or startup failed).
    Before that installed == {} and the active list is not loaded: every mod looks missing."""
    tab._ready = False
    tab.app.on("scanned", lambda *_: setattr(tab, "_ready", True))


def not_ready(tab) -> bool:
    """Guard for list-changing / download actions: True (with a toast) while the startup load still runs."""
    if tab._ready:
        return False
    tab.app.toast("Still loading your mods — try again in a moment", "warn")
    return True


def style_text(w: tk.Widget, c: dict) -> None:
    """Colour a classic tk Text/Listbox to match the ttk theme (option_add only applies at creation)."""
    opts = dict(background=c["field"], foreground=c["text"], selectbackground=c["select"],
                selectforeground=c.get("select_text", c["text"]), highlightthickness=1, highlightbackground=c["border"],
                highlightcolor=c["accent"], relief="flat", borderwidth=0)
    if isinstance(w, tk.Text):
        opts["insertbackground"] = c["text"]
    w.configure(**opts)


def add_placeholder(widget: tk.Widget, text: str, app):
    """Muted hint drawn over an Entry/Text while it is empty (like HTML placeholders it stays visible while focused).
    Returns refresh() for programmatic edits."""
    lbl = tk.Label(widget, text=text, anchor="nw", justify="left", cursor="xterm", padx=0, pady=0, borderwidth=0)
    lbl._rmm_custom_colors = True  # theme.py's live re-colour must skip it; recolor() below handles it
    is_text = isinstance(widget, tk.Text)

    def recolor(*_):
        if not lbl.winfo_exists():  # placeholder of a closed dialog: the theme handler outlives it
            return
        try:
            f = widget.cget("font")
        except tk.TclError:
            f = ""
        lbl.configure(bg=app.colors["field"], fg=app.colors["muted"], font=f or "TkDefaultFont")

    def refresh(*_):
        if not lbl.winfo_exists():
            return
        if not (widget.get("1.0", "end-1c") if is_text else widget.get()):
            box = widget.bbox("1.0" if is_text else 0)   # where the first typed character goes (valid once laid out)
            x, y = (box[0], box[1]) if box and box[0] else (6, 4)
            lbl.place(x=x, y=y, bordermode="outside")
        else:
            lbl.place_forget()

    def later(_e=None):  # after the key/paste/layout has been applied
        widget.after_idle(refresh)

    for seq in ("<Key>", "<<Paste>>", "<<Cut>>", "<FocusOut>", "<Configure>"):
        widget.bind(seq, later, add="+")
    lbl.bind("<Button-1>", lambda e: widget.focus_set())
    app.on("theme_changed", recolor)
    recolor()
    later()
    return refresh


def ago(ts: float) -> str:
    if not ts:
        return "never"
    d = max(0.0, time.time() - ts)
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if d >= size:
            n = int(d // size)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def scrolled_tree(parent, cols: list[tuple[str, str, int, bool]], sortable: bool = True,
                  height: int = 6) -> tuple[ttk.Frame, ttk.Treeview]:
    """Treeview + vertical scrollbar in a frame. cols: (id, heading, width in 96-dpi px, stretch)."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", selectmode="extended", height=height)
    k = ui_scale(parent)     # fonts grow with the display scale
    for cid, head, width, stretch in cols:
        tree.heading(cid, text=head, anchor="w")
        tree.column(cid, width=round(width * k), minwidth=round(30 * k), stretch=stretch, anchor="w")
        if sortable:
            tree.heading(cid, command=lambda c=cid: sort_tree(tree, c))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)
    return frame, tree


def sort_key(v: str):
    s = v.lstrip("×").replace(",", "").strip()
    try:
        return (0, float(s), "")
    except ValueError:
        return (1, 0.0, v.lower())


def sort_tree(tree: ttk.Treeview, col: str) -> None:
    """Heading click: sort by that column (numbers numerically, before text); a second click reverses."""
    rows = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
    desc = getattr(tree, "_sort_state", None) == (col, False)
    rows.sort(key=lambda r: sort_key(r[0]), reverse=desc)
    for i, (_v, iid) in enumerate(rows):
        tree.move(iid, "", i)
    tree._sort_state = (col, desc)


def _selftest() -> None:
    assert sort_key("×3") < sort_key("×12") < sort_key("abc")   # numbers sort numerically, before text
    assert ago(0) == "never" and ago(time.time() - 7300) == "2 hours ago"
