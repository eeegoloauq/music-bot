# CLAUDE.md

This file provides high-level guidance for AI coding assistants working with this repository.

## Read First

Before making non-trivial changes, read:

1. `README.md`
2. `src/` entry points and related modules
3. Local documentation if available in your working environment

## Repository Focus

- Telegram bot for managing a Navidrome music library
- Downloads and organizes music from Tidal via Monochrome-compatible backends
- Main code paths live in `src/bot.py`, `src/inline.py`, `src/navidrome.py`, and `src/tidal/`

## Engineering Notes

- Prefer small, reviewable changes.
- Keep user-facing behavior stable unless the change intentionally alters it.
- Treat configuration, deployment setup, and credentials as environment-specific and avoid hardcoding them.
- Do not add tool-specific attribution or co-author trailers to commits unless explicitly requested by the repository owner.
