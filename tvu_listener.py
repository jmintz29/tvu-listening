#!/usr/bin/env python3
"""
TVU Go — Social Listening Agent (open sources)
==============================================
Watches the open web for TVU Go / creator discourse, scores each mention for
sentiment + priority, and writes a JSON feed and an HTML dashboard.

Sources (all keyless, no approval required):
  - Reddit        public search JSON  (r/IRLstreamers, r/Twitch, r/kick, ...)
  - Hacker News   Algolia search API
  - Bluesky       public AppView searchPosts
  - Google News   RSS search

Triage:
  - If ANTHROPIC_API_KEY is set  -> Claude scores relevance/sentiment/priority.
  - Otherwise                    -> a transparent keyword heuristic runs instead,
                                    so the agent works out of the box.

Usage:
  python tvu_listener.py                 # last 72h, ./tvu_out/
  python tvu_listener.py --hours 24 --competitors
  python tvu_listener.py --no-llm        # force heuristic scoring

No third-party packages required (standard library only).
"""

import argparse
import json
import os
import re
import sys
import time
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# --------------------------------------------------------------------------- #
# CONFIG — tune these for TVU Go
# --------------------------------------------------------------------------- #
BRAND_QUERIES = ['"TVU Go"', "TVUGo", '"TVU Networks"']
# Context words that make a bare "TVU" mention likely relevant (heuristic use)
CONTEXT_WORDS = ["stream", "irl", "twitch", "kick", "youtube", "bonding",
                 "disconnect", "backpack", "bitrate", "isx", "multistream", "go live"]
SUBREDDITS = ["IRLstreamers", "Twitch", "kick", "streaming", "RTMP",
              "letsplay", "videography", "streamers"]
COMPETITOR_QUERIES = ["IRLToolkit", "UnlimitedIRL", "Streamlabs IRL",
                      '"LiveU Solo"', "Prism Live"]

UA = "tvu-listener/1.0 (social-listening; contact: social@tvunetworks.com)"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

POS_WORDS = ["love", "amazing", "impressed", "unreal", "saved", "smooth", "goat",
             "reliable", "clean", "insane", "best", "solid", "recommend", "great",
             "works", "clutch", "flawless", "praise", "shoutout", "🔥", "🙏"]
NEG_WORDS = ["dropped", "drop", "froze", "freeze", "crash", "buggy", "lag", "broken",
             "worst", "trash", "refund", "cancel", "scam", "unstable", "fail",
             "disconnect", "stutter", "problem", "issue", "terrible", "bad"]
QUESTION_HINTS = [" vs ", "which", "recommend", "should i", "anyone use",
                  "worth it", "how do", "does it", "?"]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _get_json(url, timeout=20):
    return json.loads(_get(url, timeout))


def _iso(ts):
    """Epoch seconds or ISO -> ISO8601 UTC string."""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return ts


def _age_ok(iso_str, cutoff):
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt >= cutoff
    except Exception:
        return True  # keep if we can't parse the date


# --------------------------------------------------------------------------- #
# Collectors  (fetch_* does IO, parse_* is pure so it can be unit-tested)
# --------------------------------------------------------------------------- #
def parse_reddit(obj, query):
    out = []
    for c in obj.get("data", {}).get("children", []):
        d = c.get("data", {})
        out.append({
            "id": "reddit_" + d.get("id", ""),
            "source": "Reddit",
            "query": query,
            "author": "u/" + d.get("author", "?"),
            "handle": d.get("subreddit_name_prefixed", ""),
            "url": "https://www.reddit.com" + d.get("permalink", ""),
            "title": d.get("title", ""),
            "text": (d.get("title", "") + " — " + d.get("selftext", "")).strip(" —"),
            "created_at": _iso(d.get("created_utc", 0)),
            "reach": int(d.get("score", 0)) + int(d.get("num_comments", 0)),
        })
    return out


def fetch_reddit(query, n):
    results = []
    q = urllib.parse.quote(query)
    # global search
    try:
        url = f"https://www.reddit.com/search.json?q={q}&sort=new&limit={n}&t=month"
        results += parse_reddit(_get_json(url), query)
    except Exception as e:
        print(f"  ! reddit global '{query}': {e}", file=sys.stderr)
    # targeted subreddits
    for sub in SUBREDDITS:
        try:
            url = (f"https://www.reddit.com/r/{sub}/search.json?q={q}"
                   f"&restrict_sr=1&sort=new&limit={n}&t=month")
            results += parse_reddit(_get_json(url), query)
            time.sleep(0.6)  # be polite
        except Exception as e:
            print(f"  ! reddit r/{sub} '{query}': {e}", file=sys.stderr)
    return results


