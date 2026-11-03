# Разработка fellow-travelers (Linux/macOS). Подробности — README.md, раздел «Разработка».
#
#   make dev-setup   — один раз: venv, зависимости, backend/.env, БД, миграции, аэропорты
#   make dev         — каждый день: БД + свежие тестовые пользователи + бэкенд, воркер и фронт вместе
#   make check       — все проверки бэкенда и фронта

SHELL := /bin/bash

BACKEND  := backend
FRONTEND := frontend
VENV     := $(BACKEND)/.venv
VENV_OK  := $(VENV)/.installed
PY       := $(CURDIR)/$(VENV)/bin/python
BIN      := $(CURDIR)/$(VENV)/bin
COMPOSE  := docker compose

# Сколько тестовых пользователей создавать (1..5).
DEV_USERS ?= 3

LOCK_FLAGS := --universal --generate-hashes --python-version 3.12

.DEFAULT_GOAL := help
.PHONY: help dev-setup dev db db-stop migrate seed dev-users backend worker frontend check test lock lock-check

help: ## Список команд
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

dev-setup: $(VENV_OK) $(BACKEND)/.env $(FRONTEND)/node_modules db migrate seed dev-users ## Один раз: всё для разработки
	@echo "Готово. Запуск: make dev"

# Метка, а не каталог: новый lock-файл переустанавливает зависимости.
$(VENV_OK): $(BACKEND)/requirements-dev.lock
	test -d $(VENV) || python3.12 -m venv $(VENV)
	unset CURL_CA_BUNDLE; $(PY) -m pip install -q --require-hashes --no-deps -r $(BACKEND)/requirements-dev.lock
	unset CURL_CA_BUNDLE; $(PY) -m pip install -q --no-deps -e $(BACKEND)
	@touch $@

lock: ## Обновить backend/requirements*.lock из pyproject.toml (нужен uv)
	cd $(BACKEND) && uv pip compile pyproject.toml $(LOCK_FLAGS) -q -o requirements.lock
	cd $(BACKEND) && uv pip compile pyproject.toml --extra dev $(LOCK_FLAGS) -c requirements.lock -q -o requirements-dev.lock

lock-check: lock ## Lock-файлы соответствуют pyproject.toml (нужно закоммиченное дерево)
	git diff --exit-code -- $(BACKEND)/requirements.lock $(BACKEND)/requirements-dev.lock

# .env для разработки без Telegram: токен любой строкой, initData живёт неделю.
$(BACKEND)/.env:
	cp $(BACKEND)/.env.example $@
	sed -i 's/^BOT_TOKEN=.*/BOT_TOKEN=dev-token/; s/^BOT_USERNAME=.*/BOT_USERNAME=dev_bot/; s/^INIT_DATA_MAX_AGE_SECONDS=.*/INIT_DATA_MAX_AGE_SECONDS=604800/' $@
	@echo "Создан $@ (BOT_TOKEN=dev-token)"

$(FRONTEND)/node_modules: $(FRONTEND)/package-lock.json
	cd $(FRONTEND) && npm ci --no-audit --no-fund
	@touch $@

db: ## Поднять Postgres (порт 5434) и дождаться готовности
	$(COMPOSE) up -d --wait db

db-stop: ## Остановить Postgres (данные сохраняются)
	$(COMPOSE) stop db

migrate: ## Накатить миграции
	cd $(BACKEND) && $(BIN)/alembic upgrade head

seed: ## Справочник аэропортов (идемпотентно)
	cd $(BACKEND) && $(PY) -m app.seeds.locations

# DEBUG=true только для скрипта: env перекрывает .env, а uvicorn не печатает весь SQL.
dev-users: ## Тестовые пользователи с телефонами → frontend/.env.development.local
	cd $(BACKEND) && DEBUG=true $(PY) -m app.dev.users --count $(DEV_USERS)

backend: ## Только бэкенд: http://localhost:8000
	cd $(BACKEND) && $(BIN)/uvicorn app.main:app --reload

worker: ## Только воркер фоновых заданий (пересчёт трансферов после правки рейса)
	cd $(BACKEND) && $(PY) -m app.worker

frontend: ## Только фронт: http://localhost:5173 (/api проксируется на :8000)
	cd $(FRONTEND) && npm run dev

dev: db migrate dev-users ## Бэкенд, воркер и фронт вместе (Ctrl+C останавливает все)
	@echo "Открой http://localhost:5173 — внизу переключатель тестовых пользователей"
	@trap 'kill 0' INT TERM EXIT; \
	  (cd $(BACKEND) && $(BIN)/uvicorn app.main:app --reload) & \
	  (cd $(BACKEND) && $(PY) -m app.worker) & \
	  (cd $(FRONTEND) && npm run dev) & \
	  wait

test: $(VENV_OK) $(FRONTEND)/node_modules db ## Тесты бэкенда и фронта
	cd $(BACKEND) && $(BIN)/pytest -q -W error
	cd $(FRONTEND) && npm test

check: $(VENV_OK) $(FRONTEND)/node_modules db ## Все проверки: ruff, mypy, pytest; lint, format, typecheck, test, build, check:api
	cd $(BACKEND) && $(BIN)/ruff check . && $(BIN)/ruff format --check . && $(BIN)/mypy && $(BIN)/pytest -q -W error
	cd $(FRONTEND) && npm run lint && npm run format:check && npm run typecheck && npm test && npm run build && npm run check:api
