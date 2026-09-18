# AI 行程规划系统 —— 生产镜像
# 构建：docker build -t ai-trip-planner .
# 运行：docker run -p 8000:8000 --env-file backend/.env -v $(pwd)/data:/app/data ai-trip-planner
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装依赖：利用 Docker 层缓存，改代码不必重装依赖
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# 再拷贝源码（.dockerignore 已排除 .env / data / 缓存）
COPY backend/ backend/
COPY static/ static/

# 运行数据目录（规划快照、收藏、媒体缓存）——生产环境请挂载卷持久化
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 8000

# 单 worker 即可：求解为 CPU 内密集小任务，多实例请横向扩展而非单机多 worker
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
