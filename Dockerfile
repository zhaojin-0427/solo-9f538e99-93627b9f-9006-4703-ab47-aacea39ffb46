FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sample ./sample

EXPOSE 8000

# 单 worker + 多线程：批次状态保存在进程内存中，单 worker 保证状态全局一致；
# 线程并发由 engine/store.py 内的锁保证原子提交与下载快照一致性。
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "60", "app.app:create_app()"]
