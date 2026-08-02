#!/usr/bin/env python3
"""
OPENMAT daily sweep.

Fetches every listed gym's OWN website, pulls out the schedule-ish lines
(anything mentioning a day, a time, or "open mat"), and compares them to
yesterday's snapshot.

It NEVER edits gym data. Times, prices and formats stay hand-verified.
All it does is say "this gym's schedule page changed, go look".

  no changes  -> bump LAST_UPDATE, commit the snapshot
  changes     -> write a report; the workflow opens a GitHub issue

Usage:
  python3 .github/scripts/sweep.py            # full run, writes snapshot
  python3 .github/scripts/sweep.py --dry-run  # no writes
  python3 .github/scripts/sweep.py --limit 10 # first 10 gyms (testing)
"""

import argparse
import concurrent.futures
import datetime
import gzip
import html
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX = os.path.join(ROOT, "index.html")
SNAPSHOT = os.path.join(ROOT, ".github", "data", "snapshots.json")
REPORT = os.path.join(ROOT, ".github", "data", "sweep-report.md")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 25
WORKERS = 8
MAX_LINES = 180          # cap the fingerprint so one bloated page can't dominate
FAIL_STREAK_ALERT = 3    # unreachable this many days running = probably really down

# Directories are provenance only, never a source of truth (and never linked in-app).
BAD_HOSTS = ("openmatlocator", "jitsopenmats", "facebook.com", "instagram.com")

