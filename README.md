# openepg-fix

Fixes up poor-quality XMLTV guide data (such as Open-EPG) so Jellyfin can record
series, then serves the result over HTTP.

Once a day the container downloads your source guide and rewrites it:

- **Episode info hidden in `<desc>`** (`S08 E14 — Episode TitleDescription…`) is
  parsed into a real `<episode-num>` and `<sub-title>`, and removed from the description.
- **Shows with no episode info** (news, talk shows) that air on 2+ different days
  get a unique date-based episode number per airing, so Jellyfin treats each
  airing as a new episode of one series.
- **Reruns** of an episode already seen get `<previously-shown/>`, so
  "record new episodes only" can skip them.

The processed guide is served at `http://<host>:8080/guide.xml`.

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Image definition (`python:3.14-slim`, runs as a non-root user) |
| `docker-compose.yml` | Optional: build and run in one step |
| `server.py` | Container entrypoint: daily refresh loop + web server |
| `openepg_fix.py` | The guide-processing script (also usable standalone) |

## Build

No registry is needed. Build the image on the machine that will run it:

```sh
cd openepg-fix
docker build -t openepg-fix .
```

Or build on another machine and copy the image across:

```sh
docker save openepg-fix | ssh user@server docker load
```

## Run

With plain Docker (replace the `SOURCE_URL`):

```sh
docker run -d --name openepg-fix --restart unless-stopped \
  -p 80:80 \
  -e SOURCE_URL="https://example.com/guide.xml" \
  -e TZ=America/Toronto \
  -e RUN_AT=04:00 \
  -v openepg_fix:/data \
  openepg-fix
```

Or with Docker Compose: edit `SOURCE_URL` in `docker-compose.yml`, then

```sh
docker compose up -d --build
```

Check that it works:

```sh
docker logs openepg-fix                 # should show the first refresh
curl http://localhost:8080/guide.xml    # should return the processed XML
```

## Point Jellyfin at it

Dashboard → Live TV → TV Guide Data Providers → XMLTV, and set the file or URL to:

- `http://openepg-fix:8080/guide.xml` if Jellyfin is on the same Docker network, or
- `http://<docker-host-ip>:8080/guide.xml` otherwise.

Jellyfin only picks up changes on its own "Refresh Guide" scheduled task. Run
that task manually after the first setup.

## Behaviour

- **On start:** processes the guide immediately, then again every day at `RUN_AT`.
- **Local copy:** every download is saved to `/data/raw/latest.xml`, with dated
  copies kept for `RAW_KEEP_DAYS` days. If a local copy is younger than
  `MIN_FETCH_HOURS`, the container reuses it instead of downloading, so
  restarts and rebuilds don't count against a rate limit.
- **Bad downloads:** a download is only saved if it is valid XML, so an error
  page can't replace a good copy.
- **Failures:** if a download fails, the older local copy is used and the
  refresh is retried after `RETRY_MINUTES`. The last good `guide.xml` keeps
  being served throughout.
- **Web server:** serves only `guide.xml`. No directory listing, and the
  history and raw files are not exposed.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SOURCE_URL` | *(required)* | URL of the guide XML to download |
| `TZ` | `America/Toronto` | Timezone for `RUN_AT` and for the dates used in episode numbers |
| `RUN_AT` | `04:00` | Daily refresh time (HH:MM, in `TZ`) |
| `RETRY_MINUTES` | `60` | Wait before retrying a failed download |
| `MIN_FETCH_HOURS` | `20` | Reuse the local copy if it is younger than this |
| `RAW_KEEP_DAYS` | `14` | How long dated copies of the source are kept |
| `MIN_DISTINCT_DATES` | `2` | Days a no-episode-info show must appear on to count as a series |
| `HISTORY_DAYS` | `60` | How long the show history is kept |
| `MOVIE_MIN_MINUTES` | `100` | No-episode-info programmes longer than this are treated as movies |
| `FORCE_SERIES` | `^CTV National News;;^CTV News;;^Global News` | Title regexes always treated as series |
| `NEVER_SERIES` | `Grey Cup;;^Movie:;;^Sign[- ]Off` | Title regexes never treated as series |

Separate multiple `FORCE_SERIES` / `NEVER_SERIES` patterns with `;;`.

Do not set `OUTPUT_PATH`, `HISTORY_PATH`, `RAW_DIR`, `DATA_DIR` or `PORT` in
Docker. The health check and web server expect the default layout. To use a
different host port, change the port mapping instead (for example `-p 9090:8080`).

## Data volume layout

```
/data/
├── history.json        show/episode history (used for series and rerun detection)
├── raw/
│   ├── latest.xml      most recent source download
│   └── guide-*.xml     dated copies
└── www/
    └── guide.xml       the processed guide that is served
```

## Maintenance

**Update after changing the code:**

```sh
docker build -t openepg-fix .
docker rm -f openepg-fix
# ...then repeat the docker run command above
```

The `guide-data` volume keeps your history and local copies across rebuilds.

**Force a download now** (ignores the local-copy age check; use sparingly if
your source has a rate limit):

```sh
docker exec openepg-fix python openepg_fix.py --force-fetch
```

**Seed the history from old guide files.** This helps catch weekly shows sooner:

```sh
docker cp old1.xml openepg-fix:/tmp/old1.xml
docker exec openepg-fix python openepg_fix.py --seed /tmp/old1.xml
```

**View logs:** `docker logs -f openepg-fix`

## Troubleshooting

- **Container shows "unhealthy":** `guide.xml` doesn't exist yet. Check
  `docker logs openepg-fix` for a failed first download (wrong `SOURCE_URL`,
  or the source is rate limiting you).
- **A weekly show isn't recognised as a series:** it has only been seen on one
  day. Add it to `FORCE_SERIES`, or seed the history with older guide files.
- **A show wrongly treated as a series:** add it to `NEVER_SERIES`.
- **Odd episode subtitles:** the episode title is separated from the description
  by finding where a lowercase letter runs straight into a capital. Titles
  with internal capitals (like "McDonald") can be cut short. This only affects
  the subtitle text, not the episode numbers.
- **Bind mount instead of a named volume:** the container runs as UID 1000, so
  the host folder must be writable by that user.

## Running without Docker

The script works standalone (Python 3.9+, plus `tzdata` on systems without
system timezone data):

```sh
SOURCE_URL="https://example.com/guide.xml" python3 openepg_fix.py \
  -o ./guide.xml --history ./history.json
```

Use `-i file.xml` to process a local file instead of downloading.
