# Soulseek via slskd

Connect [slskd](https://github.com/slskd/slskd) to download music through Soulseek.
Local imports and library management work without it.

## Docker Compose

1. Save [compose.soulseek.yaml](../../compose.soulseek.yaml) next to the base
   [compose.yaml](../../compose.yaml). This is the complete example with both services.
2. Add `SOULSEEK_USERNAME` and `SOULSEEK_PASSWORD` to your `.env`.
   See [.env.soulseek.example](../../.env.soulseek.example) for optional settings.
3. Create `slskd-config` and your library's `.slskd-downloads` directory with the same
   UID/GID as the bot. See [volume permissions](../volume-permissions.md).
4. Start:

   ```sh
   docker compose -f compose.yaml -f compose.soulseek.yaml up -d
   ```

Send `/provider` to check the connection, then send a music link.
Library sharing is configured separately in slskd; the example does not share your library.

## Existing slskd instance

Use this override as `compose.provider.yaml`, with the address, API key and completed-download
folder of your existing daemon:

```yaml
services:
  music-bot:
    image: ghcr.io/eeegoloauq/music-bot:latest-soulseek
    environment:
      DOWNLOAD_PROVIDER: soulseek
      SLSKD_HOST: http://your-slskd:5030
      SLSKD_API_KEY: ${SLSKD_API_KEY}
      SLSKD_DOWNLOAD_DIR: /downloads
    volumes:
      - /path/to/slskd/downloads:/downloads
```

```sh
docker compose -f compose.yaml -f compose.provider.yaml up -d
```

Set `SLSKD_API_KEY` in `.env` if your daemon requires it. The mounted folder must contain the
same completed files the daemon writes; `/downloads` is their path inside the bot container.

## Without Docker

Run `uv sync --frozen --extra soulseek`, then `uv run --frozen --extra soulseek python src/bot.py`.
Set `DOWNLOAD_PROVIDER=soulseek`, `SLSKD_HOST` and `SLSKD_DOWNLOAD_DIR` in `.env`, plus
`SLSKD_API_KEY` if required. See [direct-run paths](../setup.md#without-docker).
