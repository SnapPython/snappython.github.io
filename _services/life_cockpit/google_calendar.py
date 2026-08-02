from __future__ import annotations

import secrets
import time
from datetime import date, datetime, time as clock_time, timedelta
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken

from config import Settings
from database import Database


GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API_ROOT = "https://www.googleapis.com/calendar/v3"

CATEGORY_TO_COLOR = {
    "research": "10",
    "course": "9",
    "life": "5",
    "health": "11",
}

COLOR_TO_CATEGORY = {
    "1": "course",
    "2": "research",
    "3": "research",
    "4": "health",
    "5": "life",
    "6": "life",
    "7": "course",
    "8": "life",
    "9": "course",
    "10": "research",
    "11": "health",
}


class GoogleCalendarError(RuntimeError):
    pass


class GoogleCalendar:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.timezone = ZoneInfo(settings.timezone)
        self._fernet = (
            Fernet(settings.google_token_key.encode())
            if settings.google_configured
            else None
        )

    @property
    def configured(self) -> bool:
        return self.settings.google_configured

    def status(self, user: str) -> dict[str, Any]:
        token = self.database.get_google_token(user) if self.configured else None
        return {
            "configured": self.configured,
            "connected": bool(token),
            "calendar": "primary" if token else None,
        }

    def authorization_url(self, user: str) -> str:
        if not self.configured:
            raise GoogleCalendarError("Google Calendar 尚未配置")
        state = secrets.token_urlsafe(32)
        self.database.store_oauth_state(
            state, user, int(time.time()) + 10 * 60
        )
        query = urlencode(
            {
                "client_id": self.settings.google_client_id,
                "redirect_uri": self.settings.google_redirect_uri,
                "response_type": "code",
                "scope": GOOGLE_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
            }
        )
        return f"{GOOGLE_AUTH_URL}?{query}"

    async def complete_authorization(
        self, state: str, code: str, request_user: str
    ) -> str:
        if not self.configured:
            raise GoogleCalendarError("Google Calendar 尚未配置")
        state_user = self.database.consume_oauth_state(state)
        if not state_user or state_user != request_user:
            raise GoogleCalendarError("Google 授权状态无效或已过期")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "redirect_uri": self.settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if response.is_error:
            raise GoogleCalendarError(
                f"Google 授权失败：{self._google_error(response)}"
            )

        payload = response.json()
        access_token = str(payload.get("access_token", ""))
        refresh_token = str(payload.get("refresh_token", ""))
        if not access_token:
            raise GoogleCalendarError("Google 未返回访问令牌")
        self.database.set_google_token(
            state_user,
            self._encrypt(access_token),
            self._encrypt(refresh_token) if refresh_token else "",
            int(time.time()) + int(payload.get("expires_in", 3600)) - 60,
            str(payload.get("scope", GOOGLE_SCOPE)),
        )
        return state_user

    async def disconnect(self, user: str) -> None:
        token_row = self.database.get_google_token(user)
        if token_row:
            try:
                token = self._decrypt(str(token_row["access_token"]))
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        "https://oauth2.googleapis.com/revoke",
                        params={"token": token},
                    )
            except (GoogleCalendarError, httpx.HTTPError):
                pass
        self.database.delete_google_token(user)

    async def sync_day(self, user: str, day: str) -> dict[str, Any]:
        if not self.configured or not self.database.get_google_token(user):
            return {
                "connected": False,
                "changed": False,
                "revision": self.database.revision(user),
            }

        self.database.retry_sync_errors(user)
        errors = await self._push_pending_blocks(user, day)
        events = await self._list_events(user, day)
        present_ids: set[str] = set()
        changed = False
        revision = self.database.revision(user)

        for event in events:
            event_id = str(event.get("id", ""))
            if not event_id or event.get("status") == "cancelled":
                continue
            mapped = self._event_to_block(event, day)
            if not mapped:
                continue
            present_ids.add(event_id)
            event_changed, revision = self.database.upsert_google_event(
                user=user,
                **mapped,
            )
            changed = changed or event_changed

        removed, revision = self.database.remove_missing_google_events(
            user, day, present_ids
        )
        changed = changed or removed
        return {
            "connected": True,
            "changed": changed,
            "revision": revision,
            "errors": errors,
        }

    async def _push_pending_blocks(
        self, user: str, day: str
    ) -> list[str]:
        errors: list[str] = []
        for block in self.database.pending_blocks(user, day):
            block_id = str(block["id"])
            try:
                if block["sync_state"] == "pending_delete":
                    if block["google_event_id"]:
                        await self._api(
                            user,
                            "DELETE",
                            (
                                "/calendars/primary/events/"
                                f"{quote(str(block['google_event_id']), safe='')}"
                            ),
                            allow_not_found=True,
                        )
                    self.database.purge_block(user, block_id)
                    continue

                payload = self._block_to_event(block)
                if block["google_event_id"]:
                    event = await self._api(
                        user,
                        "PATCH",
                        (
                            "/calendars/primary/events/"
                            f"{quote(str(block['google_event_id']), safe='')}"
                        ),
                        json_data=payload,
                    )
                else:
                    event = await self._api(
                        user,
                        "POST",
                        "/calendars/primary/events",
                        json_data=payload,
                    )
                self.database.mark_block_synced(
                    user,
                    block_id,
                    str(event["id"]),
                    event.get("etag"),
                    event.get("updated"),
                )
            except GoogleCalendarError as exc:
                self.database.mark_block_sync_error(user, block_id)
                errors.append(str(exc))
        return errors

    async def _list_events(self, user: str, day: str) -> list[dict[str, Any]]:
        selected = date.fromisoformat(day)
        start = datetime.combine(selected, clock_time.min, self.timezone)
        end = start + timedelta(days=1)
        params: dict[str, Any] = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "showDeleted": "false",
            "orderBy": "startTime",
            "maxResults": "2500",
            "eventTypes": "default",
            "timeZone": self.settings.timezone,
        }
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            request_params = dict(params)
            if page_token:
                request_params["pageToken"] = page_token
            payload = await self._api(
                user,
                "GET",
                "/calendars/primary/events",
                params=request_params,
            )
            items.extend(payload.get("items", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        return items

    def _event_to_block(
        self, event: dict[str, Any], selected_day: str
    ) -> dict[str, Any] | None:
        start_value = event.get("start", {}).get("dateTime")
        end_value = event.get("end", {}).get("dateTime")
        if not start_value or not end_value:
            return None
        try:
            start_at = datetime.fromisoformat(start_value).astimezone(self.timezone)
            end_at = datetime.fromisoformat(end_value).astimezone(self.timezone)
        except ValueError:
            return None

        selected = date.fromisoformat(selected_day)
        day_start = datetime.combine(selected, clock_time.min, self.timezone)
        day_end = day_start + timedelta(days=1)
        visible_start = max(start_at, day_start)
        visible_end = min(end_at, day_end - timedelta(minutes=1))
        if visible_end <= visible_start:
            return None

        private = event.get("extendedProperties", {}).get("private", {})
        app_block_id = str(private.get("lifeCockpitBlockId", ""))
        category = str(
            private.get("lifeCockpitCategory")
            or COLOR_TO_CATEGORY.get(str(event.get("colorId", "")), "life")
        )
        if category not in CATEGORY_TO_COLOR:
            category = "life"
        return {
            "block_id": app_block_id or f"gcal:{event['id']}",
            "day": selected_day,
            "start": visible_start.strftime("%H:%M"),
            "end": visible_end.strftime("%H:%M"),
            "category": category,
            "title": str(event.get("summary") or "未命名日程")[:240],
            "done": str(private.get("lifeCockpitDone", "")).lower() == "true",
            "google_event_id": str(event["id"]),
            "google_etag": event.get("etag"),
            "google_updated_at": event.get("updated"),
            "source": "cockpit" if app_block_id else "google",
        }

    def _block_to_event(self, block: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": str(block["title"]),
            "start": {
                "dateTime": (
                    f"{block['day']}T{block['start_time']}:00"
                ),
                "timeZone": self.settings.timezone,
            },
            "end": {
                "dateTime": f"{block['day']}T{block['end_time']}:00",
                "timeZone": self.settings.timezone,
            },
            "colorId": CATEGORY_TO_COLOR.get(str(block["category"]), "5"),
            "extendedProperties": {
                "private": {
                    "lifeCockpitBlockId": str(block["id"]),
                    "lifeCockpitCategory": str(block["category"]),
                    "lifeCockpitDone": (
                        "true" if bool(block["done"]) else "false"
                    ),
                }
            },
        }

    async def _api(
        self,
        user: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any]:
        token = await self._access_token(user)
        response = await self._send(
            token, method, path, params=params, json_data=json_data
        )
        if response.status_code == 401:
            token = await self._access_token(user, force_refresh=True)
            response = await self._send(
                token, method, path, params=params, json_data=json_data
            )
        if allow_not_found and response.status_code in {404, 410}:
            return {}
        if response.is_error:
            raise GoogleCalendarError(
                f"Google Calendar API：{self._google_error(response)}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def _send(
        self,
        token: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_data: dict[str, Any] | None,
    ) -> httpx.Response:
        async with httpx.AsyncClient(timeout=25) as client:
            return await client.request(
                method,
                f"{GOOGLE_API_ROOT}{path}",
                params=params,
                json=json_data,
                headers={"Authorization": f"Bearer {token}"},
            )

    async def _access_token(
        self, user: str, force_refresh: bool = False
    ) -> str:
        token_row = self.database.get_google_token(user)
        if not token_row:
            raise GoogleCalendarError("Google Calendar 尚未连接")
        if not force_refresh and int(token_row["expires_at"]) > int(time.time()):
            return self._decrypt(str(token_row["access_token"]))

        refresh_token = self._decrypt(str(token_row["refresh_token"]))
        if not refresh_token:
            raise GoogleCalendarError("Google 授权已过期，请重新连接")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.settings.google_client_id,
                    "client_secret": self.settings.google_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        if response.is_error:
            raise GoogleCalendarError(
                f"Google 令牌刷新失败：{self._google_error(response)}"
            )
        payload = response.json()
        access_token = str(payload.get("access_token", ""))
        if not access_token:
            raise GoogleCalendarError("Google 未返回新的访问令牌")
        self.database.set_google_token(
            user,
            self._encrypt(access_token),
            "",
            int(time.time()) + int(payload.get("expires_in", 3600)) - 60,
            str(payload.get("scope", token_row.get("scope", GOOGLE_SCOPE))),
        )
        return access_token

    def _encrypt(self, value: str) -> str:
        if not self._fernet:
            raise GoogleCalendarError("Google 令牌加密尚未配置")
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        if not value:
            return ""
        if not self._fernet:
            raise GoogleCalendarError("Google 令牌加密尚未配置")
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise GoogleCalendarError("Google 令牌无法解密") from exc

    @staticmethod
    def _google_error(response: httpx.Response) -> str:
        try:
            payload = response.json()
            message = payload.get("error", {})
            if isinstance(message, dict):
                return str(message.get("message") or message.get("status"))
            return str(message)
        except ValueError:
            return f"HTTP {response.status_code}"
