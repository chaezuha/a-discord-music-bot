# discord-music-bot

[![CI](https://github.com/chaezuha/discord-music-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/chaezuha/discord-music-bot/actions/workflows/ci.yml)

A self-hostable Discord music bot that streams audio into voice channels using
**yt-dlp** + **ffmpeg**. Paste a URL from any yt-dlp-supported site, or search
YouTube/SoundCloud and pick from the top 10 results in a dropdown.

## Features

- `/play` with direct URLs (YouTube, SoundCloud, Bandcamp, and [anything else yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md))
- Search by name and pick from a dropdown of the top 10 matches (YouTube by default, SoundCloud via the `source` option)
- Per-server queue with add, view, skip, jump-the-queue (`/playnext`), and fuzzy remove-by-name
- Majority-vote skipping (`/skip`) with a no-vote escape hatch (`/forceskip`)
- `/loopsong` to repeat the current track, `/loopqueue` to cycle the whole queue
- Pauses itself when everyone leaves the voice channel and resumes when someone comes back
- Auto-disconnects after 3 minutes of inactivity or an empty channel (configurable)
- Can DM the bot owner when repeated failures suggest yt-dlp needs an update (set `OWNER_ID`)
- Slash commands, no privileged intents required

## Commands

| Command                    | What it does                                                                                              |
| -------------------------- | --------------------------------------------------------------------------------------------------------- |
| `/play <query> [source]`   | Play a URL, or search and pick from the top 10 results. Queues the track if something is already playing. |
| `/playnext <query> [source]` | Like `/play`, but the track jumps to the front of the queue.                                            |
| `/pause`                   | Pause playback (stays connected).                                                                         |
| `/resume`                  | Resume paused playback.                                                                                   |
| `/skip`                    | Vote to skip the current track — passes at half the voice channel, rounded up (instant with 1–2 listeners). |
| `/forceskip`               | Skip the current track immediately, no vote.                                                              |
| `/loopsong`                | Repeat the current track until you run `/loopsong` again.                                                  |
| `/loopqueue`               | Loop the whole queue — finished tracks return to the end.                                                  |
| `/queue`                   | Show the current track (with elapsed time) and upcoming queue.                                            |
| `/remove <number or name>` | Remove a queued track by its `/queue` number or closest-matching name.                                    |
| `/stop`                    | Stop playback, clear the queue, and disconnect.                                                           |
| `/help`                    | List all commands.                                                                                        |

Control commands (`/pause`, `/resume`, `/skip`, `/forceskip`, `/loopsong`,
`/loopqueue`, `/remove`, `/stop`) only work while you're in the bot's voice
channel; `/queue` and `/help` are open to everyone.

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

You don't need to clone the repo. The prebuilt image includes ffmpeg and
everything else. Put [`compose.yaml`](compose.yaml) and a `.env` (see
[`.env.example`](.env.example)) in a folder, paste your bot token into `.env`,
then:

```sh
docker compose up -d          # pulls the prebuilt GHCR image
docker compose logs -f        # follow logs
```

The compose file sets `restart: unless-stopped`, so the bot comes back on its
own after crashes and reboots.

To update, run `up` again. The compose file pulls the latest image on every
start, which also picks up new yt-dlp releases:

```sh
docker compose up -d
```

### Alternative: plain Docker

Same image, without Compose (again with your token in `.env`):

```sh
docker run --env-file .env ghcr.io/chaezuha/discord-music-bot:latest
```

Or build it yourself from a clone:

```sh
docker build -t discord-music-bot .
docker run --env-file .env discord-music-bot
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
cd discord-music-bot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and paste your bot token
python bot.py
```

### Slash-command sync

Slash commands sync automatically on startup. A global sync can take up to an
hour to show up in Discord, so set `DEV_GUILD_ID` in `.env` to your server's
ID for instant sync while testing.

## Configuration (`.env`)

| Variable               | Required | Description                                                  |
| ---------------------- | -------- | ------------------------------------------------------------ |
| `DISCORD_TOKEN`        | yes      | Bot token from the Developer Portal.                         |
| `DEV_GUILD_ID`         | no       | Server ID for instant slash-command sync during development. |
| `IDLE_TIMEOUT_SECONDS` | no       | Seconds before auto-disconnect, for both idle playback and an empty voice channel (default `180`). |
| `OWNER_ID`             | no       | Your Discord user ID. If set, the bot DMs you when repeated failures suggest yt-dlp needs an update. |

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
- Keep `yt-dlp` up to date, since sites change and old versions stop working.
  On Docker that's `docker compose up -d`; on a Python install,
  `pip install -U yt-dlp`.

## Disclaimer

This project uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) to fetch audio
streams for **personal, non-commercial playback only**. It does not
download, cache, or redistribute media, and does not circumvent DRM.

Streaming from YouTube via third-party tools may conflict with YouTube's
Terms of Service. This is a self-hosted tool for private use in your own
Discord server(s); the maintainer does not operate a public instance and is
not affiliated with YouTube, SoundCloud, or Discord. You are responsible for
complying with the ToS of any site you point this at, and for the copyright
status of anything you play.

Provided as-is, for educational purposes, with no warranty.
