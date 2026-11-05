# Образ бота «Вектор Знаний» для мессенджера МАКС
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot/*.py ./
COPY bot/assets ./assets

# Без root: боту не нужны права на запись, всё состояние живёт в памяти
RUN useradd --create-home --uid 10001 bot
USER bot

# Токен и настройки модели передаются при запуске: --env-file bot/.env или -e MAX_BOT_TOKEN=...
CMD ["python", "vektor_znaniy_max_bot.py"]