DAY_RE = re.compile(
    r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(day|s|nesday|rsday|urday)?\b", re.I)
TIME_RE = re.compile(r"\b\d{1,2}\s*[:.]\s*\d{2}\s*(am|pm)?\b|\b\d{1,2}\s*(am|pm)\b", re.I)
OPENMAT_RE = re.compile(r"open\s*mat|open\s*roll|open\s*training", re.I)

# Lines that change on every page load and mean nothing to us.
NOISE_RE = re.compile(
    r"copyright|\ball rights reserved\b|cookie|privacy policy|\bcsrf\b|nonce|"
    r"session|cache|©\s*\d{4}|\bversion\b|loading|javascript|"
    r"\b(19|20)\d{2}-\d{2}-\d{2}t\d{2}:\d{2}\b",  # ISO timestamps
    re.I)

SCHEDULE_HREF_RE = re.compile(
    r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\']', re.I)
SCHEDULE_WORD_RE = re.compile(r"schedule|timetable|class-times|open-?mat|classes", re.I)
ASSET_RE = re.compile(r"\.(css|js|json|png|jpe?g|gif|svg|webp|ico|woff2?|ttf|pdf|mp4|xml)(\?|$)", re.I)


# ---------- reading the app's own data ----------

def load_gyms(limit=None):
    """Pull gym -> official site out of the MATS array. One entry per gym."""
    src = open(INDEX, encoding="utf-8").read()
    start = src.index("const MATS = [")
    end = src.index("\n];", start)
    body = src[start:end]

    entries = re.findall(r"\{\s*gym:\"(.*?)\".*?\}", body, re.S)
    sites = dict()
    order = []
    for m in re.finditer(r"\{\s*gym:\"(.*?)\"(.*?)\}", body, re.S):
        gym, rest = m.group(1), m.group(2)
        site_m = re.search(r'site:"(.*?)"', rest)
        site = site_m.group(1) if site_m else ""
        if gym not in sites:
            order.append(gym)
            sites[gym] = ""
        if site and not sites[gym] and not any(b in site for b in BAD_HOSTS):
            sites[gym] = site

    gyms = [(g, sites[g]) for g in order if sites[g]]
    skipped = [g for g in order if not sites[g]]
    if limit:
        gyms = gyms[:limit]
    return gyms, skipped, len(entries)


# ---------- fetching ----------

def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read(3_000_000)
        enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        elif enc == "deflate":
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        charset = r.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace"), r.geturl()


def schedule_url(page_html, base):
    """If the homepage links to a schedule page, that's the page worth watching."""
    best = None
    for href in SCHEDULE_HREF_RE.findall(page_html):
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        if ASSET_RE.search(href) or not SCHEDULE_WORD_RE.search(href):
            continue
        full = urllib.parse.urljoin(base, href).split("#")[0]
        if urllib.parse.urlparse(full).netloc != urllib.parse.urlparse(base).netloc:
            continue
        if full.rstrip("/") == base.rstrip("/"):
            continue
        path = urllib.parse.urlparse(full).path.rstrip("/")
        if not re.search(r"schedule|timetable|open-?mat", path, re.I):
            continue  # "classes" alone chases kids-class pages
        score = 2 if re.search(r"(schedule|timetable|open-?mat)$", path, re.I) else 1
        if best is None or score > best[0]:
            best = (score, full)
    return best[1] if best else None


# ---------- fingerprinting ----------

def fingerprint(page_html):
    """
    Schedule-ish lines only. Returns (text_lines, image_lines).

    text_lines  = readable schedule text. A gym with none of these is "blind":
                  its schedule is a picture or a JS widget, so it stays on the
                  manual re-verify rotation.
    image_lines = filenames of schedule images. Gyms that post their timetable
                  as a JPG usually upload a new file when it changes, so the
                  filename alone is a decent change signal.
    """
    t = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", " ", page_html)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|td|h[1-6])>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)

    lines = set()
    for raw in t.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if not (6 <= len(line) <= 160):
            continue
        if NOISE_RE.search(line):
            continue
        if not (OPENMAT_RE.search(line) or (DAY_RE.search(line) and TIME_RE.search(line))
                or (TIME_RE.search(line) and len(line) < 60)):
            continue
        lines.add(line.lower())

    imgs = set()
    for tag in re.findall(r"<img\b[^>]*>", page_html, re.I):
        src = re.search(r'(?:data-src|srcset|src)\s*=\s*["\']([^"\']+)', tag, re.I)
        alt = re.search(r'alt\s*=\s*["\']([^"\']*)', tag, re.I)
        if not src:
            continue
        hay = src.group(1) + " " + (alt.group(1) if alt else "")
        if not re.search(r"schedule|timetable|class[-_ ]?times", hay, re.I):
            continue
        name = src.group(1).split(" ")[0].split("?")[0].rstrip("/").split("/")[-1]
        if name:
            imgs.add("image: " + name.lower()[:90])

    return sorted(lines)[:MAX_LINES], sorted(imgs)[:12]


def check(gym, site):
    out = {"gym": gym, "site": site, "watched": site, "lines": [], "text": 0, "error": ""}
    try:
        page, final = fetch(site)
        out["watched"] = final
        text, imgs = fingerprint(page)
        sched = schedule_url(page, final)
        if sched:
            try:
                page2, final2 = fetch(sched)
                text2, imgs2 = fingerprint(page2)
                # Only switch to the schedule page if it carries more signal.
                if len(text2) + len(imgs2) > len(text) + len(imgs):
                    text, imgs, out["watched"] = text2, imgs2, final2
            except Exception:
                pass
        out["lines"] = text + imgs
        out["text"] = len(text)
    except urllib.error.HTTPError as e:
        out["error"] = "HTTP %s" % e.code
    except Exception as e:
        out["error"] = type(e).__name__ + (": %s" % e)[:80]
    return out


# ---------- report ----------

def diff_lines(old, new):
    o, n = set(old), set(new)
    return sorted(n - o), sorted(o - n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    today = datetime.date.today().isoformat()
    gyms, no_site, n_entries = load_gyms(args.limit)
    print("sweep: %d sessions, %d gyms with an official site, %d without"
          % (n_entries, len(gyms), len(no_site)), file=sys.stderr)

    prev = {}
    if os.path.exists(SNAPSHOT):
        prev = json.load(open(SNAPSHOT, encoding="utf-8")).get("gyms", {})
    first_run = not prev

    results = []
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(lambda a: check(*a), gyms):
            results.append(r)
            print(("  ok   " if not r["error"] else "  FAIL ") + r["gym"]
                  + (" (%d lines)" % len(r["lines"]) if not r["error"] else " " + r["error"]),
                  file=sys.stderr)

    changed, unreachable, persistent, blind, thin = [], [], [], [], []
    snap = {}
    for r in results:
        old = prev.get(r["gym"], {})
        old_lines = old.get("lines") or []
        if r["error"]:
            fails = old.get("fails", 0) + 1
            snap[r["gym"]] = dict(old, error=r["error"], fails=fails, lastFail=today)
            unreachable.append((r["gym"], r["error"], fails))
            if fails >= FAIL_STREAK_ALERT:
                persistent.append((r["gym"], r["site"], fails))
            continue

        # A page that used to read fine and now reads as nothing is a blocked or
        # half-rendered fetch, not a gym that deleted its schedule. Keep what we
        # had and say so, rather than screaming about a change that didn't happen.
        if old_lines and len(r["lines"]) < len(old_lines) * 0.4:
            snap[r["gym"]] = dict(old, checked=today, fails=0, thinAt=today)
            thin.append((r["gym"], r["watched"], len(old_lines), len(r["lines"])))
            continue

        snap[r["gym"]] = {"site": r["site"], "watched": r["watched"], "lines": r["lines"],
                          "text": r["text"], "checked": today, "fails": 0}
        if not r["lines"]:
            blind.append((r["gym"], r["watched"]))
        # Nothing to diff against on a gym we've never successfully read before.
        elif old_lines and not first_run:
            added, removed = diff_lines(old_lines, r["lines"])
            if added or removed:
                changed.append((r["gym"], r["watched"], added, removed))

    ok = len(results) - len(unreachable)
    # Monday = the weekly full re-verify beat in CLAUDE.md; that's when the blind
    # list is worth printing in full.
    monday = datetime.date.today().weekday() == 0

    lines = []
    if first_run:
        lines.append("Baseline captured for %d gyms. Change detection starts tomorrow.\n" % ok)
    if changed:
        lines.append("## %d gym page(s) changed — verify by hand before touching the map\n" % len(changed))
        for gym, url, added, removed in changed:
            lines.append("### %s\n%s\n" % (gym, url))
            for a in added[:12]:
                lines.append("- **+** `%s`" % a)
            for d in removed[:12]:
                lines.append("- **−** `%s`" % d)
            if len(added) > 12 or len(removed) > 12:
                lines.append("- _(+%d more)_" % (max(0, len(added) - 12) + max(0, len(removed) - 12)))
            lines.append("")
    if persistent:
        lines.append("## Unreachable %d+ days running\n" % FAIL_STREAK_ALERT)
        for gym, url, fails in persistent:
            lines.append("- **%s** — %s (%d days)" % (gym, url, fails))
        lines.append("")
    if thin:
        lines.append("## Came back thin — probably a blocked fetch, not a change\n")
        for gym, url, was, now in thin:
            lines.append("- **%s** — %s (%d schedule lines yesterday, %d today; "
                         "kept yesterday's copy)" % (gym, url, was, now))
        lines.append("")
    handcheck = blind + [(g, u) for g, u, _, _ in thin]
    if handcheck and monday:
        lines.append("## Hand-check this week (%d)\n" % len(handcheck))
        lines.append("_The robot can't read these — the schedule is a picture, a JS widget, "
                     "or the site blocks it. They only get verified when a person looks._\n")
        for gym, url in handcheck:
            lines.append("- [ ] **%s** — %s" % (gym, url))
        lines.append("")
    lines.append("---")
    lines.append("_%s · %d gyms checked · %d reachable · %d changed · %d unreachable · "
                 "%d unreadable · %d thin_"
                 % (today, len(results), ok, len(changed), len(unreachable), len(blind), len(thin)))
    report = "\n".join(lines)

    # A thin read is noise, not news: it never opens an issue on its own.
    quiet = not changed and not persistent and not (handcheck and monday) and not first_run
    healthy = ok >= max(1, int(len(results) * 0.8))

    if not args.dry_run:
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        json.dump({"generated": today, "gyms": snap}, open(SNAPSHOT, "w", encoding="utf-8"),
                  indent=1, sort_keys=True)
        open(REPORT, "w", encoding="utf-8").write(report + "\n")
        # Only claim a fresh "Updated" date when the sweep actually cleared.
        if quiet and healthy:
            src = open(INDEX, encoding="utf-8").read()
            new = re.sub(r'const LAST_UPDATE = "\d{4}-\d{2}-\d{2}";',
                         'const LAST_UPDATE = "%s";' % today, src, count=1)
            assert new.count("{ gym:") == src.count("{ gym:"), "entry count changed — refusing to write"
            if new != src:
                open(INDEX, "w", encoding="utf-8").write(new)

    print(report)
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            f.write("changed=%d\n" % len(changed))
            f.write("alert=%s\n" % ("true" if (changed or persistent) else "false"))
            f.write("bumped=%s\n" % ("true" if (quiet and healthy) else "false"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
