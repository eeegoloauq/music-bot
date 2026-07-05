# Security

Found a vulnerability? Please report it privately through
[GitHub security advisories](https://github.com/eeegoloauq/music-bot/security/advisories/new)
instead of opening a public issue. You'll get a response within a few days, and a fix lands in
the next release — only the latest release is supported.

Worth knowing about the deployment model: the bot is meant to run privately. `ALLOWED_USERS`
gates every Telegram interaction, secrets live in `.env` (never in the image or the repo), and
slskd's web UI runs without auth — it must stay bound to loopback, as in the compose file from
the README. Dependencies are pinned via `uv.lock` and scanned daily with osv-scanner.
