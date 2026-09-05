ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12.11-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG APP_BUILD_COMMIT=unknown
ARG APP_BUILD_TIMESTAMP=unknown
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_HOST=0.0.0.0 \
    APP_DB_PATH=/app/data/designer_coach.db \
    APP_BUILD_COMMIT=${APP_BUILD_COMMIT} \
    APP_BUILD_TIMESTAMP=${APP_BUILD_TIMESTAMP}

RUN groupadd --system --gid 10001 lureai \
    && useradd --system --uid 10001 --gid lureai --home-dir /app --shell /usr/sbin/nologin lureai

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --requirement requirements.txt
COPY --chown=lureai:lureai . .
RUN mkdir -p /app/data /app/qa && chown -R lureai:lureai /app/data /app/qa

USER 10001:10001
CMD ["sh", "-c", "python3 run.py --reindex-only || echo '[boot] reindex skipped, server will rebuild if needed'; exec python3 run.py"]
