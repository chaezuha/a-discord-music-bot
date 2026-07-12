FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Deno: the JavaScript runtime yt-dlp needs for full YouTube support
# (https://github.com/yt-dlp/yt-dlp/wiki/EJS).
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY musicbot/ musicbot/

RUN useradd --create-home bot
USER bot

CMD ["python", "bot.py"]
