# Music Bot

A Telegram bot for managing your [Navidrome](https://www.navidrome.org/) music library.

[![Release](https://img.shields.io/github/v/release/eeegoloauq/music-bot?label=release)](https://github.com/eeegoloauq/music-bot/releases/latest)
[![Tests](https://github.com/eeegoloauq/music-bot/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/eeegoloauq/music-bot/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

- Send the track you're playing as an audio file to any Telegram chat.
- Share Navidrome links to tracks and albums, or send the lyrics of the playing track.
- Import ZIP archives and audio files through the [upload page](docs/setup.md#local-imports)
  or a server folder. Keep originals or identify and tag a release.
- Download albums and tracks through optional [providers](docs/providers).
- Delete albums or individual tracks remotely.
- Preview album or library tag updates from [Deezer](https://www.deezer.com/) and add genres from
  [Last.fm](https://www.last.fm/api) with your API key.
- Fill missing artwork from Deezer and synced lyrics from [LRCLIB](https://lrclib.net/),
  with optional replacement.

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

Use `/maintain` to choose fields to update, `/retag` to refresh library tags, `/scan` to rescan
Navidrome, and `/stats` for library counts and size. `/retag` lists the changes first;
`/retag confirm` applies them.
Local imports and library management work without a download provider.

Use `/upload` or **Upload page** in `/start` to add an album ZIP or audio file through the
configured [upload page](docs/setup.md#local-imports). You can also copy files into
`bot-data/uploads/` on the server. Choose how to import them in Telegram.

Music links identify a release and check your library. If tracks are missing, import your files
or configure an [optional download provider](docs/providers).
Use `/provider` to check its status.

[Importing music and other actions](docs/usage.md) · [Configuration](.env.example) · [Roadmap](ROADMAP.md)

## Development

Python 3.12+, [uv](https://docs.astral.sh/uv/), `uv sync --frozen --extra soulseek`, then `uv run --no-sync pytest`.
Tests use stubbed services and run offline. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
