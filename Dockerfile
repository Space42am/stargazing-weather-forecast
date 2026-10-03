FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright TZ=Asia/Yerevan
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && useradd --uid 10001 --create-home weather \
    && mkdir /data && chown weather:weather /data
COPY . .
USER weather
ENV WEATHER_DB_PATH=/data/monitors.sqlite
EXPOSE 8080
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:8080", "--timeout", "60", "--access-logfile", "-", "service:create_app()"]
