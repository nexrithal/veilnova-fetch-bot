# Veilnova Fetch Node

**Veilnova Fetch Node** is a self-hosted Telegram bot designed for downloading media from user-provided URLs  
using **yt-dlp** and **gallery-dl**, and sending the resulting files back to Telegram chats.

> This project is built as an engineering tool:  
> self-hosted, transparent, reproducible, and free from dependency on third-party download bots.

---

## ✨ Key Features

- Support for **hundreds of popular platforms** (YouTube, TikTok, Instagram, Twitter/X, Reddit, Pinterest, etc.)
- Primary engine: **yt-dlp** (video, streaming, HLS/DASH)
- Fallback engine: **gallery-dl** (galleries, posts, Pinterest, Instagram carousels)
- Local **telegram-bot-api (TDLib)** server in Docker
- Unified **cookies.txt** support (multiple platforms in one file)
- Pre-download size checks and upload limits
- Stable handling of large files via local Bot API
- Docker-first architecture (dev → prod without logic changes)

---

## 🧱 Architecture Overview

### High-level Flow

1. User sends a URL to the bot  
2. Bot selects a download strategy:
   - `yt-dlp` — primary path for video and streaming
   - `gallery-dl` — fallback for galleries and post-based sources
3. Media is downloaded into a temporary directory (`/data` volume)
4. Files are uploaded back to the chat via Telegram Bot API

### Docker Services

- **bot**  
  Python bot (aiogram v3) that handles message parsing, downloading, and sending files

- **telegram-bot-api**  
  Local Bot API server based on **TDLib**, used to:
  - improve upload stability
  - handle large media files more reliably

---

## 📁 Repository Structure

```
.
├── docker-compose.yml        # bot + telegram-bot-api services
├── bot/
│   ├── Dockerfile            # multi-stage build (builder/runtime)
│   ├── bot.py                # main bot logic
│   └── requirements.txt      # Python dependencies
├── .env.example              # environment variables template
├── secrets/
│   └── cookies.txt           # cookies file (DO NOT COMMIT)
└── README.md
```

---

## ⚙️ Requirements

- Docker Desktop (Windows) or Docker Engine (Linux)
- Telegram bot token (`BOT_TOKEN`) from **@BotFather**
- TDLib credentials:
  - `TELEGRAM_API_ID`
  - `TELEGRAM_API_HASH`  
  Obtained at https://my.telegram.org → *API development tools*

---

## 🚀 Quick Start (Development)

### 1. Environment Setup

```bash
cp .env.example .env
```

Fill in:
- `BOT_TOKEN`
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`

### 2. Cookies (Optional but Recommended)

Create:
```
secrets/cookies.txt
```

- Format: **Netscape cookies format**
- You may store cookies for **multiple platforms in one file**
- The file is mounted read-only into the container

### 3. Start Services

```bash
docker compose up -d --build
```

### 4. Logs

```bash
docker compose logs -f bot
docker compose logs -f telegram-bot-api
```

### 5. Stop

```bash
docker compose down
```

---

## 🔄 Development vs Production

### Development Mode (Default)

`docker-compose.yml` uses a bind mount:

```yaml
- ./bot:/app
```

This allows:
- instant code updates
- no rebuilds during development

### Production Mode (Recommended)

Remove the bind mount:

```yaml
# - ./bot:/app
```

Then rebuild:

```bash
docker compose up -d --build
```

The container will run code baked into the image.

---

## 🔐 Environment Variables

See `.env.example` for full documentation.

Key variables:

- `BOT_TOKEN` — Telegram bot token
- `TELEGRAM_API_ID / TELEGRAM_API_HASH` — TDLib credentials
- `BOT_API_BASE_URL` — Local Bot API endpoint (`http://telegram-bot-api:8081`)
- `DOWNLOAD_DIR` — Temporary download directory (`/data`)
- `MAX_FILE_MB` — Pre-download size limit
- `TG_MAX_UPLOAD_MB` — Upload size limit
- `YTDLP_IMPERSONATE` — Browser impersonation (important for TikTok)
- `YTDLP_FORCE_IPV4` — Force IPv4 networking

---

## 🍪 Cookies Handling

The bot uses a **single `cookies.txt` file**.

- Cookies for different platforms may coexist
- yt-dlp and gallery-dl automatically select cookies by domain
- The file is mounted read-only inside the container

### Updating Cookies

1. Log in to the target platform in your browser
2. Export cookies in **Netscape format**  
   (extensions: *Get cookies.txt*, *cookies.txt exporter*)
3. Append new cookies to `cookies.txt`
4. Restart the bot:

```bash
docker compose restart bot
```

---

## 🎵 Download Engines

### yt-dlp

- Universal video extractor (youtube-dl fork)
- ~1,800–2,000 extractors
- Best for:
  - YouTube, Twitch, Vimeo
  - TikTok, Instagram Reels
  - HLS / DASH streaming

> TikTok frequently breaks stable releases.  
> This project installs **yt-dlp from master** for faster fixes.

### gallery-dl

- Gallery and post extractor
- ~250–350 platforms
- Best for:
  - Instagram posts and carousels
  - Pinterest
  - Reddit galleries
  - Art and image-heavy platforms

Used strictly as a **fallback**, not a replacement for yt-dlp.

---

## 🧩 Troubleshooting

### “Cannot find command 'git'” during build
The Dockerfile uses a **multi-stage build**.  
`git` is required only in the builder stage to install yt-dlp from source.

### TikTok: “Unable to extract webpage video data”
Usually resolved by updating yt-dlp:

```bash
docker compose build --no-cache bot
docker compose up -d --force-recreate bot
```

### Large files fail to upload
- Ensure local `telegram-bot-api` is running
- Verify `TG_MAX_UPLOAD_MB` limits

---

## 🔒 Security Notes

- **Never commit**:
  - `.env`
  - `secrets/`
  - cookies
  - tokens
- For production, consider external secrets storage (CI secrets, Vault, etc.)

---

## 📄 License

This project is licensed under the **MIT License**.

Copyright (c) 2026 **Nexrithal from Veilnova**

See the [`LICENSE`](./LICENSE) file for the full license text.

### Legal Notice

Please read [`LEGAL_NOTICE.md`](./LEGAL_NOTICE.md) for important information  
regarding content responsibility and usage.

---

> Veilnova Fetch Node is a technical tool.  
> Responsibility for downloaded content always lies with the user.
