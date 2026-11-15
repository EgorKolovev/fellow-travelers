# Telegram через Cloudflare Worker

Worker пересылает запросы Bot API из YC в `api.telegram.org`. Адрес проекта:
`https://fellow-telegram-relay.maxsk-09.workers.dev`.

## Секреты

В Cloudflare → Worker → Settings → Variables and Secrets добавить два значения типа Secret:

- `BOT_TOKEN` — текущий токен продового Telegram-бота, такой же, как в YC.
- `RELAY_SHARED_SECRET` — отдельный случайный секрет; сгенерировать локально:
  `openssl rand -hex 32`.

В GitHub → Settings → Secrets and variables → Actions добавить:

- `TELEGRAM_RELAY_URL` — адрес Worker без пути, например адрес выше.
- `TELEGRAM_RELAY_SECRET` — то же значение, что `RELAY_SHARED_SECRET` в Cloudflare.

Оба параметра relay необязательны, но задаются вместе. Если они отсутствуют, бот обращается
к Telegram напрямую. Настройки передаются только контейнеру `bot`. `BOT_TOKEN` приложения
остаётся настоящим: API использует его для проверки Telegram initData.

Сессия отправляет заголовок `X-Transfer-Bot-Relay-Secret`. В URL используются заглушки
`/botrelay/{method}` и `/file/botrelay/{path}`: Worker подставляет токен из своего секрета.
Long polling ждёт 30 секунд; таймаут сессии — 65 секунд. Если Worker ограничивает ожидание
upstream, его таймаут должен быть больше времени long polling, например 75 секунд.

## Проверка и деплой

После добавления секретов проверить `getMe` с ВМ в YC. Без заголовка Worker должен возвращать 401.
Для проверки с заголовком сохранить секрет в переменной окружения `RELAY_SHARED_SECRET`:

```bash
curl --connect-timeout 10 --max-time 20 \
  -H "X-Transfer-Bot-Relay-Secret: ${RELAY_SHARED_SECRET}" \
  https://fellow-telegram-relay.maxsk-09.workers.dev/botrelay/getMe
```

Ожидается `ok: true` с данными нужного бота. Тестовый вызов `getUpdates` параллельно работающему
боту не запускать: он конкурирует с основным polling.

После попадания изменений в `main` запустить Deploy и проверить ответы бота в Telegram.
До сборки образов workflow проверит `getMe` через Worker: он должен вернуть данные бота,
соответствующего `BOT_TOKEN` проекта. Ошибка авторизации или другой бот остановит деплой.
Проверка передаёт `User-Agent: fellow-travelers-relay-check/1.0`: стандартный
`Python-urllib` блокируется Cloudflare на `workers.dev` с HTTP 403 и кодом 1010.
Простое добавление GitHub Secrets без нового деплоя конфигурацию работающей ВМ не меняет.
Для отключения relay удалить оба GitHub Secrets и повторить деплой.

Значения секретов и URL с настоящим токеном не сохранять в коде, документации или логах.
