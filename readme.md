# a-discord-music-bot

[![CI](https://github.com/chaezuha/a-discord-music-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/chaezuha/a-discord-music-bot/actions/workflows/ci.yml)

A self-hostable Discord music bot that streams audio into voice channels using
**yt-dlp** + **ffmpeg**. Paste a URL from any yt-dlp-supported site, or search
YouTube/SoundCloud and pick from the top 10 results in a dropdown.

## Features

- `/play` with direct URLs (YouTube, SoundCloud, Bandcamp, and [anything else yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md))
- Search by name — shows a dropdown of the top 10 matches (YouTube by default, SoundCloud via the `source` option)
- Per-server queue with add, view, skip, and fuzzy remove-by-name
- Auto-disconnects after 3 minutes of inactivity (configurable)
- Slash commands, no privileged intents required

## Commands

| Command                    | What it does                                                                                              |
| -------------------------- | --------------------------------------------------------------------------------------------------------- |
| `/play <query> [source]`   | Play a URL, or search and pick from the top 10 results. Queues the track if something is already playing. |
| `/pause`                   | Pause playback (stays connected).                                                                         |
| `/resume`                  | Resume paused playback.                                                                                   |
| `/skip`                    | Skip the current track. If the queue is empty, disconnects.                                               |
| `/queue`                   | Show the current track and upcoming queue.                                                                |
| `/remove <number or name>` | Remove a queued track by its `/queue` number or closest-matching name.                                    |
| `/stop`                    | Stop everything: clears the queue and disconnects.                                                        |

## Setup

### 1. Create the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a **New Application**.
2. Under **Bot**, click **Reset Token** and copy the token (you'll need it for `.env`). No privileged intents are needed.
3. Invite the bot to your server with this URL (replace `YOUR_CLIENT_ID` with the Application ID from **General Information**):

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=3165184
   ```

   (That permission set is: View Channels, Send Messages, Embed Links, Connect, Speak.)

### 2. Run with Docker Compose (recommended)

No clone needed — the prebuilt image ships with ffmpeg and everything else
included. Put [`compose.yaml`](compose.yaml) and a `.env` (see
[`.env.example`](.env.example)) in a folder, paste your bot token into `.env`,
then:

```sh
docker compose up -d          # pulls the prebuilt GHCR image
docker compose logs -f        # follow logs
```

The compose file sets `restart: unless-stopped`, so the bot comes back on its
own after crashes and reboots.

To update (this also refreshes yt-dlp, which goes stale as sites change),
just run `up` again — the compose file pulls the latest image on every start:

```sh
docker compose up -d
```

### Alternative: plain Docker

Same image, without Compose (again with your token in `.env`):

```sh
docker run --env-file .env ghcr.io/chaezuha/a-discord-music-bot:latest
```

Or build it yourself from a clone:

```sh
docker build -t a-discord-music-bot .
docker run --env-file .env a-discord-music-bot
```

### Alternative: run directly with Python

You'll need:

- Python 3.10+
- ffmpeg on your PATH:
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `sudo apt install ffmpeg`
  - Windows: `winget install ffmpeg` (or [download](https://ffmpeg.org/download.html))

Then install, configure, and run:

```sh
git clone <this repo>
cd a-discord-music-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and paste your bot token
python bot.py
```

### Slash-command sync

Whichever way you run it, slash commands sync automatically on startup. Global
sync can take up to an hour to show up in Discord — set `DEV_GUILD_ID` in
`.env` to your server's ID for instant sync while testing.

## Configuration (`.env`)

| Variable               | Required | Description                                                  |
| ---------------------- | -------- | ------------------------------------------------------------ |
| `DISCORD_TOKEN`        | yes      | Bot token from the Developer Portal.                         |
| `DEV_GUILD_ID`         | no       | Server ID for instant slash-command sync during development. |
| `IDLE_TIMEOUT_SECONDS` | no       | Idle seconds before auto-disconnect (default `180`).         |

## Development

```sh
pip install -r requirements-dev.txt
pytest            # unit + async player tests (no network needed)
ruff check .      # lint
ruff format .     # format
```

CI runs lint, the test suite on Python 3.10/3.12/3.14, and a Docker build
check on every push and PR. Pushes to `main` and `v*` tags publish the image
to GHCR.

## Notes

- The bot resolves stream URLs right before playback, so long-queued tracks
  don't hit expired links.
- Only the person who ran a search can pick from its dropdown; pickers time
  out after 60 seconds.
- Keep `yt-dlp` up to date — sites change and old versions stop working.
  On Docker that's `docker compose up -d`; on a Python install,
  `pip install -U yt-dlp`.

## Disclaimer

This project uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to fetch audio
streams for **personal, non-commercial playback only** — it does not
download, cache, or redistribute media, and does not circumvent DRM.

Streaming from YouTube via third-party tools may conflict with YouTube's
Terms of Service. This is a self-hosted tool for private use in your own
Discord server(s); the maintainer does not operate a public instance and is
not affiliated with YouTube, SoundCloud, or Discord. You are responsible for
complying with the ToS of any site you point this at, and for the copyright
status of anything you play.

Provided as-is, for educational purposes, with no warranty.
