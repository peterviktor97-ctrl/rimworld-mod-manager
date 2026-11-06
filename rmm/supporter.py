"""Asymmetric elliptic-curve (secp256k1 Schnorr + PBKDF2) Supporter Key verification and Support UI dialog.

Why this cannot be keygenned from the distributed .exe:
- Only the public curve point Q = (PUB_X, PUB_Y) is shipped in this module.
- Signing requires the 256-bit private scalar `d` (where Q = d * G on secp256k1), which lives only on the author's
  machine (`~/.rmm_supporter_master.key`, used by `tools/supporter_keygen.py`) and is never bundled into the app.
- Forging a key without `d` requires solving the elliptic-curve discrete logarithm problem on secp256k1.
- Keys are stored encrypted with Windows DPAPI (`Settings.set_secret('supporter_key', ...)`), and every theme/perk
  check re-verifies the elliptic-curve signature rather than trusting a plain boolean in `settings.json`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from dataclasses import dataclass

# Author support page links (shown in the Support dialog and Settings -> Supporter & Perks)
KOFI_URL = "https://ko-fi.com/theexpotentialrobloxian29574"
ITCH_URL = "https://therexpo.itch.io/rimmodmanager-fast-mod-manager-ai-modlist-builder"
PATREON_URL = "https://patreon.com"
REPO_URL = "https://github.com/peterviktor97-ctrl/rimworld-mod-manager"

# secp256k1 domain parameters
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_G = (_GX, _GY)

# Production public key Q = d * G (private key d is kept offline in ~/.rmm_supporter_master.key)
PUB_X = 0xD0046ECEC810DF7021702F9C4AD9DF3F9A13876F769DB4B18C70AA0E8726CB5B
PUB_Y = 0x509A463BE71CDF50CC6F894832472FE7C0676D5A2CB531F3EA3B00458A374EC6
PRODUCTION_PUBKEY = (PUB_X, PUB_Y)

_EPOCH_2025 = 1735689600  # 2025-01-01 00:00:00 UTC
_PBKDF2_ITERS = 12_000
_SALT = b"RMM-Supporter-Schnorr-secp256k1-v1"
FLAG_SELFTEST = 0x01

TIER_NAMES = {
    0: "Colony Supporter",
    1: "Archotech Supporter",
    2: "Glitterworld Founder",
}

SUPPORTER_THEMES = ("archotech", "glitterworld", "bloodmoon", "oled")


@dataclass(frozen=True)
class SupporterLicense:
    valid: bool
    key_id: int = 0
    tier_id: int = 0
    tier_name: str = ""
    holder: str = ""
    issued_date: str = ""
    proof_token: str = ""
    error: str = ""


# ---------------------------------------------------------------- elliptic-curve arithmetic
def _pt_on_curve(pt: tuple[int, int] | None) -> bool:
    if pt is None:
        return False
    x, y = pt
    return 0 < x < _P and 0 < y < _P and (y * y - (pow(x, 3, _P) + 7)) % _P == 0


def _pt_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % _P == 0 or y1 == 0:
            return None
        m = (3 * x1 * x1) * pow(2 * y1, -1, _P) % _P
    else:
        m = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    y3 = (m * (x1 - x3) - y1) % _P
    return (x3, y3)


def _pt_mul(k: int, pt: tuple[int, int] = _G) -> tuple[int, int] | None:
    k %= _N
    if k == 0 or pt is None:
        return None
    res = None
    cur: tuple[int, int] | None = pt
    while k > 0:
        if k & 1:
            res = _pt_add(res, cur)
        cur = _pt_add(cur, cur)
        k >>= 1
    return res


def _challenge(rx: int, qx: int, payload: bytes) -> int:
    material = rx.to_bytes(32, "big") + qx.to_bytes(32, "big") + payload
    digest = hashlib.pbkdf2_hmac("sha256", material, _SALT, _PBKDF2_ITERS, dklen=16)
    return int.from_bytes(digest, "big") % _N


# ---------------------------------------------------------------- encoding / verification
def _clean_key(raw: str) -> str:
    s = re.sub(r"[\s\-_]+", "", (raw or "").strip()).upper()
    if s.startswith("RMM1"):
        s = s[4:]
    # Normalize common human lookalike typos in Base32 (0 -> O, 1 -> I, 8 -> B)
    return s.translate(str.maketrans({"0": "O", "1": "I", "8": "B"}))


def _format_key(raw_bytes: bytes) -> str:
    b32 = base64.b32encode(raw_bytes).decode("ascii").rstrip("=")
    groups = [b32[i:i + 5] for i in range(0, len(b32), 5)]
    return "RMM1-" + "-".join(groups)


def sign_supporter_payload(private_key: int, key_id: int, holder: str = "Supporter",
                           tier_id: int = 1, issued_day: int | None = None,
                           selftest: bool = False) -> str:
    """Create a signed `RMM1-...` key string using the 256-bit private scalar `private_key`."""
    if not (1 <= private_key < _N):
        raise ValueError("Invalid secp256k1 private scalar")
    pub = _pt_mul(private_key, _G)
    if pub is None:
        raise ValueError("Invalid public point")
    if issued_day is None:
        issued_day = max(0, int((time.time() - _EPOCH_2025) // 86400))
    holder_b = (holder or "Supporter").strip().encode("utf-8")[:24]
    flags = ((tier_id & 0x07) << 1) | (FLAG_SELFTEST if selftest else 0)
    header = struct.pack(">BBIHB", 1, flags, key_id & 0xFFFFFFFF, min(65535, max(0, issued_day)), len(holder_b))
    payload = header + holder_b

    # Deterministic RFC-6979-style nonce k = HMAC-SHA256(d_bytes, payload)
    nonce_seed = hmac.new(private_key.to_bytes(32, "big"), _SALT + payload, hashlib.sha256).digest()
    k = (int.from_bytes(nonce_seed, "big") % (_N - 1)) + 1
    r_pt = _pt_mul(k, _G)
    assert r_pt is not None
    e = _challenge(r_pt[0], pub[0], payload)
    s = (k - e * private_key) % _N
    sig = e.to_bytes(16, "big") + s.to_bytes(32, "big")
    return _format_key(payload + sig)


def verify_supporter_key(raw_key: str, pubkey: tuple[int, int] = PRODUCTION_PUBKEY,
                         allow_selftest: bool = False) -> SupporterLicense:
    """Verify an `RMM1-...` key against `pubkey` on secp256k1. Never raises; returns SupporterLicense."""
    cleaned = _clean_key(raw_key)
    if not cleaned:
        return SupporterLicense(False, error="No key entered.")
    if len(cleaned) < 80 or len(cleaned) > 160:
        return SupporterLicense(False, error="Key length is invalid. Check that you copied the full RMM1-... key.")
    pad = (-len(cleaned)) % 8
    try:
        blob = base64.b32decode(cleaned + ("=" * pad), casefold=True)
    except Exception:
        return SupporterLicense(False, error="Key contains invalid characters.")
    if len(blob) < 9 + 48:
        return SupporterLicense(False, error="Key structure is truncated.")

    payload, sig = blob[:-48], blob[-48:]
    ver, flags, key_id, issued_day, hlen = struct.unpack(">BBIHB", payload[:9])
    if ver != 1 or len(payload) != 9 + hlen:
        return SupporterLicense(False, error="Unsupported or corrupted key format.")
    if (flags & FLAG_SELFTEST) and not allow_selftest:
        return SupporterLicense(False, error="Self-test keys cannot unlock the application.")
    if not _pt_on_curve(pubkey):
        return SupporterLicense(False, error="Invalid public key configuration.")

    e = int.from_bytes(sig[:16], "big")
    s = int.from_bytes(sig[16:], "big")
    if not (0 < e < (1 << 128) and 0 < s < _N):
        return SupporterLicense(False, error="Invalid signature parameters.")

    # Verify R' = s*G + e*Q
    sg = _pt_mul(s, _G)
    eq = _pt_mul(e, pubkey)
    r_prime = _pt_add(sg, eq)
    if r_prime is None:
        return SupporterLicense(False, error="Signature verification failed.")
    e_prime = _challenge(r_prime[0], pubkey[0], payload)
    if not hmac.compare_digest(e.to_bytes(16, "big"), e_prime.to_bytes(16, "big")):
        return SupporterLicense(False, error="Signature mismatch — key is invalid or modified.")

    try:
        holder = payload[9:9 + hlen].decode("utf-8", errors="replace").strip() or "Supporter"
    except Exception:
        holder = "Supporter"
    tier_id = (flags >> 1) & 0x07
    tier_name = TIER_NAMES.get(tier_id, "Archotech Supporter")
    issued_ts = _EPOCH_2025 + issued_day * 86400
    issued_date = time.strftime("%Y-%m-%d", time.gmtime(issued_ts))
    proof = hmac.new(sig, struct.pack(">I", key_id) + b":rmm-unlocked", hashlib.sha256).hexdigest()[:24]
    return SupporterLicense(
        valid=True,
        key_id=key_id,
        tier_id=tier_id,
        tier_name=tier_name,
        holder=holder,
        issued_date=issued_date,
        proof_token=proof,
    )


# ---------------------------------------------------------------- runtime cache & settings helpers
_cache_lock_raw: str = ""
_cache_lic: SupporterLicense = SupporterLicense(False)


def get_supporter_license(settings) -> SupporterLicense:
    """Read and cryptographically verify the DPAPI-encrypted supporter key from `settings`."""
    global _cache_lock_raw, _cache_lic
    if settings is None:
        return SupporterLicense(False)
    try:
        raw = settings.get_secret("supporter_key") if hasattr(settings, "get_secret") else ""
    except Exception:
        raw = ""
    raw = (raw or "").strip()
    if not raw:
        _cache_lock_raw = ""
        _cache_lic = SupporterLicense(False)
        return _cache_lic
    if raw == _cache_lock_raw and _cache_lic.valid:
        return _cache_lic
    lic = verify_supporter_key(raw)
    _cache_lock_raw = raw
    _cache_lic = lic
    return lic


def is_supporter(settings) -> bool:
    lic = get_supporter_license(settings)
    return bool(lic.valid and len(lic.proof_token) == 24)


def is_theme_unlocked(theme_name: str, settings=None) -> bool:
    """Free themes ('dark', 'light') are always available; Supporter themes require a verified EC signature."""
    if theme_name not in SUPPORTER_THEMES:
        return True
    return is_supporter(settings)


def is_persona_unlocked(persona_key: str, settings=None) -> bool:
    """'efficient' is the free default; the 9 storyteller/roleplay personalities require a verified Supporter Key."""
    if not persona_key or persona_key == "efficient":
        return True
    return is_supporter(settings)


def activate_supporter_key(settings, raw_key: str) -> SupporterLicense:
    """Verify `raw_key` and, if valid, persist it via DPAPI in `settings`."""
    global _cache_lock_raw, _cache_lic
    lic = verify_supporter_key(raw_key)
    if not lic.valid:
        return lic
    canonical = "RMM1-" + "-".join(_clean_key(raw_key)[i:i + 5] for i in range(0, len(_clean_key(raw_key)), 5))
    settings.set_secret("supporter_key", canonical)
    settings.save()
    _cache_lock_raw = canonical
    _cache_lic = lic
    return lic


def deactivate_supporter_key(settings) -> None:
    """Remove the stored supporter key and revert to a free theme if a supporter theme was active."""
    global _cache_lock_raw, _cache_lic
    _cache_lock_raw = ""
    _cache_lic = SupporterLicense(False)
    if hasattr(settings, "set_secret"):
        settings.set_secret("supporter_key", "")
    if settings.get("theme") in SUPPORTER_THEMES:
        settings.set("theme", "dark")
    settings.save()


# ---------------------------------------------------------------- GUI dialog
def show_support_dialog(app) -> None:
    """Open the themed 'Support & Supporter Perks' modal window."""
    import tkinter as tk
    import webbrowser
    from tkinter import ttk

    root = app.root
    existing = getattr(app, "_support_dlg", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.lift()
                existing.focus_force()
                return
        except tk.TclError:
            pass

    c = app.colors
    f = app.fonts
    px = app.px
    s = app.mgr.settings
    lic = get_supporter_license(s)

    dlg = tk.Toplevel(root)
    app._support_dlg = dlg
    dlg.title("Support RimWorld Mod Manager")
    dlg.configure(background=c["bg"])
    dlg.transient(root)
    dlg.resizable(False, False)
    try:
        from rmm.gui import theme
        theme.titlebar_dark(dlg, app.theme_name != "light")
    except Exception:
        pass

    outer = ttk.Frame(dlg, style="TFrame", padding=px(18))
    outer.pack(fill="both", expand=True)

    # Hero card
    hero = ttk.Frame(outer, style="Hero.TFrame", padding=px(16))
    hero.pack(fill="x", pady=(0, px(12)))
    title_txt = "Supporter Edition Active" if lic.valid else "Support the Project"
    ttk.Label(hero, text=title_txt, style="PanelHeading.TLabel", font=f["title"]).pack(anchor="w")
    sub_txt = (
        f"Active license: {lic.tier_name}  ·  Licensed to {lic.holder} (Key #{lic.key_id:04d}, issued {lic.issued_date})"
        if lic.valid else
        "RimWorld Mod Manager is completely free — nothing important is behind a paywall.\n"
        "If this tool saved you time fixing load orders or building modlists, you can support\n"
        "development on Itch.io or Ko-fi ($5+) to get a Supporter Key."
    )
    ttk.Label(hero, text=sub_txt, style="PanelMuted.TLabel", justify="left").pack(anchor="w", pady=(px(6), px(10)))

    btn_row = ttk.Frame(hero, style="Panel.TFrame")
    btn_row.pack(anchor="w")
    ttk.Button(btn_row, text="Itch.io Page (Get a Key)", style="Accent.TButton",
               command=lambda: webbrowser.open(ITCH_URL)).pack(side="left", padx=(0, px(8)))
    ttk.Button(btn_row, text="Support on Ko-fi", style="TButton",
               command=lambda: webbrowser.open(KOFI_URL)).pack(side="left", padx=(0, px(8)))
    ttk.Button(btn_row, text="GitHub", style="TButton",
               command=lambda: webbrowser.open(REPO_URL)).pack(side="left")

    # Perks card
    perks = ttk.Frame(outer, style="Card.TFrame", padding=px(14))
    perks.pack(fill="x", pady=(0, px(12)))
    ttk.Label(perks, text="What a Supporter Key Unlocks", style="PanelHeading.TLabel").pack(anchor="w", pady=(0, px(6)))
    perk_items = [
        "- 9 Storyteller Chat Personalities in AI Generator (Randy Random, Cassandra, Phoebe, Archotech, War Crime CEO & more)",
        "- 4 extra dark themes: Archotech Gold, Glitterworld, Blood Moon, and pitch-black Void OLED",
        "- Supporter badge in the top bar with your name/handle",
        "- Helps keep the mod database and RimWorld version support updated",
    ]
    for item in perk_items:
        ttk.Label(perks, text=item, style="Panel.TLabel").pack(anchor="w", pady=px(2))

    # Quick theme preview/switcher for supporters
    theme_row = ttk.Frame(perks, style="Panel.TFrame")
    theme_row.pack(fill="x", pady=(px(8), 0))
    ttk.Label(theme_row, text="Supporter Themes:", style="PanelMuted.TLabel").pack(side="left", padx=(0, px(8)))

    def _try_theme(tname: str) -> None:
        if not is_supporter(s):
            status_lbl.configure(text="Enter and activate a Supporter Key below to unlock these themes.",
                                 foreground=c["warn"])
            key_entry.focus_set()
            return
        s.set("theme", tname)
        app.run_bg(s.save)
        app.emit("settings_changed")
        app.toast(f"Theme set to {tname.title()}", "ok")

    for tkey, tlabel in (("archotech", "Archotech Gold"), ("glitterworld", "Glitterworld"),
                         ("bloodmoon", "Blood Moon"), ("oled", "Void OLED")):
        ttk.Button(theme_row, text=tlabel, style="Small.TButton",
                   command=lambda k=tkey: _try_theme(k)).pack(side="left", padx=(0, px(6)))

    # Key activation card
    act = ttk.Frame(outer, style="Card.TFrame", padding=px(14))
    act.pack(fill="x", pady=(0, px(10)))
    ttk.Label(act, text="Activate Supporter Key", style="PanelHeading.TLabel").pack(anchor="w")
    ttk.Label(act, text="Paste your RMM1-... key below:",
              style="PanelMuted.TLabel").pack(anchor="w", pady=(px(2), px(8)))

    entry_row = ttk.Frame(act, style="Panel.TFrame")
    entry_row.pack(fill="x")
    key_var = tk.StringVar(value=s.get_secret("supporter_key") if lic.valid else "")
    key_entry = ttk.Entry(entry_row, textvariable=key_var, width=54, font=f["mono"])
    key_entry.pack(side="left", fill="x", expand=True, padx=(0, px(8)))

    status_lbl = ttk.Label(
        act,
        text=(f"Verified: {lic.tier_name} — {lic.holder} (#{lic.key_id:04d})" if lic.valid else ""),
        style="PanelMuted.TLabel",
        foreground=c["ok"] if lic.valid else c["muted"],
        wraplength=px(500),
    )

    def _do_activate() -> None:
        raw = key_var.get().strip()
        res = activate_supporter_key(s, raw)
        if not res.valid:
            status_lbl.configure(text=res.error, foreground=c["err"])
            return
        status_lbl.configure(
            text=f"Unlocked: {res.holder} ({res.tier_name} · Key #{res.key_id:04d})",
            foreground=c["ok"],
        )
        if hasattr(app, "refresh_supporter_badge"):
            app.refresh_supporter_badge()
        app.emit("settings_changed")
        app.toast(f"Supporter Edition unlocked ({res.holder})", "ok")

    ttk.Button(entry_row, text="Activate Key", style="Success.TButton", command=_do_activate).pack(side="left")
    status_lbl.pack(anchor="w", pady=(px(6), 0))

    foot = ttk.Frame(outer, style="TFrame")
    foot.pack(fill="x")
    ttk.Button(foot, text="Close", command=dlg.destroy).pack(side="right")

    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    rx, ry, rw, rh = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    x, y = max(0, rx + (rw - w) // 2), max(0, ry + (rh - h) // 2)
    dlg.geometry(f"{w}x{h}+{x}+{y}")
    dlg.focus_force()


# ---------------------------------------------------------------- self-test
def _selftest() -> None:
    assert _pt_on_curve(_G) and _pt_on_curve(PRODUCTION_PUBKEY) and not _pt_on_curve((1, 2))
    # Ephemeral keypair roundtrip
    test_d = 0x1A2B3C4D5E6F77889900AABBCCDDEEFF112233445566778899AABBCCDDEEFF01
    test_pub = _pt_mul(test_d, _G)
    assert test_pub is not None and _pt_on_curve(test_pub)

    key = sign_supporter_payload(test_d, key_id=42, holder="Tynan Sylvester", tier_id=1, issued_day=300)
    assert key.startswith("RMM1-")
    lic = verify_supporter_key(key, pubkey=test_pub)
    assert lic.valid and lic.key_id == 42 and lic.holder == "Tynan Sylvester" and lic.tier_id == 1, lic
    assert len(lic.proof_token) == 24

    # Forgery / wrong public key must fail
    wrong_lic = verify_supporter_key(key, pubkey=PRODUCTION_PUBKEY)
    assert not wrong_lic.valid and "mismatch" in wrong_lic.error.lower(), wrong_lic

    # Tampering any character in the key must fail verification
    flipped = key[:-4] + ("AAAA" if not key.endswith("AAAA") else "BBBB")
    assert not verify_supporter_key(flipped, pubkey=test_pub).valid

    # Self-test flagged key is rejected unless allow_selftest=True
    st_key = sign_supporter_payload(test_d, key_id=7, holder="Test", selftest=True)
    assert not verify_supporter_key(st_key, pubkey=test_pub, allow_selftest=False).valid
    assert verify_supporter_key(st_key, pubkey=test_pub, allow_selftest=True).valid

    # Persona gating: "efficient" is free default, roleplay personas require supporter key
    assert is_persona_unlocked("efficient", None)
    assert not is_persona_unlocked("chaotic", None)
    assert not is_persona_unlocked("cassandra", None)



if __name__ == "__main__":
    _selftest()
    print("supporter ok")
