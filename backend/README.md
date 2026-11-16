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
Lock-файлы генерируются uv 0.10.3 — та же версия запинена в CI; `make lock-check` перегенерирует их и сравнивает
байт-в-байт, поэтому локальная версия uv должна совпадать с версией в CI.

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

## Воркер фоновых заданий

Процесс `python -m app.worker` (сервис `worker` в compose, `make worker` без Docker) раз в 2 секунды забирает из
таблицы `background_jobs` готовое задание и выполняет его. Сейчас заданий один вид: `reconcile_trip`. Его ставит
`PATCH /trips/{id}` в той же транзакции, если изменилось расчётное прибытие или аэропорт прилёта. Воркер
проверяет трансферы группы: заявки, которым новое время не подходит, возвращаются в поиск, освободившиеся места
занимают ждущие заявки. Без воркера правка рейса сохраняется, но трансферы не пересчитываются.

При ошибке задание повторяется через `min(5·2^n, 900)` секунд, где `n` — число прошлых неудач. В `last_error`
лежит только имя класса исключения. После 8 неудач задание получает статус `failed` и больше не берётся, в лог
уходит запись уровня ERROR. Увидеть такие задания и вернуть одно в работу:

```sql
SELECT id, event_id, kind, attempts, last_error FROM background_jobs WHERE status = 'failed';
UPDATE background_jobs SET status = 'pending', attempts = 0, run_after = now() WHERE id = '...';
```

Метрики Prometheus воркер отдаёт на порту `WORKER_METRICS_PORT` (по умолчанию 9100), `GET /metrics`; порт
не публикуется наружу, читать его можно из сети compose. Кроме метрик подбора (`ft_match_*`,
`ft_transfer_*`) там есть `ft_worker_jobs_total`, `ft_worker_job_duration_seconds`, `ft_worker_queue_depth`
(по статусам `pending` и `failed`), `ft_worker_oldest_job_age_seconds` и `ft_worker_last_poll_timestamp_seconds`.
Счётчики растут только после commit: откатившийся пересчёт их не меняет. Healthcheck сервиса
(`python -m app.worker.healthcheck`) успешен, если последний опрос очереди был не больше 60 секунд назад.

Кроме заданий, воркер ведёт ещё три цикла (MVP):

- **Опрос рейсов поставщика** раз в 30 секунд, если `FLIGHT_PROVIDER` не `none`. Опрашиваются только рейсы с
  активными заявками; расписание — Д2 в `docs/spec-mvp.md`. Изменения пишутся в историю от `system` и ставят
  пересчёт трансферов. Сейчас доступны `none` (по умолчанию: поиск рейсов отвечает 503) и `fake` (тестовый
  поставщик в памяти, только для разработки).
- **Отправка уведомлений** через бота раз в 5 секунд. Строки `notifications` создаются при commit изменений.
  На человека в группе есть не больше одной неотправленной строки (`pending`): новое событие склеивается в
  неё (другой вид события даёт общий текст «несколько изменений»). Новая строка ждёт 30 секунд, чтобы частые
  изменения склеились, а после 6 сообщений за час человеку по одной группе следующая откладывается до выхода
  самого раннего из окна часа (`docs/spec-mvp.md`, M-04a). Воркер арендует строку (`sending`) и ставит `sent`,
  `skipped` (пользователь не писал боту или заблокировал его) или, после 8 неудач, `failed`; повтор после
  ошибки остаётся в `sending` с паузой. Воркеру нужны `BOT_TOKEN`, `BOT_USERNAME` и, если ВМ не видит Telegram,
  `TELEGRAM_RELAY_URL` и `TELEGRAM_RELAY_SECRET` (`docs/telegram-relay.md`). Без настоящего `BOT_TOKEN` воркер
  работает, но уведомления не отправляет. Метрики — `ft_notifications_total{kind,outcome}` и
  `ft_notifications_planned_total{outcome}` (`created`, `merged`, `deferred`). Название группы в тексте
  очищается от ссылок, доменов, `@`, `#`, команд, e-mail и телефонов и обрезается до 60 символов
  (`notification_text.safe_title`): название задаёт админ, а сообщение приходит от официального бота.
