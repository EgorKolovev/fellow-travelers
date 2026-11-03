FROM python:3.12.16-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN useradd --create-home --uid 1000 app
WORKDIR /app

# зависимости отдельным слоем: правка кода не переустанавливает пакеты
COPY requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock

COPY alembic.ini ./
COPY migrations ./migrations
COPY data ./data
COPY app ./app

USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
