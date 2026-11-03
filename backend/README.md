# Fellow Travelers — backend

FastAPI + SQLAlchemy (async) + PostgreSQL. Контракт API: `../docs/contracts/`.

## Установка

```bash
python -m venv .venv
.venv/Scripts/python -m pip install --require-hashes --no-deps -r requirements-dev.lock   # Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pip install --no-deps -e .
cp .env.example .env                              # и заполнить значения
```

Точные версии с хешами лежат в `requirements.lock` (образ) и `requirements-dev.lock` (разработка и CI). В
`pyproject.toml` остаются диапазоны. После их правки выполните `make lock` из корня репозитория и закоммитьте результат.

## База данных

```bash
docker compose up -d db          # PostgreSQL для разработки и тестов; на хосте — порт 5434
```

Порт 5434 выбран, чтобы не конфликтовать с локальным PostgreSQL на 5432. Тесты с БД сами создают
базу `fellow_travelers_test` и накатывают миграции; адрес можно переопределить через `TEST_DATABASE_URL`.
Без запущенного `db` такие тесты падают; тесты без БД (`tests/matching`, `tests/test_auth_telegram.py`) от него не зависят.

## Проверки

```bash
.venv/Scripts/ruff check .
.venv/Scripts/ruff format --check .
.venv/Scripts/mypy
.venv/Scripts/pytest -q
```

## Запуск

```bash
docker compose up --build        # API на http://localhost:8000, проверка: GET /healthz
```

Без Docker: `.venv/Scripts/uvicorn app.main:app --reload` (нужен доступный PostgreSQL из `DATABASE_URL`).

## Разработка в браузере без Telegram

Короткий путь — `make dev-setup` и `make dev` из корня репозитория (см. [README](../README.md#разработка)).
Ниже — те же шаги вручную. Бэкенд принимает только initData, подписанный `BOT_TOKEN`; для разработки её подписывает сам скрипт.

```bash
# backend/.env: BOT_TOKEN=любая-строка, DEBUG=true (без DEBUG скрипт откажется; обход — --force)
# для удобства: INIT_DATA_MAX_AGE_SECONDS=604800 (initData живёт неделю)
.venv/Scripts/alembic upgrade head
.venv/Scripts/python -m app.seeds.locations          # аэропорты
.venv/Scripts/python -m app.dev.users                # 3 пользователя с телефоном (--count до 5)
.venv/Scripts/uvicorn app.main:app --reload
```

Скрипт идемпотентен и пишет `VITE_MOCK_TELEGRAM` и `VITE_DEV_USERS` в `../frontend/.env.development.local`
(путь меняется через `--frontend-env`; остальные строки файла сохраняются). Дальше — `npm run dev` во frontend.
Когда initData истечёт — перезапустите скрипт.

## Telegram-бот

Бот (aiogram 3, long polling) показывает стартовое сообщение, получает телефон и даёт кнопки открытия
мини-приложения. Запуск: `.venv/Scripts/python -m app.bot` (нужны `BOT_TOKEN`, `BOT_USERNAME`, `DATABASE_URL` в
`.env` и доступная БД) или `docker compose up --build bot`.

Состояние (запомненный код `join_<CODE>` до получения телефона) хранится в памяти бота и теряется при перезапуске.

Настройка в @BotFather: `/newapp` (или Bot Settings → Configure Mini App) — создать **Main Mini App**, указать
URL мини-приложения. Ссылки `https://t.me/<bot>?startapp=<param>` работают только при настроенном Main Mini App.

## Миграции

```bash
.venv/Scripts/alembic revision --autogenerate -m "описание"
.venv/Scripts/alembic upgrade head
# в compose: docker compose run --rm api alembic upgrade head
.venv/Scripts/python -m app.seeds.locations   # справочник аэропортов из data/airports.csv (идемпотентно)
```
