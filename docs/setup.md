# Setup

Run Music Bot alongside an existing Navidrome server with Docker Compose. The base installation
supports local imports, tagging, metadata lookup and library actions without a download provider.

## Requirements

- A Telegram bot token from [BotFather](https://t.me/BotFather) and your numeric Telegram user ID.
- A Navidrome account and write access to the music library on the Docker host.

Navidrome runs separately. [compose.yaml](../compose.yaml) starts Music Bot.

## Configure and start

1. Save [compose.yaml](../compose.yaml) and [.env.example](../.env.example) in one directory.
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

   Keep `DOWNLOAD_PROVIDER=none` for local library management.
   For Navidrome on the Docker host, use `http://host.docker.internal:4533`. For containers on a
   shared Docker network, use the Navidrome service name. `localhost` points to the bot container.
3. Create `bot-data` with the selected owner and group. Ensure the library is writable by that
   user or group. Follow [volume permissions](volume-permissions.md), including migration
   instructions for existing installs.
4. Start and check the bot:

   ```sh
   docker compose up -d
   docker compose ps
   docker compose logs --tail=50 music-bot
   ```

   Send `/start`, `/stats` and `/provider`. The provider status should say that none is configured;
   local imports and library actions remain available.

All base options and defaults are in [.env.example](../.env.example). Keep `.env` private.

## Local imports

Drop an album ZIP, a folder of tracks or an individual audio file into `bot-data/uploads/`. This folder is created
by the bot and can also receive files over Samba or SFTP. Results are reported in Telegram.
The current importer requires a matching Deezer release; see [usage](usage.md#import-an-album).

The optional browser page accepts ZIP archives and individual audio files, including MP3, M4A
and FLAC. It feeds the same importer and reports results in Telegram. It runs inside Music Bot
and needs no separate download provider or container.

For browser uploads, set `UPLOAD_HTTP_PORT` in `.env`, enable the commented port mapping under
`music-bot` in [compose.yaml](../compose.yaml), then recreate the service. The upload page has no
built-in authentication; keep access private or protect it with an authenticated reverse proxy.
Open the server's address and the configured port in your browser. The bot's upload size limits
apply; Telegram's cloud file-download limit does not apply to this page.

## Inline mode and share links

Enable inline mode with `/setinline` in BotFather. Type `@yourbot` in any chat or use the
shortcut buttons in `/start`.

For Navidrome share links, set `NAVIDROME_PUBLIC_URL` and enable sharing in Navidrome
(`ND_ENABLESHARING=true`). The Navidrome account also needs permission to share.

## Optional downloads

Follow the [Soulseek provider guide](providers/soulseek.md) for an optional local slskd daemon or
an existing instance. Download providers and library sharing are explicitly configured separately.
A link with no available provider still identifies the release and checks the library, then offers
import and setup instructions. It does not queue a future download.

## Without Docker

From a checkout, with Python 3.12+ and uv:

```sh
uv sync --frozen
cp .env.example .env
# Fill in .env, including the direct-run paths described below.
uv run --frozen python src/bot.py
```

Set `MUSIC_DIR` to your library, `UPLOAD_DIR` and `JOURNAL_PATH` to writable intake/state paths.
`MUSIC_LIBRARY_DIR` is the host mount setting used by Compose. The base Python installation
omits the Soulseek API package. See [provider setup](providers/soulseek.md#without-docker)
when you need it.

## Upgrading and rollback

Save the previous image tag, Compose files and private configuration before upgrading. Existing
slskd installations need the [optional provider configuration](providers/soulseek.md).
If switching to the base-only stack, explicitly stop the old slskd service; removing it from
a Compose file does not stop an existing container. Keep its configuration and download staging.
Preserve music and `bot-data`, including unfinished-download journals.

After upgrading, check the service health, `/start`, `/stats` and a local import. If those fail,
restore the previous image and Compose files, recreate the service, and repeat the checks.
