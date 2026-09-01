# Base image pulled from AWS ECR Public (a mirror of the official Docker
# library) instead of docker.io, whose anonymous pulls hit 429 rate limits
# on shared build infrastructure.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

# 唯一的第三方套件：psycopg（Postgres 持久化快照用）。
# 程式在沒有 Postgres 連線設定時完全不 import 它，本機開發仍是零依賴。
RUN pip3 install --no-cache-dir "psycopg[binary]>=3.1,<4"

COPY . .

ENV PYTHONUNBUFFERED=1
# 容器一定要綁 0.0.0.0，只綁 127.0.0.1 的話平台的流量進不來，
# 使用者看到的就是「一直轉」。仍可用環境變數覆寫。
ENV APP_HOST=0.0.0.0

# 先重建索引再啟動；重建失敗（例如 Volume 唯讀）不可以讓容器直接結束，
# 服務啟動時本來就會在索引過期時自動重建。
CMD ["sh", "-c", "python3 run.py --reindex-only || echo '[boot] reindex skipped, server will rebuild if needed'; exec python3 run.py"]
