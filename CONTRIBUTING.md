# Contributing to Music Bot

First off — thank you for considering a contribution! Whether it's a bug
report, a smarter source-selection idea, or a typo fix, it all helps.

## Reporting bugs

Open a [bug report](https://github.com/eeegoloauq/music-bot/issues/new/choose).
The most useful thing you can include is the link you pasted and the bot's
final status message — that's usually enough to reproduce a download problem.

If the bot picked the wrong release or the tags came out wrong, use the
dedicated **Wrong match / bad tags** form instead — those aren't crashes, and
they need different details.

> **Before posting logs:** strip your bot token, your Telegram user IDs, and
> your Navidrome URL. Logs from `docker compose logs music-bot` may contain
> all three.

Security issues don't belong in the issue tracker — see [SECURITY.md](SECURITY.md).

## Suggesting features

Open a feature request issue **before** writing code. The bot's scope is
deliberately narrow — get music from a link into Navidrome, tagged well — so
let's talk it over first; it may save you an evening of work on something
that doesn't fit.

## Development setup

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/eeegoloauq/music-bot && cd music-bot
uv sync
uv run pytest
```

The test suite runs **fully offline** — slskd and every network call are
stubbed — so it needs no credentials, no containers, and finishes in seconds.
If it's green locally, CI will agree.

To run the bot for real you need the two-container setup from the
[README](README.md#setup): slskd + the bot, configured through `.env`.
That's only necessary for testing end-to-end download behavior; most logic
changes can be developed against the test suite alone.

## Pull requests

- Keep changes small and focused — one topic per PR.
- Behavior changes need a test. The suite is offline by design, so stub any
  new network interaction the way the existing tests do (`tests/`).
- `uv run pytest` must pass.
- For anything bigger than a fix, link the issue where we discussed it.

## What to expect

This project has a single maintainer working on it in spare time. Issues and
PRs get read, but a response can take a few days — that's normal, not a brush-off.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
