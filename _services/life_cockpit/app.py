from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import Settings
from database import Database
from google_calendar import GoogleCalendar, GoogleCalendarError


settings = Settings.from_env()
database = Database(settings.database_path)
google_calendar = GoogleCalendar(settings, database)


class EventBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[int]]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, user: str) -> asyncio.Queue[int]:
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=4)
        async with self._lock:
            self._subscribers.setdefault(user, set()).add(queue)
        return queue

    async def unsubscribe(self, user: str, queue: asyncio.Queue[int]) -> None:
        async with self._lock:
            queues = self._subscribers.get(user)
            if not queues:
                return
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(user, None)

    async def publish(self, user: str, revision: int) -> None:
        async with self._lock:
            queues = list(self._subscribers.get(user, set()))
        for queue in queues:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(revision)


broker = EventBroker()
background_stop = asyncio.Event()


def validate_day(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="日期格式无效") from exc
    return parsed.isoformat()


def week_key(day: str) -> str:
    parsed = date.fromisoformat(day)
    iso_year, iso_week, _ = parsed.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def validate_block_range(start: str, end: str) -> None:
    try:
        start_hour, start_minute = (int(part) for part in start.split(":"))
        end_hour, end_minute = (int(part) for part in end.split(":"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="时间格式无效") from exc
    start_value = start_hour * 60 + start_minute
    end_value = end_hour * 60 + end_minute
    if end_value - start_value < 15:
        raise HTTPException(
            status_code=422,
            detail="结束时间需要晚于开始时间至少 15 分钟",
        )


def current_user(
    remote_user: str | None = Header(default=None, alias="Remote-User"),
    proxy_secret: str | None = Header(
        default=None, alias="X-LC-Proxy-Secret"
    ),
) -> str:
    if settings.proxy_secret and not secrets.compare_digest(
        proxy_secret or "", settings.proxy_secret
    ):
        raise HTTPException(status_code=401, detail="代理认证无效")
    user = (remote_user or "").strip().lower()
    if not user:
        raise HTTPException(status_code=401, detail="缺少 SSO 用户身份")
    if settings.allowed_users and user not in settings.allowed_users:
        raise HTTPException(status_code=403, detail="当前用户没有访问权限")
    return user


class PriorityPayload(BaseModel):
    text: str = Field(default="", max_length=240)
    done: bool = False


class NotesPayload(BaseModel):
    text: str = Field(default="", max_length=10000)


class WeekPayload(BaseModel):
    goal: str = Field(default="", max_length=800)
    milestone: str = Field(default="", max_length=240)
    progress: int = Field(default=0, ge=0, le=100)


Category = Literal["research", "course", "life", "health"]


class TaskPayload(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    day: str | None = None
    category: Category = "life"
    text: str = Field(min_length=1, max_length=240)
    done: bool = False


class BlockPayload(BaseModel):
    id: str | None = Field(default=None, max_length=120)
    day: str | None = None
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    category: Category = "life"
    text: str = Field(min_length=1, max_length=240)
    done: bool = False


class ImportPayload(BaseModel):
    state: dict[str, Any]


async def publish_revision(user: str, revision: int) -> None:
    await broker.publish(user, revision)


async def sync_google_day(user: str, day: str) -> dict[str, Any]:
    try:
        result = await google_calendar.sync_day(user, day)
        if result.get("changed"):
            await publish_revision(user, int(result["revision"]))
        return result
    except GoogleCalendarError as exc:
        return {
            "connected": True,
            "changed": False,
            "error": str(exc),
            "revision": database.revision(user),
        }


async def background_sync() -> None:
    while not background_stop.is_set():
        for user in database.google_users():
            days = database.active_days(user, limit=3)
            today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
            if today not in days:
                days.insert(0, today)
            for day in days:
                await sync_google_day(user, day)
        try:
            await asyncio.wait_for(
                background_stop.wait(),
                timeout=settings.sync_interval_seconds,
            )
        except TimeoutError:
            continue


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    database.initialize()
    background_stop.clear()
    task = asyncio.create_task(background_sync())
    try:
        yield
    finally:
        background_stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="Life Cockpit API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/api/health")
async def health(user: str = Depends(current_user)) -> dict[str, Any]:
    return {
        "ok": True,
        "user": user,
        "revision": database.revision(user),
        "google": google_calendar.status(user),
    }


@app.get("/api/status")
async def status(user: str = Depends(current_user)) -> dict[str, Any]:
    return {
        "user": user,
        "hasData": database.has_data(user),
        "revision": database.revision(user),
        "google": {
            **google_calendar.status(user),
            "redirectUri": settings.google_redirect_uri,
        },
    }


@app.get("/api/days/{day}")
async def get_day(day: str, user: str = Depends(current_user)) -> dict[str, Any]:
    selected_day = validate_day(day)
    database.touch_active_day(user, selected_day)
    google_result = await sync_google_day(user, selected_day)
    payload = database.get_day(user, selected_day, week_key(selected_day))
    payload["google"] = {
        **google_calendar.status(user),
        "error": google_result.get("error"),
        "pendingErrors": len(google_result.get("errors", [])),
    }
    return payload


@app.put("/api/days/{day}/priorities/{slot}")
async def put_priority(
    day: str,
    slot: int,
    payload: PriorityPayload,
    user: str = Depends(current_user),
) -> dict[str, int]:
    selected_day = validate_day(day)
    if slot not in {0, 1, 2}:
        raise HTTPException(status_code=422, detail="优先事项位置无效")
    revision = database.update_priority(
        user, selected_day, slot, payload.text, payload.done
    )
    await publish_revision(user, revision)
    return {"revision": revision}


@app.put("/api/days/{day}/notes")
async def put_notes(
    day: str,
    payload: NotesPayload,
    user: str = Depends(current_user),
) -> dict[str, int]:
    selected_day = validate_day(day)
    revision = database.update_notes(user, selected_day, payload.text)
    await publish_revision(user, revision)
    return {"revision": revision}


@app.put("/api/weeks/{week}")
async def put_week(
    week: str,
    payload: WeekPayload,
    user: str = Depends(current_user),
) -> dict[str, int]:
    if len(week) != 8 or week[4:6] != "-W":
        raise HTTPException(status_code=422, detail="周标识无效")
    revision = database.update_week(
        user,
        week,
        payload.goal,
        payload.milestone,
        payload.progress,
    )
    await publish_revision(user, revision)
    return {"revision": revision}


@app.post("/api/days/{day}/tasks")
async def create_task(
    day: str,
    payload: TaskPayload,
    user: str = Depends(current_user),
) -> dict[str, Any]:
    selected_day = validate_day(day)
    task, revision = database.upsert_task(
        user,
        payload.id or "",
        selected_day,
        payload.category,
        payload.text,
        payload.done,
    )
    await publish_revision(user, revision)
    return {"task": task, "revision": revision}


@app.put("/api/tasks/{task_id}")
async def update_task(
    task_id: str,
    payload: TaskPayload,
    user: str = Depends(current_user),
) -> dict[str, Any]:
    if not payload.day:
        raise HTTPException(status_code=422, detail="缺少待办日期")
    selected_day = validate_day(payload.day)
    task, revision = database.upsert_task(
        user,
        task_id,
        selected_day,
        payload.category,
        payload.text,
        payload.done,
    )
    await publish_revision(user, revision)
    return {"task": task, "revision": revision}


@app.delete("/api/tasks/{task_id}")
async def delete_task(
    task_id: str, user: str = Depends(current_user)
) -> dict[str, int]:
    revision = database.delete_task(user, task_id)
    await publish_revision(user, revision)
    return {"revision": revision}


@app.post("/api/days/{day}/blocks")
async def create_block(
    day: str,
    payload: BlockPayload,
    user: str = Depends(current_user),
) -> dict[str, Any]:
    selected_day = validate_day(day)
    validate_block_range(payload.start, payload.end)
    block, revision = database.upsert_block(
        user,
        payload.id or "",
        selected_day,
        payload.start,
        payload.end,
        payload.category,
        payload.text,
        payload.done,
    )
    await publish_revision(user, revision)
    google_result = await sync_google_day(user, selected_day)
    return {
        "block": block,
        "revision": int(google_result.get("revision", revision)),
        "googleError": google_result.get("error"),
    }


@app.put("/api/blocks/{block_id}")
async def update_block(
    block_id: str,
    payload: BlockPayload,
    user: str = Depends(current_user),
) -> dict[str, Any]:
    if not payload.day:
        raise HTTPException(status_code=422, detail="缺少时间块日期")
    selected_day = validate_day(payload.day)
    validate_block_range(payload.start, payload.end)
    block, revision = database.upsert_block(
        user,
        block_id,
        selected_day,
        payload.start,
        payload.end,
        payload.category,
        payload.text,
        payload.done,
    )
    await publish_revision(user, revision)
    google_result = await sync_google_day(user, selected_day)
    return {
        "block": block,
        "revision": int(google_result.get("revision", revision)),
        "googleError": google_result.get("error"),
    }


@app.delete("/api/blocks/{block_id}")
async def delete_block(
    block_id: str, user: str = Depends(current_user)
) -> dict[str, Any]:
    existing = database.get_block(user, block_id)
    if not existing:
        raise HTTPException(status_code=404, detail="时间块不存在")
    _, revision = database.delete_block(user, block_id)
    await publish_revision(user, revision)
    google_result = await sync_google_day(user, str(existing["day"]))
    return {
        "revision": int(google_result.get("revision", revision)),
        "googleError": google_result.get("error"),
    }


@app.post("/api/import-local")
async def import_local(
    payload: ImportPayload,
    user: str = Depends(current_user),
) -> dict[str, Any]:
    revision = database.import_local_state(user, payload.state)
    await publish_revision(user, revision)
    return {"revision": revision, "hasData": database.has_data(user)}


@app.post("/api/replace-state")
async def replace_state(
    payload: ImportPayload,
    user: str = Depends(current_user),
) -> dict[str, Any]:
    revision = database.replace_local_state(user, payload.state)
    await publish_revision(user, revision)
    return {"revision": revision, "hasData": database.has_data(user)}


@app.get("/api/events")
async def events(
    request: Request, user: str = Depends(current_user)
) -> StreamingResponse:
    queue = await broker.subscribe(user)

    async def stream() -> AsyncIterator[str]:
        try:
            yield (
                "event: ready\n"
                f"data: {json.dumps({'revision': database.revision(user)})}\n\n"
            )
            while True:
                if await request.is_disconnected():
                    break
                try:
                    revision = await asyncio.wait_for(queue.get(), timeout=20)
                    yield (
                        "event: revision\n"
                        f"data: {json.dumps({'revision': revision})}\n\n"
                    )
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            await broker.unsubscribe(user, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/google/status")
async def google_status(user: str = Depends(current_user)) -> dict[str, Any]:
    return {
        **google_calendar.status(user),
        "redirectUri": settings.google_redirect_uri,
    }


@app.post("/api/google/connect")
async def google_connect(user: str = Depends(current_user)) -> dict[str, str]:
    try:
        return {"url": google_calendar.authorization_url(user)}
    except GoogleCalendarError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/google/callback")
async def google_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    user: str = Depends(current_user),
) -> RedirectResponse:
    if error:
        query = urlencode({"google": "error", "message": error})
        return RedirectResponse(f"/?{query}", status_code=303)
    if not code:
        query = urlencode({"google": "error", "message": "缺少授权码"})
        return RedirectResponse(f"/?{query}", status_code=303)
    try:
        await google_calendar.complete_authorization(state, code, user)
        await sync_google_day(user, date.today().isoformat())
        return RedirectResponse("/?google=connected", status_code=303)
    except GoogleCalendarError as exc:
        query = urlencode({"google": "error", "message": str(exc)})
        return RedirectResponse(f"/?{query}", status_code=303)


@app.post("/api/google/disconnect")
async def google_disconnect(
    user: str = Depends(current_user),
) -> dict[str, bool]:
    await google_calendar.disconnect(user)
    revision = database.revision(user)
    await publish_revision(user, revision)
    return {"connected": False}


@app.post("/api/google/sync")
async def google_sync(
    day: str, user: str = Depends(current_user)
) -> dict[str, Any]:
    selected_day = validate_day(day)
    result = await sync_google_day(user, selected_day)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result
