# Бэкенд: техническая разбивка

Требования и этапы — в [`Capabilities_Poputchiki.md`](Capabilities_Poputchiki.md) (источник истины), API —
в [`contracts/`](contracts/). Здесь только **как** делаем бэкенд и в каком порядке. Задачи сформулированы так,
чтобы оркестратор мог отдать каждую агенту `implementer` (см. `CLAUDE.md`).

## Стек

| Что | Выбор | Почему |
| --- | --- | --- |
| Язык | Python 3.12 | `backend/.gitignore` уже под Python |
| HTTP API | FastAPI + Pydantic v2 | OpenAPI из коробки — легко сверять с контрактом |
| БД | PostgreSQL 16, SQLAlchemy 2 (async), Alembic | `timestamptz`, блокировки для подбора |
| Бот | aiogram 3, тот же процесс/репозиторий, общий слой сервисов | Бот только сохраняет телефон и открывает мини-приложение |
| Фон | Таблица-очередь `background_jobs` в Postgres и процесс `worker` | Задание пишется в одной транзакции с правкой; Celery, Redis и APScheduler не нужны |
| Тесты | pytest, pytest-asyncio, httpx, testcontainers-postgres | |
| Качество | ruff (lint + format), mypy | |
| Наблюдаемость | JSON-логи на stdlib, prometheus-client | Без внешних сервисов; хранилище подключается позже |
| Зависимости | `pyproject.toml` (диапазоны) и `requirements*.lock` с хешами | Сборка воспроизводима; обновление — `make lock` (uv) |
| Запуск | Docker Compose: `api`, `bot`, `worker`, `db` | |

## Структура `backend/`

```
backend/
  pyproject.toml  alembic.ini  Dockerfile  docker-compose.yml  .env.example
  app/
    main.py                 # FastAPI app, роутеры, обработчики ошибок → формат Error из контракта
    config.py               # pydantic-settings: DATABASE_URL, BOT_TOKEN, BOT_USERNAME, ...
    db.py                   # engine, session
    auth/telegram.py        # проверка initData (HMAC), зависимость current_user
    errors.py               # AppError(code, message, http_status, details)
    models/                 # ORM: user, event, membership, location, trip, application, transfer
    schemas/                # Pydantic-модели строго по openapi.yaml
    services/               # бизнес-логика, без HTTP: events, invites, trips, applications, transfers
    matching/               # ЧИСТЫЕ функции подбора, без БД (CAP-06)
    api/v1/                 # роутеры: me, events, invites, trips, applications, admin
    bot/                    # aiogram: /start, приём контакта, кнопки WebApp
  migrations/
  tests/  unit/ (matching, auth)  api/ (по эндпоинтам)
  data/airports.csv         # справочник аэропортов (IATA, город, страна, IANA tz) для сидов
```

## Модель данных (pre-MVP)

| Таблица | Ключевые поля | Ограничения |
| --- | --- | --- |
| `users` | telegram_id (unique), first_name, last_name, username, phone, photo_url | |
| `events` | title, starts_on, ends_on, admin_id → users, meeting_point/pickup_point/destination (JSONB: address, lat, lon), meeting_instruction, chat_url, join_open, invite_code (unique) | ends_on ≥ starts_on |
| `memberships` | event_id, user_id, joined_at | unique(event_id, user_id); админ сюда попадает, только если вступил как участник |
| `locations` | kind (airport/station), code, name, city, country, timezone | unique(kind, code) |
| `trips` | event_id, mode, number, departure_location_id, arrival_location_id, scheduled_departure, scheduled_arrival, estimated_arrival, status, source, provider_ref, updated_at | рейс принадлежит группе (ручной список, CAP-05) |
| `applications` | event_id, user_id, trip_id, with_companion, baggage_count, max_wait_minutes, status, at_meeting_point, departed_at, transfer_id | unique(event_id, user_id); baggage 0..2 |
| `transfers` | event_id, status, created_at, created_by (auto/admin) | |

