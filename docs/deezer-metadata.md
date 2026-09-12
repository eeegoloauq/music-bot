# Deezer album completeness

Album downloads fetch the ordered track list from `/album/{id}/tracks`, paging
with `index` and `limit` while `next` is present. The embedded album list can
stop at 25 without a `next` link. Before enrichment or download, the full list
must match `nb_tracks`; duplicate tracks, stalled pages and partial responses
raise a metadata error. Cover lookups only fetch the album summary.

Offline regression checks:

```sh
uv run --offline --frozen pytest -q
```

Live metadata check (no audio download or Telegram messages):

```sh
env PYTHONPATH=src uv run --offline --frozen python - <<'PY'
import asyncio
from metadata import api, client

async def main():
    try:
        album = await api.fetch_album("54268992")
        assert len(album["tracks"]) == album["numberOfTracks"] == 43
        print(album["title"], len(album["tracks"]), album["tracks"][-1]["title"])
    finally:
        await client.close()

asyncio.run(main())
PY
```
