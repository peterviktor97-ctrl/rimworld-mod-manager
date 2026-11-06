"""Author-only CLI tool to generate cryptographically signed RMM1-... Supporter Keys.

Reads your private secp256k1 master key from `~/.rmm_supporter_master.key` (never bundled into the app).

Examples:
    .venv\\Scripts\\python.exe tools\\supporter_keygen.py --holder "Colony Founder" --tier 2 --id 1
    .venv\\Scripts\\python.exe tools\\supporter_keygen.py --batch 100 --start-id 100 --out supporter_keys_batch.txt
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rmm import supporter  # noqa: E402

DEFAULT_KEYFILE = os.path.expanduser("~/.rmm_supporter_master.key")


def load_private_key(path: str = DEFAULT_KEYFILE) -> int:
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Master private key not found at {path}. Keep this file backed up and never commit it to git."
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    d = int(data["private_key_hex"], 16)
    pub = supporter._pt_mul(d)
    if pub != supporter.PRODUCTION_PUBKEY:
        raise RuntimeError("Private key does not match PRODUCTION_PUBKEY in rmm/supporter.py!")
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate signed RMM1-... Supporter Keys.")
    ap.add_argument("--holder", default="Supporter", help="Supporter name or handle (up to 24 UTF-8 bytes)")
    ap.add_argument("--tier", type=int, default=1, choices=[0, 1, 2],
                    help="0 = Colony Supporter, 1 = Archotech Supporter, 2 = Glitterworld Founder")
    ap.add_argument("--id", type=int, default=1, help="Key ID (or starting ID for --batch)")
    ap.add_argument("--batch", type=int, default=0,
                    help="Generate N unique keys (e.g. --batch 100 for Gumroad / Ko-fi license lists)")
    ap.add_argument("--out", default="", help="Optional output file path to write generated key(s)")
    args = ap.parse_args()

    priv_d = load_private_key()
    keys: list[str] = []
    if args.batch > 0:
        for i in range(args.batch):
            kid = args.id + i
            holder = f"{args.holder} #{kid:04d}" if args.holder == "Supporter" else args.holder
            k = supporter.sign_supporter_payload(priv_d, key_id=kid, holder=holder, tier_id=args.tier)
            lic = supporter.verify_supporter_key(k)
            assert lic.valid, f"Generated key #{kid} failed verification: {lic}"
            keys.append(k)
    else:
        k = supporter.sign_supporter_payload(priv_d, key_id=args.id, holder=args.holder, tier_id=args.tier)
        lic = supporter.verify_supporter_key(k)
        assert lic.valid, f"Generated key failed verification: {lic}"
        keys.append(k)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(keys) + "\n")
        print(f"Wrote {len(keys)} verified Supporter Key(s) to {args.out}")
    else:
        for k in keys:
            print(k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
