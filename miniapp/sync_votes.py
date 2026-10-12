"""Copies the voting rules from the bot's codebase into the mini app.

backend/app/votes.py is the single source of truth (the bot imports it
directly). Vercel deploys only this miniapp/ folder, so it needs its own copy:

    python miniapp/sync_votes.py      # then: cd miniapp && vercel deploy --prod

Never edit miniapp/api/_votes.py by hand — the next sync overwrites it.
"""
from pathlib import Path

root = Path(__file__).resolve().parent
src = root.parent / "backend" / "app" / "votes.py"
dst = root / "api" / "_votes.py"

banner = (
    "# GENERATED FILE — do not edit. Source: backend/app/votes.py\n"
    "# Refresh with: python miniapp/sync_votes.py\n"
)
dst.write_text(banner + src.read_text(encoding="utf-8"), encoding="utf-8")
print(f"synced {src.relative_to(root.parent)} -> {dst.relative_to(root.parent)}")
