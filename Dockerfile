FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# Зависимости из uv.lock (--frozen = строго по локу + проверка хешей)
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen
ENV PATH="/app/.venv/bin:$PATH"
COPY src/ .
# Do not run as root: /app stays root-owned and read-only to the bot, which
# only ever writes to /data. Compose overrides the numeric UID and GID.
RUN useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin app \
 && install -d -o 1000 -g 1000 /data
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('https://api.telegram.org', timeout=5)" || exit 1
CMD ["python", "bot.py"]
