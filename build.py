#!/usr/bin/env python3
"""Build the daily briefing page.

Collects headlines from each publication's public RSS feeds (falling back to a
Google News site search when a feed is missing or blocked), optionally asks
Claude to write a short briefing and extract upcoming events, then writes
docs/index.html plus a dated copy in docs/archive/.

Usage:
    python build.py          # real run
    python build.py --demo   # render with invented placeholder data, no network
"""
import argparse
import calendar
import datetime as dt
import html
import json
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
TZ_NAME = os.environ.get("BRIEFING_TZ", "Europe/Zurich")
TZ = ZoneInfo(TZ_NAME)
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
UA = "Mozilla/5.0 (compatible; PersonalDailyBriefing/1.0; RSS reader)"
ARCHIVE_DAYS_SHOWN = 14


# ---------------------------------------------------------------- collecting

def clean_text(s, limit):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", html.unescape(s)).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return dt.datetime.fromtimestamp(calendar.timegm(t), tz=dt.timezone.utc)
    return None


def fetch_feed(url):
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        })
        r.raise_for_status()
        entries = feedparser.parse(r.content).entries
        print(f"  {len(entries):3d}  {url}")
        return entries
    except Exception as ex:  # a dead feed must never break the whole page
        print(f"  !!!  {url}: {ex}", file=sys.stderr)
        return []


def entry_image(e):
    """Best image URL a feed entry offers, or None."""
    cands = []
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key) or []:
            url = m.get("url")
            if url and (m.get("medium") in (None, "image") or "image" in (m.get("type") or "")):
                try:
                    w = int(m.get("width") or 0)
                except ValueError:
                    w = 0
                cands.append((w, url))
    for l in (e.get("enclosures") or []) + (e.get("links") or []):
        if "image" in (l.get("type") or "") and l.get("href"):
            cands.append((0, l["href"]))
    if not cands:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)', e.get("summary") or "")
        if m:
            cands.append((0, html.unescape(m.group(1))))
    cands = [c for c in cands if c[1].startswith(("http://", "https://"))]
    if not cands:
        return None
    return max(cands, key=lambda c: c[0])[1]


def page_image(url):
    """Fallback: read the og:image preview picture from the article page."""
    if "news.google.com" in url:
        return None
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": UA})
        head = r.text[:200_000]
    except Exception:
        return None
    for pat in (r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']'):
        m = re.search(pat, head, re.I)
        if m and m.group(1).startswith(("http://", "https://")):
            return html.unescape(m.group(1))
    return None


DEFAULT_EXCLUDES = [r"^opinion\b", r"\bopinion\s*\|", r"^(letter|letters)\s*:", r"^tribune\b"]