`effective_arrival = coalesce(estimated_arrival, scheduled_arrival)` — подбор опирается только на него, а не на
формат поставщика (задел на поезда из «Задел на другие виды транспорта»).

## Подбор

**Правило совместимости (наше допущение, вопрос открыт в «Как применять ожидание»):** все ждут самого позднего
прилетающего, и каждый — в пределах своего ожидания: `max(t) − t_i ≤ wait_i` для каждого участника. Плюс
одинаковая точка прилёта (`arrival_location_id`), Σ пассажиров ≤ 3, Σ багажа ≤ 2.
Пример А/Б из документа (А 10:00/15 мин, Б 10:10/0 мин) — совместимы.

**Размещение заявки** (`matching.find_placement(new, open_transfers, free)`):
1. Открытые трансферы: `active`, собраны автоматически (`origin=auto`), никто не уехал.
2. Варианты: посадить заявку в открытый трансфер (вся группа проходит `is_compatible`) или собрать новую
   группу из 2–3 заявок через `find_group` среди заявок в `searching` без `departed_at`.
3. Лучший вариант: больше пассажиров в машине, затем больше размещённых заявок, затем меньше разброс времени
   и сумма ожиданий; при равенстве решают id. Новая полная тройка сильнее дополнения, дополнение сильнее новой пары.
4. Трансфер создаётся, только если в нём ≥ 2 заявки. Иначе заявка остаётся в `searching`.

Подбор при `PUT` и `resume-search` синхронный: ответ сразу отражает результат.

**Заполнение** (`services.matching.fill_event`): заявки группы в `searching` по порядку подачи проходят
размещение. Вызывается после выхода из трансфера и в конце пересчёта.

**Пересчёт** (`services.matching.reconcile_event`): запускается воркером после правки времени прибытия или
аэропорта рейса. Для каждого активного трансфера `matching.members_to_exclude` оставляет наибольшую совместимую
подгруппу неуехавших. Исключённые получают `searching`, трансфер с одной заявкой расформировывается, затем
идёт заполнение. Повторный запуск ничего не меняет.

Правило совместимости — отдельная функция `is_compatible(group)`: после ответа заказчика меняется только она.
Все сценарии, меняющие заявки и трансферы группы, идут в транзакции под `pg_advisory_xact_lock(event_id)`, чтобы
два одновременных запроса не собрали одну заявку в два трансфера.

## Фоновые задания

Очередь — таблица `background_jobs` в той же БД. Задание создаётся в одной транзакции с правкой, поэтому не
теряется и не появляется без неё. Процесс `python -m app.worker` (сервис `worker` в compose) раз в 2 с забирает
готовое задание через `FOR UPDATE SKIP LOCKED`, выполняет его и удаляет в той же транзакции.

При ошибке `attempts` растёт, повтор идёт через `min(5·2^n, 900)` с. После 8 попыток задание получает `failed`.
Вернуть его в работу: `UPDATE background_jobs SET status = 'pending', attempts = 0, run_after = now() WHERE id = ...`.

На группу и вид задания ждёт не больше одного задания: частичный уникальный индекс по `pending` и
`INSERT ... ON CONFLICT DO NOTHING`. Новые правки сливаются с уже ждущим заданием. `trip_id` при этом остаётся
от первой правки, но пересчёт проверяет все активные трансферы группы. `DO UPDATE` не годится: производитель
держит блокировку группы, воркер держит строку задания и ждёт ту же блокировку, получился бы дедлок.

Миграция 0004 ставит по одному заданию с `trip_id = NULL` на каждую текущую группу (`ends_on >= current_date`).
Первый проход воркера после обновления дополняет пары, собранные по старым правилам, и разбивает те, что стали
несовместимы после прежних правок рейсов.

Сейчас вид заданий один — `reconcile_trip`. Опрос рейсов M-01 станет вторым циклом воркера.

## Аудит и наблюдаемость

