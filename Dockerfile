FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY tapintel tapintel
COPY tapgame_mcp tapgame_mcp
COPY tapgame_api tapgame_api
COPY tools tools
RUN pip install --upgrade pip && pip install ".[postgres]"

RUN useradd --create-home --uid 10001 tapgame && mkdir -p /data /run/tapgame && \
    chown -R tapgame:tapgame /data /run/tapgame
USER tapgame

CMD ["python", "-m", "tapgame_api"]
