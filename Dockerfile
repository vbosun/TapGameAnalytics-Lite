# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL}

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY tapintel tapintel
COPY tapgame_mcp tapgame_mcp
COPY tapgame_api tapgame_api
COPY tools tools
RUN --mount=type=cache,target=/root/.cache/pip pip install ".[postgres]"

RUN useradd --create-home --uid 10001 tapgame && mkdir -p /data /run/tapgame && \
    chown -R tapgame:tapgame /data /run/tapgame
USER tapgame

CMD ["python", "-m", "tapgame_api"]
