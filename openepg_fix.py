#!/usr/bin/env python3
"""Fix up Open-EPG style XMLTV data so Jellyfin sees series + episodes.

What it does, per <programme>:

 1. Real episode info hiding in <desc>.  Most listings start their description
    with "S08 E14 — Episode TitleDescription text" (note: no space between the
    episode title and the description).  We parse that, fill in <episode-num>
    and <sub-title>, and strip the prefix from <desc>.

 2. Shows with no episode info at all (news, talk shows, ...).  If the show is
    recurring (seen on >= MIN_DISTINCT_DATES different days, per history file,
    or matches FORCE_SERIES) it gets a synthetic, unique, date-based episode
    number so every airing looks like a new episode of one series.

 3. Reruns.  Airings of an episode (title+S+E) that we have already seen at an
    earlier time get <previously-shown/>, so "record new episodes only" works.

Usage:
  openepg_fix.py                          # fetch SOURCE_URL, write OUTPUT_PATH
  openepg_fix.py -i in.xml -o out.xml     # local file in/out (testing)
  openepg_fix.py --seed old1.xml old2.xml # only add old guide files to history
"""
import argparse, hashlib, json, os, re, sys, tempfile, time, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------- config (override with environment variables) ----------------
def _list(name, default):
    v = os.environ.get(name)
    return [x.strip() for x in v.split(";;") if x.strip()] if v is not None else default

SOURCE_URL   = os.environ.get("SOURCE_URL", "https://example.com/guide.xml")
OUTPUT_PATH  = Path(os.environ.get("OUTPUT_PATH", "/data/www/guide.xml"))
HISTORY_PATH = Path(os.environ.get("HISTORY_PATH", "/data/history.json"))
LOCAL_TZ     = ZoneInfo(os.environ.get("TZ", "America/Toronto"))

MIN_DISTINCT_DATES = int(os.environ.get("MIN_DISTINCT_DATES", 2))  # no-episode-info shows
HISTORY_DAYS       = int(os.environ.get("HISTORY_DAYS", 60))
MOVIE_MIN_MINUTES  = int(os.environ.get("MOVIE_MIN_MINUTES", 100))

# Local copy of the downloaded source (protects against rate limits / outages).
# RAW_DIR defaults to a "raw" folder next to the history file.
MIN_FETCH_HOURS = float(os.environ.get("MIN_FETCH_HOURS", 20))  # younger local copy => don't re-download
RAW_KEEP_DAYS   = int(os.environ.get("RAW_KEEP_DAYS", 14))      # dated archive copies kept this long

# Regexes matched against <title>.  In env vars, separate patterns with ";;".
# FORCE for weekly/occasional shows history can't catch yet; NEVER to exclude.
FORCE_SERIES = _list("FORCE_SERIES", [r"^CTV National News", r"^CTV News", r"^Global News"])
NEVER_SERIES = _list("NEVER_SERIES", [r"Grey Cup", r"^Movie:", r"^Sign[- ]Off"])
# Shows with no episode numbers whose <desc> is specific to the episode, so the same
# description on a later airing means a rerun.  Those reruns reuse the first airing's
# episode number and get <previously-shown/>.  Don't list daily shows whose
# description never changes (news): every airing would then count as a rerun.
SAME_EPISODE_BY_DESC = _list("SAME_EPISODE_BY_DESC", [r"^\(MIS\)Treated"])
# ---------------------------------------------------------------------------------

# "S08 E14 — Title" / "S08 E14Description" / "S08 E14 - Title"
SE_RE = re.compile(r"^\s*S(\d+)\s*E(\d+)\s*(?:(—|–|-)\s*)?(.*)$", re.S)
# Episode title runs straight into the description: "...BagA W5 investigation".
# Boundary = lowercase/digit/punctuation immediately followed by a capital, no space.
# A quote only counts when it CLOSES something, i.e. it follows a word/punctuation
# character (so ` "Grill` is skipped but `Go Home"The` splits).  The source also
# sometimes has stray backslashes before quotes (\"), which are allowed here
# and removed from the text afterwards.
_END = r"[a-z0-9.!?)]"
_QUOTE = r"[\"'’”]"
BOUNDARY_RE = re.compile(
    rf"(?:(?<={_END})|(?<={_END}{_QUOTE})|(?<={_END}\\{_QUOTE}))(?=[A-Z])"
)
MAX_SUBTITLE = 120