def parse_hn(obj, query):
    out = []
    for h in obj.get("hits", []):
        is_story = "story" in (h.get("_tags") or [])
        text = h.get("title") or h.get("story_title") or h.get("comment_text") or ""
        url = h.get("url") or h.get("story_url") or \
            f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append({
            "id": "hn_" + str(h.get("objectID", "")),
            "source": "Hacker News",
            "query": query,
            "author": h.get("author", "?"),
            "handle": "story" if is_story else "comment",
            "url": url,
            "title": text[:120],
            "text": text,
            "created_at": _iso(h.get("created_at", "")),
            "reach": int(h.get("points") or 0) + int(h.get("num_comments") or 0),
        })
    return out


def fetch_hn(query, n):
    try:
        q = urllib.parse.quote(query)
        url = (f"http://hn.algolia.com/api/v1/search_by_date?query={q}"
               f"&tags=(story,comment)&hitsPerPage={n}")
        return parse_hn(_get_json(url), query)
    except Exception as e:
        print(f"  ! hn '{query}': {e}", file=sys.stderr)
        return []


def parse_bluesky(obj, query):
    out = []
    for p in obj.get("posts", []):
        rec = p.get("record", {})
        handle = p.get("author", {}).get("handle", "")
        rkey = p.get("uri", "").split("/")[-1]
        out.append({
            "id": "bsky_" + p.get("cid", rkey),
            "source": "Bluesky",
            "query": query,
            "author": p.get("author", {}).get("displayName") or handle,
            "handle": "@" + handle,
            "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
            "title": rec.get("text", "")[:120],
            "text": rec.get("text", ""),
            "created_at": _iso(rec.get("createdAt", "")),
            "reach": int(p.get("likeCount", 0)) + int(p.get("repostCount", 0))
                     + int(p.get("replyCount", 0)),
        })
    return out


def fetch_bluesky(query, n):
    try:
        q = urllib.parse.quote(query)
        url = ("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
               f"?q={q}&limit={n}&sort=latest")
        return parse_bluesky(_get_json(url), query)
    except Exception as e:
        print(f"  ! bluesky '{query}': {e}", file=sys.stderr)
        return []


def parse_news(xml_text, query):
    out = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        src_el = item.find("source")
        src = src_el.text if src_el is not None else "News"
        try:
            dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
            iso = dt.replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            iso = datetime.now(timezone.utc).isoformat()
        out.append({
            "id": "news_" + str(abs(hash(link)) % (10 ** 10)),
            "source": "News",
            "query": query,
            "author": src or "News",
            "handle": src or "",
            "url": link,
            "title": title,
            "text": title,
            "created_at": iso,
            "reach": 5,  # press pickup baseline
        })
    return out


def fetch_news(query, n):
    try:
        q = urllib.parse.quote(query)
        url = (f"https://news.google.com/rss/search?q={q}"
               "&hl=en-US&gl=US&ceid=US:en")
        return parse_news(_get(url), query)[:n]
    except Exception as e:
        print(f"  ! news '{query}': {e}", file=sys.stderr)
        return []


# --------------------------------------------------------------------------- #
# Collect + dedupe
# --------------------------------------------------------------------------- #
def collect(queries, per_source, hours):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    raw = []
    for q in queries:
        print(f"· scanning: {q}")
        raw += fetch_reddit(q, per_source)
        raw += fetch_hn(q, per_source)
        raw += fetch_bluesky(q, per_source)
        raw += fetch_news(q, per_source)

    seen, deduped = set(), []
    for m in raw:
        key = m["id"] or m["url"]
        if key in seen:
            continue
        if not _age_ok(m["created_at"], cutoff):
            continue
        if not (m.get("text") or "").strip():
            continue
        seen.add(key)
        deduped.append(m)
    return deduped


# --------------------------------------------------------------------------- #
# Triage — heuristic
# --------------------------------------------------------------------------- #
def _score_reach(r):
    if r <= 0:
        return 1
    import math
    return min(6, 1 + int(math.log10(r + 1) * 2.2))


