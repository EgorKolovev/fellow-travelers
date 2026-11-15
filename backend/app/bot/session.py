from collections.abc import AsyncGenerator
from typing import Any

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import ClientSession
from yarl import URL

RELAY_SECRET_HEADER = "X-Transfer-Bot-Relay-Secret"


class RelaySession(AiohttpSession):
    def __init__(self, relay_url: str, relay_secret: str) -> None:
        origin = relay_url.rstrip("/")
        # Worker подставляет BOT_TOKEN сам: настоящий токен не попадает в URL relay.
        super().__init__(
            api=TelegramAPIServer(
                base=f"{origin}/botrelay/{{method}}",
                file=f"{origin}/file/botrelay/{{path}}",
            ),
            timeout=65,
        )
        self._relay_origin = URL(origin).origin()
        self._relay_secret = relay_secret

    async def create_session(self) -> ClientSession:
        session = await super().create_session()
        session.headers[RELAY_SECRET_HEADER] = self._relay_secret
        return session

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 — сигнатура aiogram
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        # URLInputFile может скачивать сторонние URL: секрет им не передаём.
        if URL(url).origin() == self._relay_origin:
            async for chunk in super().stream_content(
                url, headers, timeout, chunk_size, raise_for_status
            ):
                yield chunk
        else:
            async with AiohttpSession() as direct:
                async for chunk in direct.stream_content(
                    url, headers, timeout, chunk_size, raise_for_status
                ):
                    yield chunk