- **Удаление данных** раз в час, только при `RETENTION_ENABLED=true`. Группа удаляется целиком через 7 суток
  после самого позднего из моментов: конец дня `ends_on`, последняя отметка «Уехал», создание и последняя
  правка группы. Удаление необратимо; пользователи
  остаются.

В проде `FLIGHT_PROVIDER` и `RETENTION_ENABLED` задаются секретами репозитория с теми же именами; без секретов
действуют `none` и `false`. Деплой (`.github/workflows/deploy.yml`) подставляет их в
`deploy/docker-compose.prod.yml` через `envsubst`, поэтому там стоят простые `${FLIGHT_PROVIDER}` и
`${RETENTION_ENABLED}`: выражения вида `${VAR:-default}` `envsubst` не подставляет. Перед первым включением
`RETENTION_ENABLED` сделайте дамп БД: удаление необратимо.

```sql
SELECT status, count(*) FROM notifications GROUP BY status;
SELECT id, kind, attempts, last_error FROM notifications WHERE status = 'failed' ORDER BY id DESC LIMIT 20;
-- вернуть в отправку: через sending, чтобы не столкнуться с новой pending того же человека
UPDATE notifications SET status = 'sending', attempts = 0, run_after = now() WHERE id = ...;
```

Остановка по SIGTERM или Ctrl+C: текущее задание доводится до конца, затем процесс выходит. Несколько
экземпляров воркера безопасны: задания делятся через `FOR UPDATE SKIP LOCKED`, а изменения одной группы
сериализует advisory-блокировка.

## Миграции

```bash
.venv/Scripts/alembic revision --autogenerate -m "описание"
.venv/Scripts/alembic upgrade head
# в compose: docker compose run --rm api alembic upgrade head
.venv/Scripts/python -m app.seeds.locations   # справочник аэропортов из data/airports.csv (идемпотентно)
```

## Откат релиза в проде

Контейнер `api` при старте выполняет `alembic upgrade head`. Старый образ не знает новых ревизий, поэтому
после простого revert он падает с «Can't locate revision» и перезапускается по кругу. Схему нужно откатить
**до** revert, пока на ВМ работает новый образ.

1. Сохранить дамп: `sudo docker exec db pg_dump -U fellow -d fellow_travelers | gzip > ~/pre-rollback.sql.gz`.
2. Откатить схему новым образом до ревизии старого релиза:
   - релиз MVP → релиз с историей и воркером: `0004`. Сначала остановить `worker` и `bot`
     (`sudo docker stop worker bot`): новый код пишет историю с причинами MVP до самого revert. Затем у
     записей истории с причинами MVP обнулить причину: старый код их не знает, а миграция `0005` не вернёт
     старое ограничение, пока они есть. Строки остаются, поэтому составы трансферов по истории
     восстанавливаются, как раньше. Таблица истории защищена триггером «только добавление», поэтому на время
     правки он отключается:

     ```bash
     sudo docker exec -i db psql -U fellow -d fellow_travelers <<'SQL'
     BEGIN;
     ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only;
     UPDATE audit_log SET reason = NULL WHERE reason IN
       ('application_changed', 'departure_undone', 'flight_cancelled', 'provider_update');
     ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only;
     COMMIT;
     SQL
     sudo docker exec api alembic downgrade 0004
     ```

     Если `downgrade` упал на проверке `ck_audit_log_audit_reason` (успела появиться новая запись), схема
     остаётся прежней: повторите SQL и `downgrade`. Удаляются таблица `notifications` (неотправленные
     уведомления пропадут), поля опроса рейсов и `applications.departed_from_transfer_id`. Заявки, трансферы
     и рейсы остаются.
   - релиз с историей и воркером → pre-MVP: `sudo docker exec api alembic downgrade 0002`. Таблицы
     `audit_log` и `background_jobs` удаляются вместе с данными: история с момента обновления пропадёт, она
     остаётся только в дампе.
3. Сделать revert слияния в `main` и запушить: деплой соберёт и поставит старые образы.
4. Проверить: `curl -s -o /dev/null -w '%{http_code}' https://<сайт>/api/v1/me` даёт 401, а не 502.

Заявки и трансферы при откате схемы не меняются. Составы, которые воркер успел дополнить или разбить, остаются
такими же и после отката.
