# Фронтенд: Telegram Mini App

Мини-приложение сервиса поиска попутчиков. Контракт с бэкендом — `../docs/contracts/` (источник истины — `openapi.yaml`).
Сейчас реализованы «Ваши данные» (CAP-01), «Мои группы», ввод кода и превью приглашения со вступлением,
создание группы и управление ей (настройки, приглашение, рейсы группы), мастер заявки (`/events/:id/apply`,
`?step=review` — сразу сводка) «Моя поездка» (`/events/:id`: рейс, попутчики, отметки, такси, действия
с подтверждениями) и вкладки управления «Участники» / «Трансферы» (`/events/:id/admin?tab=…`: сводка, фильтры
в URL, карточки участников, составы трансферов `/events/:id/admin/transfers/:tid`).

## Стек

Vite, React 19, TypeScript (strict), npm. `@telegram-apps/sdk-react` (SDK Telegram), `openapi-fetch` +
`openapi-typescript` (типы из контракта), TanStack Query, React Router (memory router), CSS Modules.
Шрифты локальные: Arimo (`@fontsource/arimo`), Martian Mono (`@fontsource/martian-mono`) и Martian Grotesk (OFL, `src/assets/fonts/`).

## Команды

```bash
npm ci               # установка зависимостей
npm run dev          # dev-сервер, /api проксируется на http://localhost:8000
npm run build        # tsc -b && vite build -> dist/
npm run preview      # предпросмотр сборки
npm run lint         # ESLint
npm run format       # Prettier (запись); format:check — проверка
npm run typecheck    # tsc -b --noEmit
npm test             # Vitest
npm run gen:api      # перегенерировать src/api/schema.d.ts из ../docs/contracts/openapi.yaml
npm run check:api    # проверить, что schema.d.ts актуален (ненулевой код при расхождении)
```

Типы API пишем только через `src/api/schema.d.ts`; после изменения контракта выполните `npm run gen:api`
и закоммитьте результат.

## Переменные окружения

См. `.env.example` (копия в `.env.local`):

| Переменная           | Назначение                                                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `VITE_API_BASE`      | Базовый URL API, по умолчанию `/api/v1`                                                                                          |
| `VITE_BOT_USERNAME`  | Username бота без `@`: открывается (с `?start=<start param>`) кнопкой «Перейти в бота», когда нет телефона (его запрашивает бот) |
| `VITE_MOCK_TELEGRAM` | `true` (только dev) — имитировать окружение Telegram (initData из `VITE_DEV_USERS` или фейковая)                                 |
| `VITE_MOCK_API`      | `true` — вместо сети отдавать фиктивный профиль                                                                                  |
| `VITE_MOCK_NO_PHONE` | `true` — профиль без телефона (с `VITE_MOCK_API`): join → `PHONE_REQUIRED`                                                       |
| `VITE_DEV_USERS`     | Только dev, с `VITE_MOCK_TELEGRAM`: тестовые пользователи с подписанной initData                                                 |

Переменные `VITE_*` подставляются при сборке (в Docker-образе — через `--build-arg`, см. «Деплой»).

## Запуск вне Telegram (моки)

```bash
cp .env.example .env.local
# в .env.local: VITE_MOCK_TELEGRAM=true, VITE_MOCK_API=true (и при необходимости VITE_MOCK_NO_PHONE=true)
npm run dev
```

Без `VITE_MOCK_TELEGRAM` в обычном браузере показывается экран «Откройте приложение из Telegram».

## Разработка в браузере с настоящим бэкендом

Короткий путь — `make dev-setup` и `make dev` из корня репозитория (см. [README](../README.md#разработка)). Ниже — как это устроено.

Бэкенд принимает только initData, подписанную `BOT_TOKEN`. Её вместе с тестовыми пользователями (с телефонами)
готовит скрипт бэкенда, подробнее — в `../backend/README.md`.

1. Бэкенд (из `../backend`, в `backend/.env`: `DEBUG=true` и любой `BOT_TOKEN`):

   ```bash
   docker compose up -d db
   alembic upgrade head
   python -m app.seeds.locations   # справочник аэропортов
   python -m app.dev.users         # тестовые пользователи -> ../frontend/.env.development.local
   uvicorn app.main:app --reload
   ```

   Скрипт пишет в `frontend/.env.development.local` (не коммитится, `*.local` в `.gitignore`)
   `VITE_MOCK_TELEGRAM=true` и `VITE_DEV_USERS` — base64url без паддинга от JSON
   `[{"label": "...", "init_data": "..."}]`.

2. Фронтенд: `npm run dev`, открыть http://localhost:5173. Внизу экрана — плашка «Dev: пользователь»:
   смена пользователя перезагружает страницу. Выбор хранится в `sessionStorage`, поэтому во второй вкладке
   можно сидеть под другим пользователем (например, админ группы и участник).

3. initData действует `INIT_DATA_MAX_AGE_SECONDS` (настройка бэкенда, по умолчанию сутки). Когда истечёт
   (API отвечает 401), перезапустите `python -m app.dev.users` и dev-сервер Vite: env-файлы читаются при старте.

Плашка и код тестовых пользователей подключаются только в dev (`import.meta.env.DEV`) и в prod-сборку не попадают.
Без `VITE_DEV_USERS` мок Telegram подставляет фейковую initData — её бэкенд отклонит, подходит только для
`VITE_MOCK_API`.

## Запуск внутри Telegram

Telegram открывает мини-приложения только по HTTPS, поэтому локальный сервер нужно вывести через туннель:

1. Запустите бэкенд (`../backend`, API на `:8000`) и `npm run dev`.
2. Поднимите HTTPS-туннель на порт Vite (по умолчанию 5173), например
   `cloudflared tunnel --url http://localhost:5173` или `ngrok http 5173`. Для Vite может понадобиться
   `server.allowedHosts` (или переменная `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS`) для домена туннеля.
3. В `@BotFather`: `/mybots` → бот → **Bot Settings → Configure Mini App** (Main Mini App) → указать
   HTTPS-URL туннеля. Ссылки вида `https://t.me/<bot>?startapp=<param>` открывают это приложение.
4. Откройте бота в Telegram и запустите мини-приложение.

## Деплой

Docker-образ собирается в два этапа (`node:24-alpine` -> `nginx:1.27-alpine`), статика отдаётся nginx
(`nginx.conf`): `/api/` проксируется на сервис `api:8000`, `/assets/` кешируется навсегда, остальное —
fallback на `index.html`.
Защитные заголовки (CSP, `nosniff`, `Referrer-Policy`) — в `security-headers.conf`; он подключается
в `server` и в каждом `location` со своим `add_header`. Inline-скриптов в сборке нет (`script-src 'self'`).

```bash
cd ../backend
docker compose up -d --build   # db, api, bot, web (web: http://localhost:8080)
```

Username бота для кнопки «Поделиться номером» (`VITE_BOT_USERNAME`) вшивается в бандл при сборке:
compose берёт его из переменной `BOT_USERNAME` в shell или в `backend/.env`. Для ручной сборки:
`docker build --build-arg VITE_BOT_USERNAME=my_bot -t fellow-web ./frontend`.

TLS в compose не терминируется. Telegram требует HTTPS, поэтому перед `web` (порт 8080) нужен внешний
reverse-proxy (Caddy, nginx, Traefik) с сертификатом либо туннель. Этот публичный HTTPS-URL и указывается
в BotFather как Main Mini App. Reverse-proxy должен передавать `X-Forwarded-Proto`.