def triage_heuristic(mentions):
    for m in mentions:
        t = (m["text"] or "").lower()
        m["is_competitor"] = ("tvu" not in t) and any(
            c.lower().strip('"') in t for c in COMPETITOR_QUERIES)
        m["relevant"] = ("tvu" in t) or m["is_competitor"]
        pos = sum(w in t for w in POS_WORDS)
        neg = sum(w in t for w in NEG_WORDS)
        m["sentiment"] = "neg" if neg > pos else ("pos" if pos > neg else "neu")
        is_q = any(h in t for h in QUESTION_HINTS)
        if m["sentiment"] == "neg":
            cat = "respond"
        elif is_q or m["is_competitor"]:
            cat = "opportunity"
        elif m["sentiment"] == "pos":
            cat = "amplify"
        else:
            cat = "monitor"
        m["category"] = cat
        pri = _score_reach(m["reach"])
        pri += {"respond": 3, "opportunity": 2, "amplify": 1, "monitor": 0}[cat]
        m["priority"] = max(1, min(10, pri))
        m["suggestion"] = {
            "respond": "Verify + draft a support/PR reply fast",
            "opportunity": "Reply helpfully (honest, not shill) with the right info",
            "amplify": "Reshare / thank the creator",
            "monitor": "Watch — no action yet",
        }[cat]
    return mentions


# --------------------------------------------------------------------------- #
# Triage — Claude (optional, keyed)
# --------------------------------------------------------------------------- #
TRIAGE_SYS = (
    "You triage social-listening mentions for TVU Go, an IRL live-streaming app "
    "by TVU Networks (multistream to Twitch/Kick/YouTube/TikTok/X; ISX bonding; "
    "disconnect protection; $29/mo). For each mention return STRICT JSON only.\n"
    "Fields per id: relevant(bool: is it actually about TVU/TVU Go, not the "
    "unrelated 'TVU' acronym), is_competitor(bool), sentiment(pos|neu|neg), "
    "category(respond|opportunity|amplify|monitor), priority(int 1-10), "
    "suggestion(short action, <12 words). Respond as {\"results\":[{...}]} and nothing else."
)


