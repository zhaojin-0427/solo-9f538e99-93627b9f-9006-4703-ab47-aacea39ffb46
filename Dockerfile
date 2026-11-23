FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sample ./sample

EXPOSE 8000

# 生产用 gunicorn；单份核查会话存于签名 cookie，版号核查批次存于 SQLite
# （默认 /app/instance/versioncheck.sqlite3，可用 BATCH_DB_PATH 覆盖），
# 多 worker 经同一 SQLite 文件共享批次数据。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "app.app:create_app()"]
