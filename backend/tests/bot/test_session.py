from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError
from aiohttp import web
from pydantic import ValidationError

from app.bot.session import RELAY_SECRET_HEADER, RelaySession
from app.config import Settings
from tests.helpers.telegram import BOT_TOKEN


@asynccontextmanager
async def serve(app: web.Application) -> AsyncIterator[str]:
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        host, port = runner.addresses[0]
        yield f"http://{host}:{port}"
    finally:
        await runner.cleanup()


async def test_relay_polling_and_session_recreation() -> None:
    calls: list[dict[str, Any]] = []

    async def updates(request: web.Request) -> web.Response:
        calls.append(
            {
                "path": request.path,
                "secret": request.headers.get(RELAY_SECRET_HEADER),
                "form": dict(await request.post()),
            }
        )
        return web.json_response({"ok": True, "result": []})

    app = web.Application()
    app.router.add_post("/botrelay/getUpdates", updates)
    async with serve(app) as origin:
        session = RelaySession(origin, "relay-test-secret")
        async with Bot(BOT_TOKEN, session=session) as bot:
            assert await bot.get_updates(offset=123, timeout=30) == []
            await session.close()
            assert await bot.get_updates(offset=124, timeout=30) == []
    assert [call["form"] for call in calls] == [
        {"offset": "123", "timeout": "30"},
        {"offset": "124", "timeout": "30"},
    ]
    assert all(call["path"] == "/botrelay/getUpdates" for call in calls)
    assert all(call["secret"] == "relay-test-secret" for call in calls)


async def test_download_headers_are_scoped_to_relay() -> None:
    received: list[str | None] = []

    async def download(request: web.Request) -> web.Response:
        received.append(request.headers.get(RELAY_SECRET_HEADER))
        return web.Response(body=b"file-data")

    relay = web.Application()
    relay.router.add_get("/file/botrelay/documents/test.txt", download)
    external = web.Application()
    external.router.add_get("/external.txt", download)
    async with (
        serve(relay) as origin,
        serve(external) as other,
        RelaySession(origin, "relay-test-secret") as session,
    ):
        url = session.api.file_url(BOT_TOKEN, "documents/test.txt")
        assert BOT_TOKEN not in url
        assert b"".join([chunk async for chunk in session.stream_content(url)]) == b"file-data"
        assert (
            b"".join([chunk async for chunk in session.stream_content(f"{other}/external.txt")])
            == b"file-data"
        )
    assert received == ["relay-test-secret", None]


async def test_relay_error_is_preserved() -> None:
    async def unauthorized(request: web.Request) -> web.Response:
        return web.json_response(
            {"ok": False, "error_code": 401, "description": "Unauthorized"}, status=401
        )

    app = web.Application()
    app.router.add_post("/botrelay/getMe", unauthorized)
    async with (
        serve(app) as origin,
        Bot(BOT_TOKEN, session=RelaySession(origin, "wrong-secret")) as bot,
    ):
        with pytest.raises(TelegramUnauthorizedError):
            await bot.get_me()


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://relay.example", ""),
        ("", "secret"),
        ("http://relay.example", "secret"),
        ("https://user:password@relay.example", "secret"),
        ("https://relay.example/path", "secret"),
        ("https://relay.example/?query=1", "secret"),
        ("https://relay.example/#fragment", "secret"),
    ],
)
def test_relay_rejects_incomplete_or_unsafe_settings(url: str, secret: str) -> None:
    with pytest.raises(ValidationError):
        Settings(telegram_relay_url=url, telegram_relay_secret=secret)


@pytest.mark.parametrize("url", [None, "", "https://relay.example/"])
def test_relay_settings_are_optional(url: str | None) -> None:
    settings = Settings(
        telegram_relay_url=url,
        telegram_relay_secret="secret" if url else "",
    )
    assert bool(settings.telegram_relay_url) == bool(url)
