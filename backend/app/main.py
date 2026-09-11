"""FastAPI app: local-only CRM backend (TZ §6). No auth — bound to
127.0.0.1 by the run command, not exposed to the network.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import database, orchestrator
from app.config import settings

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(title="Telegram Ads Parser CRM")


@app.on_event("startup")
async def on_startup() -> None:
    database.init_db()


class AddChatRequest(BaseModel):
    identifier: str


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/chats")
async def list_chats() -> list[dict]:
    return [dict(row) for row in database.list_chats()]


@app.post("/api/chats")
async def add_chat(payload: AddChatRequest) -> dict:
    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Пустой идентификатор чата")
    try:
        row = database.add_chat(identifier)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Не удалось добавить чат: {exc}")
    return dict(row)


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: int) -> dict:
    database.remove_chat(chat_id)
    return {"ok": True}


@app.post("/api/run")
async def start_run() -> dict:
    if orchestrator.is_running():
        raise HTTPException(status_code=409, detail="Проход уже выполняется")
    try:
        orchestrator.start_run_background()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True}


@app.get("/api/status")
async def status() -> dict:
    stats = database.get_stats()
    latest_run = database.get_latest_run()
    active_chat = database.get_next_active_chat()
    return {
        "running": orchestrator.is_running(),
        "stats": stats,
        "latest_run": dict(latest_run) if latest_run else None,
        "active_chat": dict(active_chat) if active_chat else None,
        "cards_per_run": settings.cards_per_run,
    }


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
