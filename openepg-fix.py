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
  guide_fix.py                          # fetch SOURCE_URL, write OUTPUT_PATH
  guide_fix.py -i in.xml -o out.xml     # local file in/out (testing)
  guide_fix.py --seed old1.xml old2.xml # only add old guide files to history
"""
import argparse, json, os, re, sys, tempfile, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------- config ----------------
#SOURCE_URL   = "https://www.open-epg.com/app/pdownload.php?file=yourID.xml"
OUTPUT_PATH  = Path("/usr/share/jellyfin/guide.xml")  # or any path Jellyfin can read
HISTORY_PATH = Path("history.json")
LOCAL_TZ     = ZoneInfo("America/Toronto")

MIN_DISTINCT_DATES = 2     # no-episode-info shows: seen on this many days => series
HISTORY_DAYS       = 60
MOVIE_MIN_MINUTES  = 100   # no-episode-info shows longer than this => movie/special

# Regexes matched against <title>.  Use FORCE for weekly/occasional shows that
# the history check can't catch yet; NEVER for things that must not be a series.
FORCE_SERIES = [r"^CTV National News", r"^CTV News", r"^Global News"]
NEVER_SERIES = [r"Grey Cup", r"^Movie:", r"^Sign[- ]Off"]
# ----------------------------------------

# "S08 E14 — Title" / "S08 E14Description" / "S08 E14 - Title"
SE_RE = re.compile(r"^\s*S(\d+)\s*E(\d+)\s*(?:(—|–|-)\s*)?(.*)$", re.S)

# Episode title runs straight into the description: "...BagA W5 investigation".
# Boundary = lowercase/digit/punctuation immediately followed by a capital, no space.
# Also need special handling for quotes to only split on second one
_END = r"[a-z0-9.!?)]"
_QUOTE = r"[\"'’”]"
BOUNDARY_RE = re.compile(
    rf"(?:(?<={_END})|(?<={_END}{_QUOTE})|(?<={_END}\\{_QUOTE}))(?=[A-Z])"
)
#BOUNDARY_RE = re.compile(r"(?<=[a-z0-9.!?'’\")])(?=[A-Z])")
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
    return h


def save_history(h):
    cutoff = datetime.now(LOCAL_TZ) - timedelta(days=HISTORY_DAYS)
    h["dates"] = {k: sorted(d for d in set(v) if d >= cutoff.date().isoformat())
                  for k, v in h["dates"].items()}
    h["dates"] = {k: v for k, v in h["dates"].items() if v}
    cut_iso = cutoff.astimezone(timezone.utc).isoformat()
    h["episodes"] = {k: v for k, v in h["episodes"].items() if v >= cut_iso}
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


def update_history_from_root(root, h):
    for p in root.findall("programme"):
        if not p.findtext("title"):
            continue
        d = parse_time(p.get("start")).date().isoformat()
        dates = h["dates"].setdefault(show_key(p), [])
        if d not in dates:
            dates.append(d)
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
            season = start.year
            episode = int(start.strftime("%m%d%H%M"))      # e.g. 11170630
            set_episode_num(p, season, episode)
            if not (p.findtext("sub-title") or "").strip():
                set_text(p, "sub-title", start.strftime("%Y-%m-%d %H:%M"), after="title")
            stats["synthetic"] += 1
            synthetic_titles.add(title)
        else:
            stats["untouched"] += 1
    return stats, synthetic_titles


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "guide-fix/2.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i", "--input", help="local XMLTV file instead of SOURCE_URL")
    ap.add_argument("-o", "--output", help="output path (default OUTPUT_PATH)")
    ap.add_argument("--history", help="history file (default HISTORY_PATH)")
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

    raw = Path(args.input).read_bytes() if args.input else fetch(SOURCE_URL)
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


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"guide_fix failed: {e}", file=sys.stderr)
        sys.exit(1)   # previous output stays in place (writes are atomic)
