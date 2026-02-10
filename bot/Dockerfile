# ===== builder =====
FROM python:3.12-slim AS builder
# Builder stage: installs Python dependencies into a venv.
# Стадия сборки: устанавливаем зависимости Python в venv.
#
# NOTE: git is needed only here because yt-dlp is installed from the upstream repository (master).
# NOTE: git нужен только здесь, потому что yt-dlp ставим из репозитория (master).


WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
  ffmpeg \
  ca-certificates \
  curl \
  unzip \
  && rm -rf /var/lib/apt/lists/*

# Install Deno (yt-dlp YouTube EJS JS runtime)
RUN curl -fsSL https://deno.land/install.sh | sh -s -- -q \
  && ln -s /root/.deno/bin/deno /usr/local/bin/deno \
  && deno --version

# Keep builder minimal; runtime image below does NOT include git.
# Держим builder минимальным; в runtime-образ git НЕ попадает.

COPY requirements.txt /app/requirements.txt

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir -U pip && \
  pip install --no-cache-dir -r /app/requirements.txt


# ===== runtime =====
FROM python:3.12-slim
# Runtime stage: only what is required to run the bot.
# Стадия runtime: только то, что нужно для работы бота.

WORKDIR /app

# ffmpeg is used by yt-dlp for post-processing/merging streams.
# ffmpeg нужен yt-dlp для пост-обработки/склейки потоков.
RUN apt-get update && apt-get install -y --no-install-recommends \
  ffmpeg \
  ca-certificates \
  && rm -rf /var/lib/apt/lists/*
# Keep builder minimal; runtime image below does NOT include git.
# Держим builder минимальным; в runtime-образ git НЕ попадает.

COPY --from=builder /opt/venv /opt/venv
# Bring Deno from builder into runtime (needed for yt-dlp YouTube EJS)
COPY --from=builder /root/.deno /root/.deno
RUN ln -s /root/.deno/bin/deno /usr/local/bin/deno && deno --version

ENV PATH="/opt/venv/bin:$PATH"

COPY bot.py /app/bot.py

CMD ["python", "bot.py"]
