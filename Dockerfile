FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 仅安装必要依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据库默认存放于持久化卷
ENV DB_PATH=/data/exhibit.db
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动时自动建表并初始化默认账号/语言/示例展签（见 app/db.py init_db）
CMD ["sh", "-c", "python -c \"from app.db import init_db; init_db()\" && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
