"""Проверка relay до сборки и деплоя; значения секретов в лог не выводятся."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        # Произвольный Location не должен получить секретный заголовок.
        return None


def main() -> int:
    url = os.environ.get("TELEGRAM_RELAY_URL", "").rstrip("/")
    secret = os.environ.get("TELEGRAM_RELAY_SECRET", "")
    if not url and not secret:
        print("Telegram relay отключён")
        return 0
    if not url or not secret:
        print("::error::TELEGRAM_RELAY_URL и TELEGRAM_RELAY_SECRET задаются вместе")
        return 1
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        print("::error::Некорректный HTTPS-адрес Telegram relay")
        return 1
    request = urllib.request.Request(
        f"{url}/botrelay/getMe",
        data=b"",
        headers={
            "X-Transfer-Bot-Relay-Secret": secret,
            # workers.dev блокирует стандартный Python-urllib с HTTP 403 / 1010.
            "User-Agent": "fellow-travelers-relay-check/1.0",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=20
        ) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise TypeError("Expected a Bot API response")
        if (
            not result.get("ok")
            or str(result["result"]["id"]) != os.environ["BOT_TOKEN"].split(":", 1)[0]
        ):
            print("::error::Telegram relay должен использовать BOT_TOKEN этого проекта")
            return 1
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and b"error code: 1010" in exc.read(4096):
            print(
                "::error::Cloudflare блокирует HTTP-клиент (1010); проверьте User-Agent"
            )
        elif exc.code == 401:
            print(
                "::error::Telegram relay: HTTP 401; проверьте RELAY_SHARED_SECRET и BOT_TOKEN Worker"
            )
        else:
            print(
                f"::error::Telegram relay вернул HTTP {exc.code}; проверьте Worker и upstream"
            )
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print("::error::Telegram relay недоступен или вернул некорректный ответ")
        return 1
    print("Telegram relay: getMe успешно, токен соответствует боту проекта")
    return 0


if __name__ == "__main__":
    sys.exit(main())
