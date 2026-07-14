# discord-music-bot

[![CI](https://github.com/chaezuha/discord-music-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/chaezuha/discord-music-bot/actions/workflows/ci.yml)

A self-hostable Discord music bot that streams audio into voice channels using
**yt-dlp** + **ffmpeg**. Paste a URL from any yt-dlp-supported site, or search
YouTube/SoundCloud and pick from the top 10 results in a dropdown.

## Features

- `/play` with direct URLs — YouTube, SoundCloud, and Bandcamp out of the box; the `ALLOWED_URL_DOMAINS` setting can open it up to [anything yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
- Playlist and album URLs import their first tracks in one go (10 by default, `PLAYLIST_MAX_TRACKS` to change)
- Search by name and pick from a private dropdown of the top 10 matches (YouTube by default, SoundCloud via the `source` option)
- A now-playing card with thumbnail, progress bar, and Pause/Skip/Loop/Queue/Stop buttons
- Paginated `/queue` with Prev/Next buttons, total queued time, estimated start times, and a remove-a-track menu
- Queue management: `/move`, `/shuffle`, `/clear`, `/remove` (with live suggestions), `/remove-mine`, jump-the-queue (`/playnext`)
- Majority-vote skipping (`/skip`) with a no-vote escape hatch (`/forceskip`)
- `/loopsong` to repeat the current track, `/loopqueue` to cycle the whole queue
- Failed streams retry once on a fresh URL before skipping
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
| `/queue`                   | Browse the queue page by page, with totals, estimated start times, and a remove-a-track menu.             |
| `/move <track> <position>` | Move a queued track to a new position (autocompletes from the queue).                                     |
| `/shuffle`                 | Shuffle the queue.                                                                                        |
| `/clear`                   | Clear the queue — the current track keeps playing.                                                        |
| `/remove <number or name>` | Remove a queued track by its `/queue` number or closest-matching name (autocompletes from the queue).     |
| `/remove-mine`             | Remove every track you requested from the queue.                                                          |
| `/stop`                    | Stop playback, clear the queue, and disconnect.                                                           |
| `/help`                    | List all commands.                                                                                        |

Control commands (`/pause`, `/resume`, `/skip`, `/forceskip`, `/loopsong`,
`/loopqueue`, `/move`, `/shuffle`, `/clear`, `/remove`, `/remove-mine`,
`/stop`) and the now-playing buttons only work while you're in the bot's voice
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
own after crashes and reboots. It also enables a connection watchdog
(`WATCHDOG_DISCONNECT_SECONDS=300`): if the bot is cut off from Discord for
more than 5 minutes — say your router restarts and the reconnect never
completes — it exits and the restart policy brings it back with a fresh
session.

#### Logs

Besides `docker compose logs`, the bot writes rotating log files (about
10 MB of recent history, including whatever led up to a crash or
disconnect) to a `botlogs` volume:

```sh
docker compose exec bot tail -F logs/bot.log
```

Use `tail -F` (capital F) so following continues across log rotation. The
same directory holds `faulthandler.log`, a normally-empty file that only
receives a traceback if the process dies hard (e.g. a native-library crash).
If you'd rather have the files directly on the host, replace the `botlogs`
volume with a `./logs:/app/logs` bind mount — but create `./logs` yourself
with permissions the container's `bot` user can write to, or the bot falls
back to console-only logging.

To update, run `up` again. The compose file pulls the latest image on every
start, and the image is rebuilt weekly (plus on every release) so it tracks
new yt-dlp releases:

```sh
docker compose up -d
```

The compose file also runs the container with a read-only filesystem, no
capabilities, and memory/PID limits. If you want stronger isolation (e.g.
because you widen `ALLOWED_URL_DOMAINS`), consider firewalling the
container's egress to your LAN and cloud-metadata ranges — URL allowlisting
limits which sites yt-dlp is pointed at, but it is not a full SSRF sandbox.

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
- [Deno](https://docs.deno.com/runtime/getting_started/installation/) on your
  PATH — yt-dlp needs a JavaScript runtime for full YouTube support
  ([details](https://github.com/yt-dlp/yt-dlp/wiki/EJS))

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
| `PLAYLIST_MAX_TRACKS`  | no       | How many tracks `/play` imports from a playlist or album URL (default `10`, max `500`). |
| `OWNER_ID`             | no       | Your Discord user ID. If set, the bot DMs you when repeated failures suggest yt-dlp needs an update. |
| `LOG_DIR`              | no       | Directory for rotating log files (default `./logs`). Falls back to console-only logging if unwritable. |
| `WATCHDOG_DISCONNECT_SECONDS` | no | Exit after being disconnected from Discord this long, so a supervisor can restart the bot fresh. Default `0` (disabled); `compose.yaml` sets `300`. Only enable outside Docker if something (systemd, a shell loop) restarts the process for you. |
| `ALLOWED_URL_DOMAINS`  | no       | Which sites direct URLs may point at. Unset: YouTube, SoundCloud, and Bandcamp (subdomains included). A comma-separated domain list **replaces** those defaults. `*` disables the check and allows any yt-dlp-supported site — only do this on servers where you trust everyone, since URLs are fetched from inside your network. |

## Development

```sh
pip install -r requirements-dev.txt
pytest            # unit + async player tests (no network needed)
ruff check .      # lint
ruff format .     # format
```

CI runs lint, the test suite on Python 3.10/3.12/3.14, a dependency audit
(`pip-audit`), and a Docker build + container smoke test on every push and
PR. Pushes to `main` and `v*` tags publish the image to GHCR only after all
of those pass — the exact image that was smoke-tested is what gets pushed. A
weekly scheduled run republishes `latest` so it picks up new yt-dlp releases;
versioned tags are immutable.

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