def google_news_url(site, lang):
    hl, gl = {"fr": ("fr", "FR")}.get(lang, ("en-US", "US"))
    q = requests.utils.quote(f"site:{site} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"


def collect_source(source, cfg, now):
    cutoff = now - dt.timedelta(hours=cfg.get("max_age_hours", 36))
    items, seen = [], set()
    excludes = [re.compile(p, re.I) for p in cfg.get("exclude_title_patterns", DEFAULT_EXCLUDES)]

    def add(entries, via_google=False):
        for e in entries:
            title = clean_text(e.get("title"), 220)
            link = e.get("link")
            if not title or not link:
                continue
            if via_google:  # Google News appends " - Publisher"
                title = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", title)
            if any(x.search(title) for x in excludes) or "/opinion" in link:
                continue
            key = title.lower()
            t = entry_time(e)
            if key in seen or (t and t < cutoff):
                continue
            seen.add(key)
            summary = "" if via_google else clean_text(e.get("summary") or e.get("description"), 240)
            if summary.lower().startswith(title.lower()[:50]):
                summary = ""
            items.append({"title": title, "link": link, "summary": summary,
                          "image": None if via_google else entry_image(e),
                          "time": t.isoformat() if t else None})

    for url in source.get("feeds", []):
        add(fetch_feed(url))
    if len(items) < 3 and source.get("site"):
        add(fetch_feed(google_news_url(source["site"], source.get("lang", "en"))), via_google=True)

    items.sort(key=lambda i: i["time"] or "", reverse=True)
    items = items[: cfg.get("max_per_source", 8)]
    for it in items[: cfg.get("fetch_images_for_top", 4)]:
        if not it["image"]:
            it["image"] = page_image(it["link"])
    return items


def collect_all(cfg, now):
    desks = []
    for desk in cfg["desks"]:
        sources = []
        for src in desk["sources"]:
            print(f"{src['name']}")
            sources.append({**src, "items": collect_source(src, cfg, now)})
        desks.append({**desk, "sources": sources})
    n = 0
    for desk in desks:
        for src in desk["sources"]:
            for item in src["items"]:
                n += 1
                item["id"] = f"h{n}"
                item["desk"] = desk["id"]
                item["source"] = src["name"]
    return desks


# ---------------------------------------------------------------- Claude

PROMPT = """You are preparing a morning briefing for a busy executive. Today is {today} (time zone {tz}).
Below are today's headlines from several publications, as JSON. Each has an id.

Return ONLY a JSON object, with no prose and no code fences, in this shape:
{{"briefing": [{{"text": str, "desk": "business"|"world"|"defense"|"sport", "ids": [str]}}],
  "upcoming": [{{"date": "YYYY-MM-DD" or null, "when": str, "title": str, "desk": "business"|"world"|"defense"|"sport", "ids": [str]}}]}}

Rules:
- briefing: the 6 most important stories of the day across all desks (include at least one sport story and at least one space & defense story). One sentence each, at most 30 words, in English, in your own words. When several outlets cover the same story, merge them and list every id.
- upcoming: scheduled events after today that the headlines explicitly mention or clearly imply: central bank meetings, data releases, elections, votes, summits, earnings, trials, launches, matches, finals, drafts, and similar. Only include events supported by the headlines and never invent a date. If only a vague time is known, set date to null and describe it in "when" (for example "next week"); otherwise "when" is a short label like "Wed 30 Sep". Up to 12 events, sorted by date.
- Paraphrase; do not quote the articles.

Headlines:
{items}"""


def ask_claude(desks, now_local):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("No ANTHROPIC_API_KEY set: skipping briefing and upcoming events.")
        return {"briefing": [], "upcoming": []}
    items = [{"id": i["id"], "source": i["source"], "desk": i["desk"], "title": i["title"],
              "summary": i["summary"]}
             for d in desks for s in d["sources"] for i in s["items"]]
    if not items:
        return {"briefing": [], "upcoming": []}
    prompt = PROMPT.format(today=now_local.strftime("%A %d %B %Y"), tz=TZ_NAME,
                           items=json.dumps(items, ensure_ascii=False))
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=180, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, json={"model": MODEL, "max_tokens": 4000,
                 "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
        data = json.loads(text)
    except Exception as ex:
        print(f"Claude step failed, page will be built without it: {ex}", file=sys.stderr)
        return {"briefing": [], "upcoming": []}

    valid = {i["id"] for i in items}
    today = now_local.date().isoformat()
    for group in ("briefing", "upcoming"):
        data[group] = [x for x in data.get(group, []) if isinstance(x, dict)]
        for x in data[group]:
            x["ids"] = [i for i in x.get("ids", []) if i in valid]
            if x.get("desk") not in ("business", "world", "defense", "sport"):
                x["desk"] = "world"
    data["upcoming"] = [e for e in data["upcoming"] if not e.get("date") or e["date"] > today]
    data["upcoming"].sort(key=lambda e: e.get("date") or "9999")
    return data


# ---------------------------------------------------------------- rendering

def esc(s):
    return html.escape(s or "", quote=True)


def fmt_time(iso):
    if not iso:
        return ""
    return dt.datetime.fromisoformat(iso).astimezone(TZ).strftime("%H:%M")


def day_label(date_str, today):
    if not date_str:
        return "Date to be confirmed"
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        return date_str
    if d == today + dt.timedelta(days=1):
        return "Tomorrow"
    if d.year != today.year:
        return d.strftime("%a %d %b %Y")
    return d.strftime("%A %d %B")


def render(desks, ai, now_local, archive_prefix, archive_dates):
    by_id = {i["id"]: i for d in desks for s in d["sources"] for i in s["items"]}

    def source_links(ids):
        seen, out = set(), []
        for i in ids:
            it = by_id.get(i)
            if it and it["source"] not in seen:
                seen.add(it["source"])
                out.append(f'<a href="{esc(it["link"])}" target="_blank" rel="noopener">{esc(it["source"])}</a>')
        return ", ".join(out)

    def img_tag(url, cls):
        if not url:
            return ""
        return (f'<img class="{cls}" src="{esc(url)}" alt="" loading="lazy" '
                f'referrerpolicy="no-referrer" onerror="this.remove()">')

    def first_image(ids):
        for i in ids:
            it = by_id.get(i)
            if it and it.get("image"):
                return it["image"]
        return None

    # briefing: first story is the large feature, the rest are picture cards
    if ai["briefing"]:
        cards = []
        for n, b in enumerate(ai["briefing"]):
            cls = "brief feature" if n == 0 else "brief"
            pic = first_image(b["ids"])
            first = by_id.get(b["ids"][0]) if b["ids"] else None
            href = esc(first["link"]) if first else "#"
            cards.append(
                f'<li class="{cls}{"" if pic else " no-img"}" data-desk="{b["desk"]}">'
                f'<a class="card-link" href="{href}" target="_blank" rel="noopener">'
                f'<div class="pic">{img_tag(pic, "cover")}</div>'
                f'<p>{esc(b.get("text"))}</p></a>'
                f'<span class="via">{source_links(b["ids"])}</span></li>')
        briefing = (f'<section class="briefing" aria-labelledby="brief-h"><h2 id="brief-h">The essentials</h2>'
                    f'<ul>{"".join(cards)}</ul></section>')
    else:
        briefing = ""

    # desks: each source leads with its best-illustrated story, then compact rows with thumbnails
    desk_html = []
    for d in desks:
        blocks = []
        for s in d["sources"]:
            items = list(s["items"])
            if items:
                lead_i = next((k for k, it in enumerate(items[:3]) if it.get("image")), 0)
                items.insert(0, items.pop(lead_i))
                lis = []
                for k, i in enumerate(items):
                    t = f'<time>{fmt_time(i["time"])}</time>' if i["time"] else ""
                    link = f'href="{esc(i["link"])}" target="_blank" rel="noopener"'
                    if k == 0:
                        summ = f'<p>{esc(i["summary"])}</p>' if i["summary"] else ""
                        lis.append(f'<li class="lead"><a {link}>{img_tag(i.get("image"), "lead-img")}'
                                   f'<span class="hl">{esc(i["title"])}</span></a>{t}{summ}</li>')
                    else:
                        lis.append(f'<li class="row"><a {link}><span class="hl">{esc(i["title"])}</span>'
                                   f'{img_tag(i.get("image"), "thumb")}</a>{t}</li>')
                lis = "".join(lis)
            else:
                lis = '<li class="empty">No headlines came through from this source today. Check its feed address in sources.json.</li>'
            lang = "fr" if s.get("lang") == "fr" else "en"
            blocks.append(f'<div class="source" lang="{lang}"><h4>{esc(s["name"])}</h4><ul>{lis}</ul></div>')
        desk_html.append(
            f'<section class="desk" id="desk-{d["id"]}" data-desk="{d["id"]}">'
            f'<h3>{esc(d["name"])}</h3><div class="sources">{"".join(blocks)}</div></section>')

    # upcoming: calendar-style date badges
    today = now_local.date()
    if ai["upcoming"]:
        groups, order = {}, []
        for e in ai["upcoming"]:
            key = e.get("date") or ""
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(e)
        parts = []
        for key in order:
            try:
                d = dt.date.fromisoformat(key)
                badge = (f'<div class="badge"><span class="wd">{d.strftime("%a")}</span>'
                         f'<span class="dn">{d.day}</span><span class="mo">{d.strftime("%b")}</span></div>')
                label = "Tomorrow" if d == today + dt.timedelta(days=1) else ""
            except ValueError:
                badge = '<div class="badge tbc"><span class="dn">?</span><span class="mo">TBC</span></div>'
                label = "Date to be confirmed"
            evs = "".join(
                f'<li class="event" data-desk="{e["desk"]}"><strong>{esc(e.get("title"))}</strong>'
                + (f'<span class="when">{esc(e.get("when"))}</span>' if not e.get("date") and e.get("when") else "")
                + f'<span class="via">{source_links(e["ids"])}</span></li>'
                for e in groups[key])
            lab = f'<span class="rel">{label}</span>' if label else ""
            parts.append(f'<li class="day">{badge}<div class="evs">{lab}<ul>{evs}</ul></div></li>')
        upcoming = f'<ol class="timeline">{"".join(parts)}</ol>'
    elif os.environ.get("ANTHROPIC_API_KEY"):
        upcoming = '<p class="note">No scheduled events were mentioned in today\'s headlines.</p>'
    else:
        upcoming = ('<p class="note">Add an ANTHROPIC_API_KEY secret to the repository to get the '
                    'daily summary and the list of upcoming events.</p>')

    archive = "".join(
        f'<li><a href="{archive_prefix}{d}.html">{dt.date.fromisoformat(d).strftime("%a %d %b")}</a></li>'
        for d in archive_dates)

    page = TEMPLATE
    for k, v in {
        "TITLE": f'Briefing, {now_local.strftime("%d %B %Y")}',
        "DATE": now_local.strftime("%A %d %B"),
        "YEAR": now_local.strftime("%Y"),
        "UPDATED": f'Updated at {now_local.strftime("%H:%M")}, {TZ_NAME.split("/")[-1]} time',
        "BRIEFING": briefing,
        "DESKS": "".join(desk_html),
        "UPCOMING": upcoming,
        "ARCHIVE": archive,
    }.items():
        page = page.replace("{{" + k + "}}", v)
    return page


TEMPLATE = (ROOT / "template.html").read_text("utf-8")


# ---------------------------------------------------------------- demo data

DEMO_IMG = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 9'>"
            "<rect width='16' height='9' fill='%238aa1b8'/><circle cx='5' cy='4' r='2' fill='%23c9d6e2'/></svg>")

def demo_data(cfg, now):
    desks = []
    n = 0
    for d in cfg["desks"]:
        srcs = []
        for s in d["sources"]:
            items = []
            for k in range(4):
                n += 1
                items.append({"id": f"h{n}", "desk": d["id"], "source": s["name"],
                              "title": f"Placeholder headline {k + 1} from {s['name']} for layout testing",
                              "link": "https://example.com", "image": DEMO_IMG if k != 2 else None, "summary": "Short placeholder description of the article, as a feed would provide it." if k % 2 == 0 else "",
                              "time": (now - dt.timedelta(hours=k * 3)).isoformat()})
            srcs.append({**s, "items": items})
        desks.append({**d, "sources": srcs})
    t = now.astimezone(TZ).date()
    ai = {"briefing": [{"text": "Placeholder summary sentence for the briefing block.", "desk": d, "ids": ["h1", "h5"]}
                       for d in ("business", "world", "sport", "business", "world", "sport")],
          "upcoming": [{"date": (t + dt.timedelta(days=i)).isoformat(), "when": "", "title": f"Placeholder event {i}",
                        "desk": ("business", "world", "sport")[i % 3], "ids": ["h2"]} for i in (1, 1, 2, 4, 6)]
          + [{"date": None, "when": "next month", "title": "Placeholder event without a date", "desk": "world", "ids": ["h3"]}]}
    return desks, ai


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="render placeholder data without network")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "sources.json").read_text("utf-8"))
    now = dt.datetime.now(dt.timezone.utc)
    now_local = now.astimezone(TZ)
    if args.demo:
        desks, ai = demo_data(cfg, now)
    else:
        desks = collect_all(cfg, now)
        ai = ask_claude(desks, now_local)

    stamp = now_local.date().isoformat()
    (DOCS / "archive").mkdir(parents=True, exist_ok=True)
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "data" / f"{stamp}.json").write_text(
        json.dumps({"desks": desks, "ai": ai}, ensure_ascii=False, indent=1), "utf-8")

    dates = sorted({p.stem for p in (DOCS / "archive").glob("*.html")} | {stamp}, reverse=True)
    dates = [d for d in dates if d != stamp][:ARCHIVE_DAYS_SHOWN]
    (DOCS / "index.html").write_text(render(desks, ai, now_local, "archive/", dates), "utf-8")
    (DOCS / "archive" / f"{stamp}.html").write_text(render(desks, ai, now_local, "", dates), "utf-8")
    (DOCS / ".nojekyll").write_text("")
    print(f"Wrote docs/index.html for {stamp}")


if __name__ == "__main__":
    main()
