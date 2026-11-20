#!/usr/bin/env python3
"""Container entrypoint: refresh the guide once a day and serve the result over HTTP.

Env vars (see also openepg_fix.py):
  SOURCE_URL   guide XML to download (required)
  RUN_AT       daily refresh time, local (TZ), HH:MM            default 04:00
  TZ           timezone name                                     default America/Toronto
  PORT         HTTP port                                         default 8080
  DATA_DIR     persistent dir (history + served file)            default /data
  RETRY_MINUTES  wait before retrying a failed refresh           default 60

The processed guide is served at  http://<host>:PORT/guide.xml
"""
import os, sys, threading, time, traceback
from datetime import datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
WWW_DIR = DATA_DIR / "www"
os.environ.setdefault("OUTPUT_PATH", str(WWW_DIR / "guide.xml"))
os.environ.setdefault("HISTORY_PATH", str(DATA_DIR / "history.json"))

import openepg_fix  # noqa: E402  (must come after the env defaults above)

RUN_AT = os.environ.get("RUN_AT", "04:00")
PORT = int(os.environ.get("PORT", 8080))
RETRY_MIN = int(os.environ.get("RETRY_MINUTES", 60))


class Handler(SimpleHTTPRequestHandler):
    def list_directory(self, path):          # no directory listings
        self.send_error(404, "Not found")
        return None


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Clients that hang up mid-download (health checks, Jellyfin timeouts)
        # are normal; don't print a traceback for them.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve():
    WWW_DIR.mkdir(parents=True, exist_ok=True)
    srv = Server(("0.0.0.0", PORT), partial(Handler, directory=str(WWW_DIR)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Serving {WWW_DIR} on :{PORT}", flush=True)


def refresh():
    """True if the guide is current. False if the download failed (a stale local copy
    was used, or nothing was updated) so the caller should retry later."""
    print(f"[{datetime.now(openepg_fix.LOCAL_TZ):%Y-%m-%d %H:%M}] refreshing guide...", flush=True)
    try:
        return openepg_fix.run_once()
    except Exception:
        traceback.print_exc()
        return False


def next_run():
    hh, mm = (int(x) for x in RUN_AT.split(":"))
    now = datetime.now(openepg_fix.LOCAL_TZ)
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def main():
    if not os.environ.get("SOURCE_URL"):
        sys.exit("SOURCE_URL is not set")
    serve()
    ok = refresh()                            # fetch immediately on start-up
    while True:
        if ok:
            target = next_run()
        else:
            target = datetime.now(openepg_fix.LOCAL_TZ) + timedelta(minutes=RETRY_MIN)
            print(f"Refresh failed; retrying at {target:%H:%M}", flush=True)
        while (wait := (target - datetime.now(openepg_fix.LOCAL_TZ)).total_seconds()) > 0:
            time.sleep(min(wait, 60))
        ok = refresh()


if __name__ == "__main__":
    main()
