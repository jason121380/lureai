# Base image pulled from AWS ECR Public (a mirror of the official Docker
# library) instead of docker.io, whose anonymous pulls hit 429 rate limits
# on shared build infrastructure.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app
COPY . .

ENV PYTHONUNBUFFERED=1

# Rebuild the knowledge index for the bundled JSONL, then serve. run.py reads
# APP_HOST / PORT from the environment (Zeabur injects PORT).
CMD ["sh", "-c", "python3 run.py --reindex-only && exec python3 run.py"]