- `audit_log` — append-only история: действие, причина, актор (участник, админ, система), заявка, трансфер, рейс,
  `request_id` или `job_id`. Триггер запрещает UPDATE и DELETE, кроме каскадов. Телефонов нет. Читается через
  `GET /events/{id}/admin/history`, удаляется вместе с группой.
- Логи — JSON в stdout с `request_id`. В логах нет телефонов, имён, username, telegram_id, initData и invite-кодов.
- Метрики — формат Prometheus: `/metrics` у `api` (наружу не проброшен), порт 9100 у `worker`. Хранилища пока нет.
- `/healthz` — процесс жив, `/readyz` — есть связь с БД.

## Статус (на 2026-10-03, ветка `feature/backend-contracts`)

| Задача | Статус |
| --- | --- |
| B-01 … B-12 | Готово, после ревью, закоммичено. 510 тестов, ruff и mypy strict чистые |
| MVP (M-01 … M-06) | Не начато |
| Правки по ревью | В работе, критерии — `docs/spec-review-fixes.md` |

Запуск тестов: `docker compose up -d db` в `backend/`, затем `.venv/Scripts/pytest -q -W error`.

## Задачи pre-MVP

Порядок: **сверху вниз**; задачи в одной волне независимы и могут идти параллельно.
Критерий готовности каждой задачи — эндпоинты соответствуют `openapi.yaml`, тесты зелёные, `ruff` и `mypy` чистые.

**Волна 0 — каркас**
- **B-01 Каркас проекта.** pyproject (uv), структура выше, config, db, docker-compose (`api`, `db`), ruff/mypy/pytest,
  `GET /healthz`. Обработчик ошибок → `Error` из контракта (включая 422 с `details.fields`).

**Волна 1 — фундамент** (после B-01, параллельно)
- **B-02 Модели и миграции.** Все таблицы выше, первая миграция Alembic, сид `locations` из `data/airports.csv`.
- **B-03 Авторизация Telegram.** Проверка `initData` (HMAC, `auth_date` ≤ 24 ч) и зависимость, отдающая
  проверенного Telegram-пользователя — без БД. Unit-тесты с подписанными фикстурами. (CAP-01)
- **B-04 Модуль подбора.** `matching/` — чистые функции, без БД. Таблица тестов: вместимость 3/2, спутник = 2
  места, ожидание (пример А/Б, тройки), разные аэропорты, переход через полночь, детерминированность. (CAP-06)

**Волна 2 — API** (после B-02 и B-03, параллельно)
- **B-05 Профиль и группы.** `current_user`: создание пользователя при первом входе и обновление
  имени/username/фото при каждом; `GET /me`, `GET /me/events`, `POST/GET/PATCH /events/{id}`, `GET /events/{id}/invite`;
  генерация `invite_code` (6 символов, без похожих O/0, I/1); права: управляет только создатель. (CAP-01, 02, 07)
- **B-06 Приглашения.** `GET /invites/{code}`, `POST /invites/{code}/join` — идемпотентно, `JOIN_CLOSED`,
  код без учёта регистра. (CAP-03)
- **B-07 Рейсы.** `GET /locations`, `GET/POST /events/{id}/trips`, `PATCH/DELETE /trips/{id}`; нормализация номера,
  интерпретация времени без смещения в таймзоне аэропорта, вывод со смещением аэропорта, `TRIP_IN_USE`. (CAP-05)

**Волна 3 — заявка и подбор** (после B-04…B-07)
- **B-08 Заявка.** `GET/PUT /my-application`, `go-solo`, `resume-search`, `cancel`, `PUT /marks`; машина состояний
  из `contracts/README.md` §5; `PHONE_REQUIRED`, `APPLICATION_LOCKED`, `ALREADY_DEPARTED`; выход из трансфера
  оставляет остальных вместе; трансфер с 1 заявкой расформировывается. Вызов подбора после PUT и
  resume-search. (CAP-04, 06, 08)
