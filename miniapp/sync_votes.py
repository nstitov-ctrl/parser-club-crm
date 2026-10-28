"""Copies the voting rules from the bot's codebase into the mini app.

backend/app/votes.py is the single source of truth (the bot imports it
directly). Vercel deploys only this miniapp/ folder, so it needs its own copy:

    python miniapp/sync_votes.py      # then: cd miniapp && vercel deploy --prod

Never edit miniapp/api/_votes.py by hand — the next sync overwrites it.
"""
from pathlib import Path

import ast

root = Path(__file__).resolve().parent
app = root.parent / "backend" / "app"
out = root / "api"


def banner(source: str) -> str:
    return (f"# GENERATED FILE — do not edit. Source: backend/app/{source}\n"
            "# Refresh with: python miniapp/sync_votes.py\n")


# 1) shared rules, copied as-is (the bot imports the originals directly)
(out / "_votes.py").write_text(banner("votes.py") + (app / "votes.py").read_text(encoding="utf-8"), encoding="utf-8")

# 2) author actions: same file, only the package import differs inside the mini app
authoring = (app / "authoring.py").read_text(encoding="utf-8")
assert "from app import votes" in authoring
authoring = authoring.replace("from app import votes", "import _votes as votes")
(out / "_authoring.py").write_text(banner("authoring.py") + authoring, encoding="utf-8")

# 3) category normalisation: CATEGORY_ALIASES + normalize_category, cut out of sheets_writer.py
#    (that module can't be imported on Vercel: it needs config/credentials and a writable data/ dir)
src = (app / "sheets_writer.py").read_text(encoding="utf-8")
parts = []
for node in ast.parse(src).body:
    if (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "CATEGORY_ALIASES") or \
       (isinstance(node, ast.FunctionDef) and node.name == "normalize_category"):
        parts.append(ast.get_source_segment(src, node))
assert len(parts) == 2, "CATEGORY_ALIASES / normalize_category not found in sheets_writer.py"
(out / "_aliases.py").write_text(
    banner("sheets_writer.py (CATEGORY_ALIASES, normalize_category)")
    + "from __future__ import annotations\n\nfrom typing import Optional\n\n" + "\n\n\n".join(parts) + "\n",
    encoding="utf-8",
)

# 4) forbidden-topic check: same prompt and tool schema as the bot's "Добавить"/"Изменить" flows
bot_src = (app / "bot.py").read_text(encoding="utf-8")
consts = {}
for node in ast.parse(bot_src).body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in (
            "_FORBIDDEN_CHECK_SYSTEM_PROMPT", "_FORBIDDEN_CHECK_TOOL_SCHEMA"):
        consts[node.targets[0].id] = ast.literal_eval(node.value)
assert len(consts) == 2, "forbidden-topic constants not found in bot.py"
(out / "_forbidden.py").write_text(
    banner("bot.py (forbidden-topic prompt)")
    + f"SYSTEM_PROMPT = {consts['_FORBIDDEN_CHECK_SYSTEM_PROMPT']!r}\n\nTOOL_SCHEMA = {consts['_FORBIDDEN_CHECK_TOOL_SCHEMA']!r}\n",
    encoding="utf-8",
)
print("synced: _votes.py, _authoring.py, _aliases.py, _forbidden.py")