def parse_time(s):
    s = s.strip()
    dt = datetime.strptime(s, "%Y%m%d%H%M%S %z" if " " in s else "%Y%m%d%H%M%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def norm(title):
    return re.sub(r"\s+", " ", title or "").strip().lower()


def atomic_write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


# ---------- history ----------
def load_history():
    try:
        h = json.loads(HISTORY_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        h = {}
    h.setdefault("dates", {})      # "channel|title" -> [YYYY-MM-DD, ...]
    h.setdefault("episodes", {})   # "channel|title|S|E" -> earliest start (ISO, UTC)
    h.setdefault("descs", {})      # "channel|title|desc-hash" -> earliest start (ISO, UTC)
    return h


def save_history(h):
    cutoff = datetime.now(LOCAL_TZ) - timedelta(days=HISTORY_DAYS)
    h["dates"] = {k: sorted(d for d in set(v) if d >= cutoff.date().isoformat())
                  for k, v in h["dates"].items()}
    h["dates"] = {k: v for k, v in h["dates"].items() if v}
    cut_iso = cutoff.astimezone(timezone.utc).isoformat()
    h["episodes"] = {k: v for k, v in h["episodes"].items() if v >= cut_iso}
    h["descs"] = {k: v for k, v in h["descs"].items() if v >= cut_iso}
    atomic_write(HISTORY_PATH, json.dumps(h).encode())


def show_key(p):
    return f"{p.get('channel')}|{norm(p.findtext('title'))}"


def parse_desc_episode(p):
    """Return (season, episode, ep_title, clean_desc) or None."""
    desc = (p.findtext("desc") or "").replace('\\"', '"').replace("\\'", "'")
    m = SE_RE.match(desc)
    if not m:
        return None
    season, episode, dash, rest = int(m[1]), int(m[2]), m[3], m[4]
    ep_title, clean = None, rest
    if dash:
        b = BOUNDARY_RE.search(rest, 1)
        if b and b.start() <= MAX_SUBTITLE:
            ep_title, clean = rest[:b.start()].strip(), rest[b.start():].strip()
        # else: couldn't split safely -> leave desc alone, no subtitle
        else:
            clean = desc
    return season, episode, ep_title, clean


def matches(patterns, title):
    return any(re.search(rx, title or "", re.I) for rx in patterns)


def desc_key(p):
    """History key for 'same description = same episode' shows, else None."""
    title, desc = p.findtext("title") or "", (p.findtext("desc") or "").strip()
    if not desc or norm(desc) == norm(title) or not matches(SAME_EPISODE_BY_DESC, title):
        return None
    return f"{show_key(p)}|{hashlib.md5(desc.encode()).hexdigest()[:12]}"


def split_embedded_title(title, desc):
    """'Show investigates: TopicDescription...' -> ('Show investigates: Topic', 'Description...').

    For shows without S/E data whose desc starts with the show name and a colon,
    with the description jammed straight onto the end of the episode title.
    The split is at the first lowercase-to-capital transition after the show name."""
    if not desc.startswith(title):
        return None
    b = BOUNDARY_RE.search(desc, len(title) + 1)
    if not b or b.start() > MAX_SUBTITLE:
        return None
    head = desc[:b.start()].strip()
    if ": " not in head:
        return None
    return head, desc[b.start():].strip()


def update_history_from_root(root, h):
    for p in root.findall("programme"):
        if not p.findtext("title"):
            continue
        d = parse_time(p.get("start")).date().isoformat()
        dates = h["dates"].setdefault(show_key(p), [])
        if d not in dates:
            dates.append(d)
        dk = desc_key(p)
        if dk:
            start = parse_time(p.get("start")).astimezone(timezone.utc).isoformat()
            if dk not in h["descs"] or start < h["descs"][dk]:
                h["descs"][dk] = start
        info = parse_desc_episode(p)
        if info:
            ek = f"{show_key(p)}|{info[0]}|{info[1]}"
            start = parse_time(p.get("start")).astimezone(timezone.utc).isoformat()
            if ek not in h["episodes"] or start < h["episodes"][ek]:
                h["episodes"][ek] = start


# ---------- element helpers ----------
def set_episode_num(p, season, episode):
    """Write xmltv_ns (zero-based, 'S.E.') plus an onscreen tag."""
    for e in p.findall("episode-num"):
        p.remove(e)
    ns = ET.SubElement(p, "episode-num", {"system": "xmltv_ns"})
    ns.text = f"{season - 1}.{episode - 1}."
    on = ET.SubElement(p, "episode-num", {"system": "onscreen"})
    on.text = f"S{season:02d}E{episode:02d}"


def set_text(p, tag, text, after=None):
    el = p.find(tag)
    if el is None:
        el = ET.Element(tag)
        anchor = p.find(after) if after else None
        p.insert(list(p).index(anchor) + 1 if anchor is not None else 0, el)
    el.text = text


def add_previously_shown(p):
    if p.find("previously-shown") is None:
        ET.SubElement(p, "previously-shown")


def has_real_episode(p):
    return any((e.text or "").strip().strip(".") for e in p.findall("episode-num"))


def is_series(p, h):
    title = p.findtext("title") or ""
    if any(re.search(rx, title, re.I) for rx in NEVER_SERIES):
        return False
    if any(re.search(rx, title, re.I) for rx in FORCE_SERIES):
        return True
    minutes = (parse_time(p.get("stop")) - parse_time(p.get("start"))).total_seconds() / 60
    if minutes > MOVIE_MIN_MINUTES:
        return False
    return len(set(h["dates"].get(show_key(p), []))) >= MIN_DISTINCT_DATES


# ---------- main processing ----------
def process(root, h):
    stats = {"total": 0, "from_desc": 0, "synthetic": 0, "rerun": 0, "untouched": 0}
    synthetic_titles = set()
    for p in root.findall("programme"):
        stats["total"] += 1
        title = p.findtext("title")
        if not title:
            stats["untouched"] += 1
            continue
        start = parse_time(p.get("start"))
        start_utc = start.astimezone(timezone.utc).isoformat()

        info = None if has_real_episode(p) else parse_desc_episode(p)
        if info:
            season, episode, ep_title, clean = info
            set_episode_num(p, season, episode)
            if ep_title and not (p.findtext("sub-title") or "").strip():
                set_text(p, "sub-title", ep_title, after="title")
            set_text(p, "desc", clean)
            stats["from_desc"] += 1
            ek = f"{show_key(p)}|{season}|{episode}"
            first = h["episodes"].get(ek)
            if first and first < start_utc:
                add_previously_shown(p)
                stats["rerun"] += 1
        elif not has_real_episode(p) and is_series(p, h):
            # No episode info anywhere: unique, date-based numbering per airing.
            num_start = start
            dk = desc_key(p)
            first = h["descs"].get(dk) if dk else None
            is_rerun = bool(first and first < start_utc)   # same description seen earlier
            if first:
                num_start = datetime.fromisoformat(first).astimezone(LOCAL_TZ)
            season = num_start.year
            episode = int(num_start.strftime("%m%d%H%M"))      # e.g. 11170630
            set_episode_num(p, season, episode)
            if is_rerun:                    # after episode-num, as the XMLTV DTD orders it
                add_previously_shown(p)
                stats["rerun"] += 1
            if not (p.findtext("sub-title") or "").strip():
                emb = split_embedded_title(title, (p.findtext("desc") or "").strip())
                if emb:
                    set_text(p, "sub-title", emb[0], after="title")
                    set_text(p, "desc", emb[1])
                else:
                    set_text(p, "sub-title", start.strftime("%Y-%m-%d %H:%M"), after="title")
            stats["synthetic"] += 1
            synthetic_titles.add(title)
        else:
            stats["untouched"] += 1
    return stats, synthetic_titles


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "openepg-fix/2.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def raw_dir():
    return Path(os.environ.get("RAW_DIR") or HISTORY_PATH.parent / "raw")


def fetch_source(force=False):
    """Get the source guide, keeping a local copy.  Returns (bytes, is_current).

    - A local copy younger than MIN_FETCH_HOURS is used instead of downloading.
    - A download is only saved if it parses as XML (so an error page can't
      overwrite a good copy).
    - If the download fails and a local copy exists, that copy is used and
      is_current is False.
    """
    rdir = raw_dir()
    latest = rdir / "latest.xml"
    age_h = (time.time() - latest.stat().st_mtime) / 3600 if latest.exists() else None

    if not force and age_h is not None and age_h < MIN_FETCH_HOURS:
        print(f"Using local copy ({age_h:.1f}h old); skipping download")
        return latest.read_bytes(), True
    try:
        raw = fetch(SOURCE_URL)
        ET.fromstring(raw)                       # sanity check
    except Exception as e:
        if age_h is None:
            raise
        print(f"WARNING: download failed ({e}); using local copy from {age_h:.1f}h ago",
              file=sys.stderr)
        return latest.read_bytes(), False

    atomic_write(latest, raw)
    atomic_write(rdir / f"guide-{datetime.now(LOCAL_TZ):%Y%m%d-%H%M}.xml", raw)
    cutoff = time.time() - RAW_KEEP_DAYS * 86400
    for f in rdir.glob("guide-*.xml"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
    print(f"Downloaded {len(raw):,} bytes; saved to {latest}")
    return raw, True


def run_once(input_path=None, out_path=None, h=None, force=False):
    """Fetch (or read) the guide, process it, write the output. Safe to call repeatedly."""
    out_path = Path(out_path) if out_path else OUTPUT_PATH
    h = h if h is not None else load_history()
    if input_path:
        raw, current = Path(input_path).read_bytes(), True
    else:
        raw, current = fetch_source(force)
    root = ET.fromstring(raw)

    update_history_from_root(root, h)      # BEFORE processing, so reruns/series see this file
    stats, synth = process(root, h)
    save_history(h)

    ET.indent(root)
    atomic_write(out_path, ET.tostring(root, encoding="utf-8", xml_declaration=True))
    print(f"{stats['total']} programmes: {stats['from_desc']} had S/E in desc, "
          f"{stats['synthetic']} given synthetic numbers, {stats['rerun']} marked rerun, "
          f"{stats['untouched']} untouched")
    if synth:
        print("Synthetic-numbered shows:", ", ".join(sorted(synth)))
    return current        # False => download failed and an older local copy was used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", help="local XMLTV file instead of SOURCE_URL")
    ap.add_argument("-o", "--output", help="output path (default OUTPUT_PATH)")
    ap.add_argument("--history", help="history file (default HISTORY_PATH)")
    ap.add_argument("--force-fetch", action="store_true",
                    help="download even if the local copy is recent")
    ap.add_argument("--seed", nargs="+", help="old XMLTV files to add to history only")
    args = ap.parse_args()

    global HISTORY_PATH
    if args.history:
        HISTORY_PATH = Path(args.history)
    out_path = Path(args.output) if args.output else OUTPUT_PATH
    h = load_history()

    if args.seed:
        for f in args.seed:
            update_history_from_root(ET.parse(f).getroot(), h)
        save_history(h)
        print(f"Seeded history from {len(args.seed)} file(s).")
        return

    run_once(args.input, out_path, h, force=args.force_fetch)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"openepg_fix failed: {e}", file=sys.stderr)
        sys.exit(1)   # previous output stays in place (writes are atomic)
