# Security

Found a vulnerability? Please report it privately through
[GitHub security advisories](https://github.com/eeegoloauq/music-bot/security/advisories/new)
instead of opening a public issue. You'll get a response within a few days, and a fix lands in
the next release — only the latest release is supported.

`ALLOWED_USERS` restricts Telegram actions. Keep `.env`, bot tokens, upload tokens and
Navidrome credentials private. The browser upload listener requires `UPLOAD_HTTP_TOKEN`;
its optional Compose port mapping binds to loopback. Use HTTPS for remote browser access.

Download providers are optional. The example Soulseek configuration keeps slskd's management
UI on loopback; see [provider setup](docs/providers/soulseek.md). Library sharing is a separate
opt-in. Dependencies are locked in `uv.lock`; vulnerability reports receive the same private
handling as other security issues.
