# Music Bot

A Telegram bot for managing your [Navidrome](https://www.navidrome.org/) music library.

[![Release](https://img.shields.io/github/v/release/eeegoloauq/music-bot?label=release)](https://github.com/eeegoloauq/music-bot/releases/latest)
[![Tests](https://github.com/eeegoloauq/music-bot/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/eeegoloauq/music-bot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

- Send the track you're playing as an audio file to any Telegram chat.
- Share Navidrome links to tracks and albums, or send the lyrics of the playing track.
- Import ZIP archives and individual audio files through an optional upload page or watched folder.
- Delete albums or individual tracks remotely.
- Update library tags from [Deezer](https://www.deezer.com/) and add genres from
  [Last.fm](https://www.last.fm/api) with your API key.
- Fetch synced lyrics from [LRCLIB](https://lrclib.net/) when importing music.

## Setup

1. Save [compose.yaml](compose.yaml) and [.env.example](.env.example) in one directory. Rename
   `.env.example` to `.env`.
2. Follow the [setup guide](docs/setup.md) to fill in credentials, connect your library and prepare
   writable directories.
3. Run `docker compose up -d`, then send `/start` to your bot.

## Usage

Enable inline mode in [BotFather](https://t.me/BotFather), then type `@yourbot` in any chat:

| Query | Action |
|---|---|
| `np` | Send the playing track as an audio file |
| `s` | Share a Navidrome link to the playing track |
| `l` | Send the lyrics of the playing track |
| `lib name` | Find a track or album in your library to share a link |
| `del name` | Delete a track or album, with confirmation |

Use `/retag` to update library tags and genres, `/scan` to rescan Navidrome, and `/stats` for
library counts and size. `/retag` lists the changes first; `/retag confirm` applies them.
Local imports and library management work without a download provider.

To add your music, upload an album ZIP or an MP3/M4A/FLAC file through the optional
[upload page](docs/setup.md#local-imports), or drop files into `bot-data/uploads/` on the server.
Import results appear in Telegram.

Music links identify a release and check your library. If tracks are missing, import your files
or explicitly configure an [optional download provider](docs/providers/soulseek.md).
Use `/provider` to check its status.

[Importing music and other actions](docs/usage.md) · [Configuration](.env.example) · [Roadmap](ROADMAP.md)

## Development

Python 3.12+, [uv](https://docs.astral.sh/uv/), `uv sync --frozen --extra soulseek`, then `uv run --no-sync pytest`.
Tests use stubbed services and run offline. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
