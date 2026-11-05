# Setup

Run Music Bot alongside an existing Navidrome server with Docker Compose.

## Requirements

- A Telegram bot token from [BotFather](https://t.me/BotFather) and your numeric Telegram user ID.
- A Navidrome account and write access to the music library on the Docker host.
- Soulseek credentials for [slskd](https://github.com/slskd/slskd), which the current stack requires
  even when using local imports. Making download providers optional is on the [roadmap](../ROADMAP.md).

Navidrome runs separately. [compose.yaml](../compose.yaml) starts Music Bot and slskd.

## Configure and start

1. Save [compose.yaml](../compose.yaml) and [.env.example](../.env.example) in an empty directory.
   Rename `.env.example` to `.env`.
2. Fill in these values in `.env`:

   | Setting | Value |
   |---|---|
   | `TG_TOKEN` | Token from BotFather |
   | `ALLOWED_USERS` | Comma-separated Telegram user IDs allowed to use the bot |
   | `NAVIDROME_URL` | Navidrome address reachable from the bot container |
   | `NAVIDROME_USER`, `NAVIDROME_PASS` | Navidrome credentials |
   | `MUSIC_LIBRARY_DIR` | Absolute host path to the music library |
   | `MUSICBOT_UID`, `MUSICBOT_GID` | Bot state owner and music library group, as numeric IDs |
   | `SOULSEEK_USERNAME`, `SOULSEEK_PASSWORD` | Soulseek credentials |

   For Navidrome on the Docker host, use `http://host.docker.internal:4533`. For containers on a
   shared Docker network, use the Navidrome service name. `localhost` points to the bot container.
3. Create `bot-data`, `slskd-config` and the library's `.slskd-downloads` directory with the selected
   owner and group. Ensure the library is writable by that user or group. Follow
   [volume permissions](volume-permissions.md), including migration instructions for existing installs.
4. Start and check the stack:

   ```sh
   docker compose up -d
   docker compose ps
   docker compose logs --tail=50 music-bot
   ```

   slskd must log in before Music Bot starts. Send `/start` to the bot when both services are running.

All options and defaults are in [.env.example](../.env.example). Keep `.env` private.

## Local imports

Drop an album ZIP or a folder of tracks into `bot-data/uploads/`. This folder is created
by the bot and can also receive files over Samba or SFTP. Results are reported in Telegram.

For browser uploads, set `UPLOAD_HTTP_PORT` in `.env`, enable the commented port mapping under
`music-bot` in [compose.yaml](../compose.yaml), then recreate the service. The upload page has no
built-in authentication; keep access private or protect it with an authenticated reverse proxy.

## Inline mode and share links

Enable inline mode with `/setinline` in BotFather. Type `@yourbot` in any chat or use the
shortcut buttons in `/start`.

For Navidrome share links, set `NAVIDROME_PUBLIC_URL` and enable sharing in Navidrome
(`ND_ENABLESHARING=true`). The Navidrome account also needs permission to share.

## slskd connections

The Compose file exposes slskd's unauthenticated web UI on host loopback at `127.0.0.1:5030`.
Keep that binding private. For incoming peer connections, forward the port selected by
`SLSKD_LISTEN_PORT` on your router; its default is `50300`.

Use download and sharing sources only where you have permission to obtain and share the material.

## Without Docker

From a checkout, with Python 3.12+, uv and a running slskd instance:

```sh
uv sync --frozen
cp .env.example .env
# Fill in .env, including the direct-run paths described below.
uv run python src/bot.py
```

Set `MUSIC_DIR` to your library, `UPLOAD_DIR` and `JOURNAL_PATH` to writable intake/state paths,
and `SLSKD_HOST` and `SLSKD_DOWNLOAD_DIR` to your running slskd instance and its download directory.
`MUSIC_LIBRARY_DIR` is the host mount setting used by Compose. See
[src/config.py](../src/config.py) for direct-run settings.