def _anthropic_call(payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=data,
        headers={
            "content-type": "application/json",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def triage_claude(mentions, model):
    BATCH = 15
    for i in range(0, len(mentions), BATCH):
        batch = mentions[i:i + BATCH]
        compact = [{"id": m["id"], "source": m["source"],
                    "reach": m["reach"], "text": m["text"][:500]} for m in batch]
        payload = {
            "model": model, "max_tokens": 1500, "system": TRIAGE_SYS,
            "messages": [{"role": "user", "content": json.dumps(compact)}],
        }
        try:
            resp = _anthropic_call(payload)
            txt = "".join(b.get("text", "") for b in resp.get("content", [])
                          if b.get("type") == "text")
            txt = re.sub(r"```(json)?", "", txt).strip()
            results = {r["id"]: r for r in json.loads(txt).get("results", [])}
            for m in batch:
                r = results.get(m["id"])
                if not r:
                    triage_heuristic([m])
                    continue
                m.update({
                    "relevant": bool(r.get("relevant", True)),
                    "is_competitor": bool(r.get("is_competitor", False)),
                    "sentiment": r.get("sentiment", "neu"),
                    "category": r.get("category", "monitor"),
                    "priority": int(r.get("priority", 3)),
                    "suggestion": r.get("suggestion", "Review"),
                })
        except Exception as e:
            print(f"  ! claude batch failed ({e}); using heuristic", file=sys.stderr)
            triage_heuristic(batch)
    return mentions


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
SENTI_LABEL = {"pos": "positive", "neu": "neutral", "neg": "negative"}
LIMITED = {"Reddit", "Bluesky", "News", "Hacker News"}  # note: no Twitch/Kick/TikTok/Discord here


def _sig_bars(n, color):
    bars = ""
    for i in range(1, 6):
        on = n >= i * 2
        bars += (f'<i style="height:{3 + i * 2}px;background:'
                 f'{color if on else "var(--faint)"}"></i>')
    return f'<span class="sig-bars">{bars}</span>'


def render_html(mentions, hours):
    order = {"respond": 0, "opportunity": 1, "amplify": 2, "monitor": 3}
    mentions = sorted(mentions, key=lambda m: (-m["priority"], order.get(m["category"], 9)))
    scol = {"pos": "var(--good)", "neu": "var(--warn)", "neg": "var(--live)"}

    # brief = top items per action bucket
    brief = {}
    for m in mentions:
        if m["category"] in ("respond", "opportunity", "amplify") and m["category"] not in brief:
            brief[m["category"]] = m
    bk = {"respond": ("▲ Respond now", ""), "opportunity": ("◆ Opportunity", "opp"),
          "amplify": ("● Amplify", "pos")}
    alert_html = ""
    for cat in ("respond", "opportunity", "amplify"):
        if cat in brief:
            m = brief[cat]
            lbl, cls = bk[cat]
            alert_html += (f'<div class="alert {cls}"><div class="k">{lbl}</div>'
                           f'<p>{html.escape(m["text"][:150])}</p></div>')

    # source + sentiment counts
    from collections import Counter
    src_ct = Counter(m["source"] for m in mentions)
    sen_ct = Counter(m["sentiment"] for m in mentions)
    src_line = " · ".join(f"{k} {v}" for k, v in src_ct.most_common())

    cards = ""
    for m in mentions:
        lim = '<span class="lim">limited access</span>' if False else ""
        cards += f'''<div class="panel mention">
      <div class="m-top">
        <span class="src">{html.escape(m["source"])}</span>
        <span class="m-author">{html.escape(m["author"])}</span>
        <span class="m-handle">{html.escape(m["handle"])}</span>
        <span class="senti {m["sentiment"]}">{SENTI_LABEL.get(m["sentiment"],"neutral")}</span>
        {'<span class="lim">competitor</span>' if m.get("is_competitor") else ''}
        <span class="m-time">{_ago(m["created_at"])}</span>
      </div>
      <div class="m-body">{html.escape(m["text"][:280])}</div>
      <div class="m-foot">
        <span class="signal">signal {_sig_bars(m["priority"], scol[m["sentiment"]])} {m["priority"]}/10</span>
        <span class="m-sug">↳ {html.escape(m["suggestion"])}</span>
        <a class="m-act" href="{html.escape(m["url"])}" target="_blank">Open →</a>
      </div>
    </div>'''

    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return HTML_TMPL.replace("{{GEN}}", gen).replace("{{HOURS}}", str(hours)) \
        .replace("{{TOTAL}}", str(len(mentions))).replace("{{SRC}}", html.escape(src_line)) \
        .replace("{{NEG}}", str(sen_ct.get("neg", 0))).replace("{{ALERTS}}", alert_html) \
        .replace("{{CARDS}}", cards or '<div class="pv-empty">No mentions in this window.</div>')


def _ago(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
        for unit, s in (("d", 86400), ("h", 3600), ("m", 60)):
            if secs >= s:
                return f"{int(secs // s)}{unit}"
        return "now"
    except Exception:
        return ""


HTML_TMPL = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TVU Go · Listening Agent</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap');
:root{--bg:#0E1116;--panel:#161A21;--panel-2:#1B212B;--hair:#262C36;--ink:#E6E9EF;--muted:#8A94A6;--faint:#5C6577;--live:#EC3B2E;--good:#2FD07A;--warn:#FBB040;--info:#4FA3F7;--cre:#25F4EE;--disp:'Space Grotesk',sans-serif;--mono:'IBM Plex Mono',monospace;--body:'Inter',sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);font-size:14px}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 60px}
.top{display:flex;align-items:center;gap:12px;padding:18px 0;border-bottom:1px solid var(--hair)}
.logo{width:32px;height:32px;border-radius:7px;background:var(--cre);display:grid;place-items:center;font-family:var(--disp);font-weight:700;color:#062b2b;box-shadow:0 0 22px rgba(37,244,238,.4)}
.top h1{font-family:var(--disp);font-size:16px;margin:0;font-weight:600}
.top span{display:block;font-family:var(--mono);font-size:10.5px;color:var(--faint);letter-spacing:1.5px;text-transform:uppercase}
.meta{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--muted);text-align:right;line-height:1.7}
.layout{display:grid;grid-template-columns:1fr 300px;gap:18px;margin-top:20px}
.eyebrow{font-family:var(--mono);font-size:10.5px;letter-spacing:1.6px;text-transform:uppercase;color:var(--faint)}
.h2{font-family:var(--disp);font-size:17px;font-weight:600;margin:2px 0 14px}
.panel{background:var(--panel);border:1px solid var(--hair);border-radius:13px}
.mention{padding:15px 16px;margin-bottom:12px}
.m-top{display:flex;align-items:center;gap:9px;margin-bottom:8px;flex-wrap:wrap}
.src{font-family:var(--mono);font-size:10.5px;padding:3px 8px;border-radius:5px;background:var(--bg);border:1px solid var(--hair);color:var(--muted)}
.m-author{font-weight:600;font-size:13px}.m-handle{color:var(--faint);font-size:12px}
.m-time{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--faint)}
.senti{font-family:var(--mono);font-size:10px;padding:3px 8px;border-radius:20px;font-weight:600}
.senti.pos{color:var(--good);background:rgba(47,208,122,.1)}.senti.neu{color:var(--warn);background:rgba(251,176,64,.1)}.senti.neg{color:var(--live);background:rgba(236,59,46,.12)}
.lim{font-family:var(--mono);font-size:9.5px;color:var(--warn);border:1px solid rgba(251,176,64,.3);border-radius:5px;padding:2px 6px}
.m-body{font-size:13.5px;margin-bottom:11px}
.m-foot{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.signal{display:flex;align-items:center;gap:5px;font-family:var(--mono);font-size:11px;color:var(--muted)}
.sig-bars{display:flex;gap:2px;align-items:flex-end;height:13px}.sig-bars i{width:3px;border-radius:1px;display:block}
.m-act{margin-left:auto;font-size:12px;color:var(--info);font-weight:500;text-decoration:none}
.m-sug{font-family:var(--mono);font-size:11px;color:var(--faint)}
.brief{padding:18px;align-self:start;position:sticky;top:16px}
.alert{border:1px solid var(--hair);border-left:3px solid var(--live);border-radius:9px;padding:11px 12px;margin-top:10px;background:var(--bg)}
.alert.opp{border-left-color:var(--warn)}.alert.pos{border-left-color:var(--good)}
.alert .k{font-family:var(--mono);font-size:9.5px;letter-spacing:1px;text-transform:uppercase;color:var(--faint)}
.alert p{margin:4px 0 0;font-size:12.5px}
.pv-empty{color:var(--faint);text-align:center;padding:40px}
.mock{font-family:var(--mono);font-size:10px;color:var(--faint);border:1px dashed var(--hair);border-radius:6px;padding:3px 8px}
@media(max-width:840px){.layout{grid-template-columns:1fr}.brief{position:static}}
</style></head><body><div class="wrap">
<div class="top"><div class="logo">T</div>
<div><h1>Listening Agent</h1><span>TVU Go · Creator</span></div>
<div class="meta">generated {{GEN}}<br>{{TOTAL}} mentions · last {{HOURS}}h · {{NEG}} negative<br>{{SRC}}</div></div>
<div class="layout">
<div><div class="eyebrow">Live signal · open sources</div><div class="h2">Where TVU Go is showing up</div>{{CARDS}}</div>
<aside class="panel brief"><div class="eyebrow">Agent brief</div><div class="h2" style="margin-bottom:0">Needs you</div>{{ALERTS}}
<div style="margin-top:16px" class="eyebrow">Coverage</div>
<p style="font-size:12px;color:var(--muted)">Open sources only. Twitch/Kick clips, TikTok, and Discord are blind spots — add keyed collectors (YouTube Data API, Twitch Helix) to close them.</p>
</aside></div></div></body></html>"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="TVU Go social listening agent")
    ap.add_argument("--hours", type=int, default=72)
    ap.add_argument("--per-source", type=int, default=15)
    ap.add_argument("--out", default="./tvu_out")
    ap.add_argument("--competitors", action="store_true",
                    help="also track competitor share-of-voice")
    ap.add_argument("--no-llm", action="store_true",
                    help="force heuristic scoring even if a key is set")
    ap.add_argument("--model", default=CLAUDE_MODEL)
    args = ap.parse_args()

    queries = list(BRAND_QUERIES)
    if args.competitors:
        queries += COMPETITOR_QUERIES

    print(f"TVU Go listening agent — last {args.hours}h")
    mentions = collect(queries, args.per_source, args.hours)
    print(f"· collected {len(mentions)} unique mentions")

    use_llm = (not args.no_llm) and os.getenv("ANTHROPIC_API_KEY")
    if use_llm:
        print(f"· triaging with Claude ({args.model})")
        triage_claude(mentions, args.model)
    else:
        print("· triaging with heuristic (set ANTHROPIC_API_KEY for Claude scoring)")
        triage_heuristic(mentions)

    mentions = [m for m in mentions if m.get("relevant", True)]
    print(f"· {len(mentions)} relevant after triage")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "mentions.json"), "w") as f:
        json.dump(mentions, f, indent=2)
    with open(os.path.join(args.out, "dashboard.html"), "w") as f:
        f.write(render_html(mentions, args.hours))

    # console digest
    hot = sorted(mentions, key=lambda m: -m["priority"])[:8]
    print("\nTOP SIGNALS")
    for m in hot:
        print(f"  [{m['priority']}/10] {m['sentiment']:>3} {m['category']:<11} "
              f"{m['source']:<12} {m['text'][:70]}")
    print(f"\nWrote {args.out}/dashboard.html and mentions.json")


if __name__ == "__main__":
    main()