- **B-09 Экран поездки.** `GET /events/{id}/my-trip`: событие, заявка, трансфер с контактами только своего
  трансфера, блок `taxi` (`yandex_go_url = null`). (CAP-07, 09, 10)

**Волна 4** (параллельно)
- **B-10 Админ.** `GET /admin/participants` с фильтрами и счётчиками (спутники считаются, `cancelled` не входит
  в ожидаемых), `GET /admin/transfers`. (CAP-11)
- **B-11 Бот.** `/start` (в т.ч. `/start join_<CODE>`): стартовое сообщение, запрос контакта
  (`request_contact`), сохранение телефона (только свой контакт: `contact.user_id == from.id`), кнопки WebApp
  «Открыть» и «Управление». (CAP-01)

**Волна 5**
- **B-12 Сквозная проверка pre-MVP.** API-тест сценария из «Проверка pre-MVP» (шаги 1–6) целиком.

```
B-01 ─┬─ B-02 ─┬─ B-05 ─┐
      ├─ B-03 ─┼─ B-06 ─┼─ B-08 ─ B-09 ─┬─ B-10 ─┐
      └─ B-04 ─┘  B-07 ─┘               └─ B-11 ─┴─ B-12
```

## MVP (после pre-MVP, детализируем позже)

- **M-01 Поставщик рейсов**: интерфейс `FlightProvider` + адаптер (FlightAware?), `GET /flights/search`,
  воркер с интервалами из CAP-05, `FLIGHT_PROVIDER_UNAVAILABLE`.
- **M-02 Обработка изменений**: правка назначенной заявки. Дозаполнение освободившихся мест и исключение при
  несовместимой задержке уже работают; в MVP задержки приходят от поставщика через тот же пересчёт.
- **M-03 Админ**: ручные составы, отметки отъезда, табло `GET /admin/board`, закрытие вступления.
- **M-04 Уведомления** через бота с deep link `event_<id>`; без дублей и без сообщений при обновлении без изменений.
- **M-05 Яндекс Go / карты**: `yandex_go_url`, координаты точек.
- **M-06 Удаление данных** через неделю — после ответа на вопросы из «От какого момента удалять данные».

## Допущения, которые надо подтвердить

Вопросы из «Требует уточнения», без ответа на которые мы выбрали вариант сами:

| Вопрос | Допущение | Где в коде |
| --- | --- | --- |
| Ожидание при подборе | Каждый ждёт самого позднего в пределах своего ожидания | `matching.is_compatible` |
| Разные аэропорты в группе | Подбираем только при одинаковом аэропорте прилёта; точка посадки одна на группу | `matching`, модель `events` |
| Исправление «Уехал» | На pre-MVP отметка необратима | B-08 |
| Багаж > 2 мест | 422 на заявке; фронт предлагает исправить или ехать самому | схема `ApplicationInput` |
| Ручной ввод телефона | Только передача контакта боту | B-11 |
| Размер трансфера | Создаём при ≥ 2 заявках; одиночная заявка со спутником остаётся в поиске | `matching.find_group` |
| Остаток трансфера уехал | При расформировании уехавший получает `solo`, не `searching` | `services/matching.leave_transfer` |
| Дополнение трансфера | Новая заявка садится в автоматический трансфер со свободным местом, пока никто не уехал | `matching.find_placement` |
| Задержка рейса | Исключаем, только если новое время нарушает совместимость; задержка в пределах ожидания состав не меняет | `matching.members_to_exclude` |
| Статус исключённого | `searching`: от попутчиков участник не отказывался | `services/matching.reconcile_event` |
| Ручные составы | Не дополняем до ответа заказчика | `services/matching` |

## Вопросы к заказчику, выявленные при реализации

Открытых вопросов нет. Вопрос «Тройки почти не собираются» закрыт 2026-11-03 после ревью: выбран вариант (а),
дополнение активного трансфера со свободными местами, пока никто не уехал. Пакетный подбор перед прилётом
отклонён: без уведомлений участник в полёте не узнал бы о попутчиках.
