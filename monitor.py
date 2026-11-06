"""
Instagram influencer monitor.

Finds every creator who made reels or posts with an Instagram account
(collab reels + tagged reels/posts), compares the result with the last
snapshot, emails a change report and saves the new snapshot.

Usage:
    python monitor.py --login      # first time: log in to Instagram in a browser window
    python monitor.py              # run a check (this is what the daily schedule runs)
    python monitor.py --no-email   # run a check, only save the report to reports/
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / "browser-profile"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
SNAPSHOT = DATA_DIR / "influencers_reels_posts.json"
OWNERS_FILE = DATA_DIR / "reel_owners.json"     # reel id -> who posted it (never changes, so cached)
PRODUCTS_FILE = DATA_DIR / "product_influencers.json"   # who featured which product
HISTORY_FILE = DATA_DIR / "history.json"                 # what changed in each daily scan (for the dashboard)
SESSION_FILE = ROOT / "instagram-session.json"   # login cookies, for copying the login to a server


# ---------------------------------------------------------------- config

def load_env():
    """Read KEY=VALUE lines from .env into os.environ (no extra package needed)."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg(key, default=None):
    return os.environ.get(key, default)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- parsing

OG_DESC = re.compile(r'property="og:description" content="([^"]*)"')
OWNER = re.compile(r"(?:^|- )([A-Za-z0-9._]+) on ([A-Z][a-z]+ \d{1,2}, \d{4})")
FOLLOWERS = re.compile(r"^([\d.,]+[KM]?) Followers", re.I)


def og_description(page_html):
    m = OG_DESC.search(page_html or "")
    return html.unescape(m.group(1)) if m else ""


def parse_owner(desc):
    """'27 likes, 0 comments - user on November 3, 2026: "..."' -> ('user', '2026-11-03')"""
    m = OWNER.search(desc)
    if not m:
        return None, None
    try:
        date = datetime.strptime(m.group(2), "%B %d, %Y").date().isoformat()
    except ValueError:
        date = None
    return m.group(1), date


def parse_caption(desc):
    """'5 likes, 0 comments - user on November 3, 2026: "Caption text"' -> 'Caption text'"""
    m = re.search(r'on [A-Z][a-z]+ \d{1,2}, \d{4}: (.*)$', desc, re.S)
    text = m.group(1) if m else desc
    return text.strip().strip('".').strip()


OG_TITLE = re.compile(r'property="og:title" content="([^"]*)"')
SHORTCODE_ABC = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def post_author_name(page_html):
    """og:title of a post page: 'Seño Alba 🌱 on Instagram: "..."' -> 'Seño Alba 🌱'"""
    m = OG_TITLE.search(page_html or "")
    if not m:
        return ""
    title = html.unescape(m.group(1))
    return title.split(" on Instagram")[0].strip() if " on Instagram" in title else ""


def shortcode_age(sc):
    """Instagram post codes count up over time: a bigger number = a newer post."""
    n = 0
    for ch in sc[:11]:
        n = n * 64 + max(0, SHORTCODE_ABC.find(ch))
    return n


def parse_name(desc):
    """'3,020 Followers, ... - See Instagram photos and videos from Asun SanBeni (@asunsanbeni)' -> 'Asun SanBeni'"""
    m = re.search(r"from (.+) \(@[A-Za-z0-9._]+\)", desc)
    return m.group(1).strip() if m else ""


def parse_count(text):
    """'17K' -> 17000, '1.2M' -> 1200000, '3,020' -> 3020"""
    s = text.replace(",", "").strip().upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    return int(round(float(s) * mult))


def parse_followers(desc):
    m = FOLLOWERS.search(desc)
    return parse_count(m.group(1)) if m else None


def split_href(href):
    """'/user/reel/ID/' -> ('user','reel','ID'); '/reel/ID/' -> (None,'reel','ID')"""
    parts = [p for p in href.split("/") if p]
    if len(parts) >= 3 and parts[1] in ("reel", "p"):
        return parts[0], parts[1], parts[2]
    if len(parts) >= 2 and parts[0] in ("reel", "p"):
        return None, parts[0], parts[1]
    return None, None, None


# ---------------------------------------------------------------- browser

class RateLimited(Exception):
    pass


class NotLoggedIn(Exception):
    pass


def open_browser(p, headless, offscreen=False):
    """Use the computer's own Google Chrome when available (Instagram trusts it more),
    otherwise Playwright's built-in Chromium. Set BROWSER=chromium in .env to force the latter."""
    PROFILE_DIR.mkdir(exist_ok=True)
    opts = dict(headless=headless, viewport={"width": 1280, "height": 900},
                locale="en-US", args=["--lang=en-US", "--disable-blink-features=AutomationControlled"])
    if offscreen and not headless:
        # A normal Chrome window, placed far outside the screen so nobody sees it.
        opts["args"] += ["--window-position=-32000,-32000", "--window-size=1280,900"]
    def clear_locks(folder):
        # A run that was stopped (Ctrl+C, crash, restart) leaves "profile in use" lock files behind,
        # which make the next start fail. Only one check runs at a time, so it is safe to remove them.
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (folder / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log(f"Could not remove old browser lock {name}: {e}")

    def launch(extra):
        o = dict(opts, **extra)
        clear_locks(PROFILE_DIR)
        clear_locks(PROFILE_DIR / "chromium")
        if cfg("BROWSER", "chrome").lower() == "chrome":
            try:
                return p.chromium.launch_persistent_context(str(PROFILE_DIR), channel="chrome", **o)
            except Exception as e:
                log(f"Google Chrome not available ({str(e).splitlines()[0]}), using built-in Chromium")
        return p.chromium.launch_persistent_context(str(PROFILE_DIR / "chromium"), **o)

    ctx = launch({})
    if headless:
        # Hidden Chrome announces itself as "HeadlessChrome", which Instagram treats as logged out.
        # Relaunch with the normal Chrome user agent.
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        ua = pg.evaluate("navigator.userAgent")
        if "Headless" in ua:
            ctx.close()
            ctx = launch({"user_agent": ua.replace("HeadlessChrome", "Chrome")})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def fetch(page, path, tries=2):
    """Fetch an Instagram page from inside the logged-in browser. Returns HTML.
    On a rate limit (429) it waits once, then gives up; the caller leaves the rest for the next run."""
    for attempt in range(tries):
        status, text = page.evaluate(
            """async (p) => { const r = await fetch(p, {credentials: 'include'});
                              return [r.status, r.status === 200 ? await r.text() : '']; }""",
            path,
        )
        if status == 200:
            return text
        if status == 429:
            if attempt == tries - 1:
                break
            wait = int(cfg("RATE_LIMIT_WAIT_SECONDS", "180"))
            log(f"Instagram rate limit (429) on {path}, waiting {wait}s")
            time.sleep(wait)
            continue
        return ""
    raise RateLimited(path)


def collect_links(page, url, kinds=("reel", "p"), idle_rounds=8):
    """Open a grid page and scroll until no new links appear."""
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if "/accounts/login" in page.url:
        raise NotLoggedIn()
    sel = ", ".join(f'main a[href*="/{k}/"]' for k in kinds)
    seen, idle = [], 0
    page.mouse.move(640, 500)
    while idle < idle_rounds:
        hrefs = page.eval_on_selector_all(sel, "els => els.map(e => e.getAttribute('href'))")
        before = len(seen)
        for h in hrefs:
            if h and h not in seen:
                seen.append(h)
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(2000)
        idle = idle + 1 if len(seen) == before else 0
    return seen


def scrape(page, account, delay, owners):
    """Return ({username: {'reels': {id: date}, 'posts': {id: date}}}, notes).
    owners: cache {reel id: {"u": username, "d": date}}, updated in place."""
    people, notes = {}, []

    def add(user, kind, sid, date=None):
        if not user or user.lower() == account.lower():
            return
        entry = people.setdefault(user, {"reels": {}, "posts": {}})
        bucket = entry["reels" if kind == "reel" else "posts"]
        if sid not in bucket or (date and not bucket[sid]):
            bucket[sid] = date

    # 1. Reels tab: collab reels. The grid links don't show the owner, so open each one.
    log("Collecting the Reels tab")
    reel_links = collect_links(page, f"https://www.instagram.com/{account}/reels/", kinds=("reel",))
    ids = [sid for _, _, sid in map(split_href, reel_links) if sid]
    new_ids = [sid for sid in ids if sid not in owners]
    log(f"  {len(ids)} reels found, {len(new_ids)} new ones to check (the rest are already known)")
    blocked = 0
    for sid in ids:
        if sid in owners:
            add(owners[sid]["u"], "reel", sid, owners[sid].get("d"))
            continue
        if blocked:
            blocked += 1
            continue
        try:
            page_html = fetch(page, f"/reel/{sid}/")
        except RateLimited:
            blocked = 1
            continue
        desc = og_description(page_html)
        owner, date = parse_owner(desc)
        if owner:
            owners[sid] = {"u": owner, "d": date, "c": parse_caption(desc), "n": post_author_name(page_html)}
            add(owner, "reel", sid, date)
        time.sleep(delay)
    if blocked:
        notes.append(f"Instagram blocked requests, so {blocked} new reels could not be checked yet. "
                     "They will be checked on the next run.")

    # 2. Tagged tab: owner is in the link itself.
    log("Collecting the Tagged tab")
    tagged = collect_links(page, f"https://www.instagram.com/{account}/tagged/")
    log(f"  {len(tagged)} tagged reels/posts found")
    for href in tagged:
        user, kind, sid = split_href(href)
        if sid:
            add(user, kind, sid)

    return people, notes


def follower_counts(page, users, delay, notes, old):
    """Refresh follower counts for at most FOLLOWER_CHECKS_PER_RUN accounts per run:
    new creators first, then those checked longest ago. The rest keep their last known count."""
    limit = int(cfg("FOLLOWER_CHECKS_PER_RUN", "50"))
    order = sorted(users, key=lambda u: (u in old, old.get(u, {}).get("followers_checked", ""), u))
    todo = order[:limit]
    counts, names = {}, {}
    log(f"Checking follower counts for {len(todo)} of {len(users)} accounts (rotating, oldest first)")
    for i, u in enumerate(todo, 1):
        try:
            desc = og_description(fetch(page, f"/{u}/"))
            n = parse_followers(desc)
            if parse_name(desc):
                names[u] = parse_name(desc)
        except RateLimited:
            notes.append(f"Instagram blocked follower lookups after {i - 1} of {len(todo)} accounts; "
                         "the rest keep their last known numbers.")
            break
        if n is not None:
            counts[u] = n
        time.sleep(delay)
    if len(users) > limit:
        notes.append(f"Follower counts were refreshed for {len(counts)} of {len(users)} creators today; "
                     f"every creator is refreshed in turn, about every {-(-len(users) // limit)} days.")
    return counts, names


# ---------------------------------------------------------------- snapshot + diff

def load_snapshot():
    if not SNAPSHOT.exists():
        return {}
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return {e["username"]: e for e in data}


def load_owners(old):
    """Known reel owners: from the cache file, plus every reel id already in the snapshot."""
    owners = {}
    if OWNERS_FILE.exists():
        owners = json.loads(OWNERS_FILE.read_text(encoding="utf-8"))
    for u, e in old.items():
        for sid in e.get("reel_ids", []):
            owners.setdefault(sid, {"u": u})
    return owners


def build_snapshot(people, counts, old, names=None):
    out = []
    for u, e in people.items():
        followers = counts.get(u)
        checked = datetime.now().strftime("%Y-%m-%d") if followers is not None else old.get(u, {}).get("followers_checked", "")
        if followers is None and u in old:
            followers = old[u].get("followers")
        reel_ids = sorted(e["reels"])
        post_ids = sorted(e["posts"])
        out.append({
            "username": u,
            "followers": followers,
            "reels": len(reel_ids),
            "posts": len(post_ids),
            "total": len(reel_ids) + len(post_ids),
            "reel_ids": reel_ids,
            "post_ids": post_ids,
            "followers_checked": checked,
            "name": (names or {}).get(u) or old.get(u, {}).get("name", ""),
            "first_seen": old.get(u, {}).get("first_seen") or (datetime.now().strftime("%Y-%m-%d") if old else ""),
        })
    out.sort(key=lambda o: (-o["total"], -(o["followers"] or 0), o["username"]))
    return out


def diff(old, new_list):
    new = {e["username"]: e for e in new_list}
    changes = {"new_people": [], "new_content": [], "followers": [], "gone": []}

    for u, e in new.items():
        if u not in old:
            changes["new_people"].append(e)
            continue
        o = old[u]
        if "reel_ids" in o:
            nr = sorted(set(e["reel_ids"]) - set(o.get("reel_ids", [])))
            np_ = sorted(set(e["post_ids"]) - set(o.get("post_ids", [])))
            n_r, n_p = len(nr), len(np_)
        else:  # older snapshot without ids: compare counts only (no links)
            nr, np_ = [], []
            n_r = max(0, e["reels"] - o.get("reels", 0))
            n_p = max(0, e["posts"] - o.get("posts", 0))
        if n_r or n_p:
            changes["new_content"].append({"username": u, "followers": e["followers"],
                                           "reel_ids": nr, "post_ids": np_,
                                           "n_reels": n_r, "n_posts": n_p})
        of, nf = o.get("followers"), e.get("followers")
        if of and nf and of != nf:
            if abs(nf - of) >= 1000 or abs(nf - of) / of >= 0.10:
                changes["followers"].append({"username": u, "old": of, "new": nf})

    for u, o in old.items():
        if u not in new:
            changes["gone"].append(o)

    changes["followers"].sort(key=lambda c: -abs(c["new"] - c["old"]))
    return changes


# ---------------------------------------------------------------- products

def load_products():
    """PRODUCTS in .env: products separated by ';', other spellings of the same product by '|'.
    Example: PRODUCTS=Dragona; Girasol|Gira-sol; Ma ma ma mamut"""
    out = {}
    for part in (cfg("PRODUCTS", "") or "").split(";"):
        variants = [v.strip() for v in part.split("|") if v.strip()]
        if variants:
            out[variants[0]] = variants
    return out


def plain(text):
    """Lower case without accents, so 'Dragón' matches 'dragon'."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", text or "").lower() if unicodedata.category(c) != "Mn")


def fetch_captions(page, people, owners, old, delay, notes):
    """Make sure every reel and post has its caption in the cache (fetched once, then reused).
    New content first; at most CAPTION_CHECKS_PER_RUN pages per run, the rest on later runs."""
    limit = int(cfg("CAPTION_CHECKS_PER_RUN", "100"))
    known_ids = {i for e in old.values() for i in e.get("reel_ids", []) + e.get("post_ids", [])}
    todo = []
    for u, e in people.items():
        for kind, bucket in (("reel", e["reels"]), ("p", e["posts"])):
            for sid in bucket:
                if "c" not in owners.get(sid, {}):
                    todo.append((sid in known_ids, -shortcode_age(sid), kind, sid, u))
    todo.sort()  # content new since yesterday first, then newest posts first
    if not todo:
        return
    log(f"Reading captions: {min(len(todo), limit)} of {len(todo)} posts without a saved caption")
    for n, (_, _, kind, sid, u) in enumerate(todo[:limit], 1):
        try:
            page_html = fetch(page, f"/{kind}/{sid}/")
            desc = og_description(page_html)
        except RateLimited:
            notes.append(f"Instagram blocked caption checks after {n - 1} posts; "
                         f"{len(todo) - n + 1} captions will be read on later runs.")
            return
        entry = owners.setdefault(sid, {"u": u})
        entry["c"] = parse_caption(desc)
        if post_author_name(page_html):
            entry["n"] = post_author_name(page_html)
        time.sleep(delay)
    if len(todo) > limit:
        notes.append(f"{len(todo) - limit} older posts have no saved caption yet; they are read on later runs, "
                     f"{limit} per run.")


def match_products(people, owners, new_list, products, today):
    """Build {product: [influencer, ...]} from the saved captions, keeping each first-seen date."""
    previous = {}
    if PRODUCTS_FILE.exists():
        try:
            previous = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8")).get("products", {})
        except (ValueError, OSError):
            previous = {}
    snap = {e["username"]: e for e in new_list}
    result, new_people = {}, {}
    for product, variants in products.items():
        keys = [plain(v) for v in variants]
        seen_before = {i["username"]: i for i in previous.get(product, [])}
        rows = []
        for u, e in people.items():
            links = []
            for kind, bucket in (("reel", e["reels"]), ("p", e["posts"])):
                for sid in bucket:
                    cap = plain(owners.get(sid, {}).get("c", ""))
                    if cap and any(k in cap for k in keys):
                        links.append(f"https://www.instagram.com/{kind}/{sid}/")
            if not links:
                continue
            row = {
                "username": u,
                "name": snap.get(u, {}).get("name", ""),
                "followers": snap.get(u, {}).get("followers"),
                "first_seen": seen_before.get(u, {}).get("first_seen", today),
                "posts": sorted(links),
            }
            rows.append(row)
            if u not in seen_before:
                new_people.setdefault(product, []).append(row)
        rows.sort(key=lambda r: (-(r["followers"] or 0), r["username"]))
        result[product] = rows
    return result, new_people


def save_history(today, ch, new_list, product_rows, product_new, notes):
    """Append today's changes to data/history.json (newest first, last 180 scans)."""
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            history = []
    history = [h for h in history if h.get("date") != today]
    history.insert(0, {
        "date": today,
        "summary": summary_line(ch),
        "totals": {"influencers": len(new_list), "reels": sum(e["reels"] for e in new_list),
                   "posts": sum(e["posts"] for e in new_list)},
        "new_influencers": [{"username": e["username"], "followers": e["followers"]} for e in ch["new_people"]],
        "new_content": [{"username": c["username"], "reels": c["reel_ids"], "posts": c["post_ids"],
                         "n_reels": c["n_reels"], "n_posts": c["n_posts"]} for c in ch["new_content"]],
        "follower_changes": ch["followers"],
        "gone": [e["username"] for e in ch["gone"]],
        "product_new": {p: [{"username": r["username"], "followers": r["followers"]} for r in rows]
                        for p, rows in (product_new or {}).items()},
        "product_totals": {p: len(rows) for p, rows in (product_rows or {}).items()},
        "notes": notes,
    })
    HISTORY_FILE.write_text(json.dumps(history[:180], ensure_ascii=False, indent=1), encoding="utf-8")


def save_products(result, today):
    PRODUCTS_FILE.write_text(json.dumps({"updated": today, "products": result}, ensure_ascii=False, indent=2),
                             encoding="utf-8")


# ---------------------------------------------------------------- report

def fmt(n):
    return "?" if n is None else f"{n:,}"


def prof(u):
    return f"https://www.instagram.com/{u}/"


def summary_line(ch):
    nr = sum(c["n_reels"] for c in ch["new_content"]) + sum(e["reels"] for e in ch["new_people"])
    np_ = sum(c["n_posts"] for c in ch["new_content"]) + sum(e["posts"] for e in ch["new_people"])
    parts = [
        f"{len(ch['new_people'])} new influencers",
        f"{nr} new reels",
        f"{np_} new posts",
        f"{len(ch['followers'])} follower changes",
        f"{len(ch['gone'])} no longer showing",
    ]
    return ", ".join(parts)


def render(account, ch, new_list, notes, today, product_rows=None, product_new=None):
    links = lambda ids, kind: " ".join(
        f'<a href="https://www.instagram.com/{kind}/{i}/">{kind}/{i}</a>' for i in ids)
    total_r = sum(e["reels"] for e in new_list)
    total_p = sum(e["posts"] for e in new_list)
    nothing = not any(ch.values())

    H = ['<div style="font-family:Arial,sans-serif;font-size:14px;color:#1f2a22;max-width:760px">',
         f'<h2 style="margin:0 0 4px">@{account} influencer report</h2>',
         f'<p style="margin:0 0 16px;color:#5f6b62">{today} &middot; '
         f'{len(new_list)} influencers, {total_r} reels, {total_p} posts in total</p>',
         f'<p><b>{"No changes since the last check." if nothing else summary_line(ch)}</b></p>']
    T = [f"@{account} influencer report - {today}",
         f"{len(new_list)} influencers, {total_r} reels, {total_p} posts in total", "",
         "No changes since the last check." if nothing else summary_line(ch), ""]

    th = 'style="text-align:left;padding:6px 10px;border-bottom:1px solid #dde4dc"'
    td = 'style="padding:6px 10px;border-bottom:1px solid #eef2ed;vertical-align:top"'

    def table(title, head, rows):
        if not rows:
            return
        H.append(f'<h3 style="margin:20px 0 6px">{title}</h3><table style="border-collapse:collapse">')
        H.append("<tr>" + "".join(f"<th {th}>{h}</th>" for h in head) + "</tr>")
        for r in rows:
            H.append("<tr>" + "".join(f"<td {td}>{c}</td>" for c in r[0]) + "</tr>")
        T.append(title.upper())
        T.extend("  " + r[1] for r in rows)
        T.append("")

    table("New influencers", ["Account", "Followers", "Reels", "Posts", "Links"], [
        ([f'<a href="{prof(e["username"])}">@{e["username"]}</a>', fmt(e["followers"]), e["reels"], e["posts"],
          links(e["reel_ids"], "reel") + " " + links(e["post_ids"], "p")],
         f'@{e["username"]} ({fmt(e["followers"])} followers): {e["reels"]} reels, {e["posts"]} posts')
        for e in ch["new_people"]])

    def content_cell(c):
        if c["reel_ids"] or c["post_ids"]:
            return links(c["reel_ids"], "reel") + " " + links(c["post_ids"], "p")
        return f'{c["n_reels"]} new reels, {c["n_posts"]} new posts'

    def content_text(c):
        urls = ([f"https://www.instagram.com/reel/{i}/" for i in c["reel_ids"]] +
                [f"https://www.instagram.com/p/{i}/" for i in c["post_ids"]])
        return " ".join(urls) if urls else f'{c["n_reels"]} new reels, {c["n_posts"]} new posts'

    table("New reels and posts", ["Account", "Followers", "New content"], [
        ([f'<a href="{prof(c["username"])}">@{c["username"]}</a>', fmt(c["followers"]), content_cell(c)],
         f'@{c["username"]}: {content_text(c)}')
        for c in ch["new_content"]])

    table("Follower changes", ["Account", "Before", "Now", "Change"], [
        ([f'<a href="{prof(c["username"])}">@{c["username"]}</a>', fmt(c["old"]), fmt(c["new"]),
          f'{c["new"] - c["old"]:+,} ({(c["new"] - c["old"]) / c["old"]:+.0%})'],
         f'@{c["username"]}: {fmt(c["old"])} -> {fmt(c["new"])}')
        for c in ch["followers"]])

    table("No longer showing", ["Account", "Followers", "Reels", "Posts"], [
        ([f'<a href="{prof(e["username"])}">@{e["username"]}</a>', fmt(e.get("followers")),
          e.get("reels", 0), e.get("posts", 0)],
         f'@{e["username"]} (content deleted, tag removed or account made private)')
        for e in ch["gone"]])

    for product, rows in (product_rows or {}).items():
        fresh = (product_new or {}).get(product, [])
        table(f"New influencers featuring {html.escape(product)} ({len(fresh)} new, {len(rows)} in total)",
              ["Account", "Name", "Followers", "Posts"], [
            ([f'<a href="{prof(r["username"])}">@{r["username"]}</a>', html.escape(r["name"] or ""),
              fmt(r["followers"]), " ".join(f'<a href="{l}">{l.split("instagram.com/")[1].rstrip("/")}</a>'
                                            for l in r["posts"])],
             f'@{r["username"]} ({fmt(r["followers"])} followers): ' + " ".join(r["posts"]))
            for r in fresh])
        if not fresh:
            H.append(f'<p style="color:#5f6b62">{html.escape(product)}: no new influencers today '
                     f'({len(rows)} in total, see data/product_influencers.json).</p>')
            T.append(f"{product}: no new influencers today ({len(rows)} in total).")

    if notes:
        H.append('<h3 style="margin:20px 0 6px">Notes</h3><ul>' +
                 "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>")
        T.append("NOTES")
        T.extend("  - " + n for n in notes)

    H.append('<p style="color:#5f6b62;font-size:12px;margin-top:24px">Instagram rounds follower counts '
             'above 10K, so small changes for large accounts may not show.</p></div>')
    return "\n".join(H), "\n".join(T)


def send_email(subject, html_body, text_body):
    host, user, pw = cfg("SMTP_HOST"), cfg("SMTP_USER"), cfg("SMTP_PASSWORD")
    to = [a.strip() for a in (cfg("EMAIL_TO") or "").split(",") if a.strip()]
    if not (host and user and pw and to):
        log("Email not configured in .env, skipping email")
        return False
    port = int(cfg("SMTP_PORT", "587"))
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg("EMAIL_FROM", user)
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=60)
        else:
            server = smtplib.SMTP(host, port, timeout=60)
            server.starttls()
        with server:
            server.login(user, pw.replace(" ", ""))
            server.sendmail(msg["From"], to, msg.as_string())
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as e:
        log(f"Email login failed ({type(e).__name__}). The email server did not accept "
            f"SMTP_USER / SMTP_PASSWORD in .env. For Gmail, SMTP_USER must be the full address "
            f"and SMTP_PASSWORD a 16-letter App password (https://myaccount.google.com/apppasswords), "
            f"not your normal password. If that is correct, try SMTP_PORT=465.")
        return False
    except (OSError, smtplib.SMTPException) as e:
        log(f"Could not send email via {host}:{port}: {e}. Check SMTP_HOST and SMTP_PORT in .env, "
            f"and whether antivirus or a firewall blocks outgoing email.")
        return False
    log(f"Email sent to {', '.join(to)}")
    return True


# ---------------------------------------------------------------- session file

def ig_cookies(ctx):
    return [c for c in ctx.cookies() if "instagram.com" in c.get("domain", "")]


def has_session(ctx):
    return any(c["name"] == "sessionid" for c in ig_cookies(ctx))


def save_session(ctx):
    SESSION_FILE.write_text(json.dumps(ig_cookies(ctx), indent=2), encoding="utf-8")


def load_session(ctx):
    """On a server: put the login copied from another computer into the browser."""
    if has_session(ctx) or not SESSION_FILE.exists():
        return
    try:
        ctx.add_cookies(json.loads(SESSION_FILE.read_text(encoding="utf-8")))
        log(f"Loaded Instagram login from {SESSION_FILE.name}")
    except Exception as e:
        log(f"Could not read {SESSION_FILE.name}: {e}")


# ---------------------------------------------------------------- main

def do_login():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = open_browser(p, headless=False)
        page.goto("https://www.instagram.com/accounts/login/")
        print("\nLog in to Instagram in the browser window that just opened.")
        print("If Instagram asks for a code: check the app on your phone for 'Was this you?',")
        print("or use 'Try another way' to get the code by SMS. Take your time.")
        input("When you can see your Instagram feed, come back here and press Enter... ")
        logged_in = has_session(ctx)
        if logged_in:
            save_session(ctx)
        ctx.close()
    if logged_in:
        print("Login saved. You can now run: python monitor.py")
        print(f"To run the monitor on a server, copy {SESSION_FILE.name} to the server's project folder.")
    else:
        print("It looks like the login did not finish. Run python monitor.py --login again.")


def run(send=True):
    from playwright.sync_api import sync_playwright
    account = cfg("INSTAGRAM_ACCOUNT", "editorialcuentosconvalores").lstrip("@")
    delay = float(cfg("REQUEST_DELAY_SECONDS", "1.0"))
    headless = cfg("HEADLESS", "false").lower() == "true"
    offscreen = cfg("HIDE_WINDOW", "true").lower() != "false"
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"@{account} influencer report {today}"
    DATA_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)

    old = load_snapshot()
    with sync_playwright() as p:
        ctx, page = open_browser(p, headless, offscreen=offscreen)
        try:
            load_session(ctx)
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            if not has_session(ctx) or "/accounts/login" in page.url:
                raise NotLoggedIn()
            owners = load_owners(old)
            people, notes = scrape(page, account, delay, owners)
            OWNERS_FILE.write_text(json.dumps(owners, indent=0), encoding="utf-8")
            counts, names = follower_counts(page, sorted(people), delay, notes, old)
            products = load_products()
            if products:
                fetch_captions(page, people, owners, old, delay, notes)
                OWNERS_FILE.write_text(json.dumps(owners, indent=0), encoding="utf-8")
            if SESSION_FILE.exists():
                save_session(ctx)  # keep the copied login fresh
        except NotLoggedIn:
            log("Not logged in to Instagram")
            msg = ("The influencer check could not run because the Instagram login has expired. "
                   "On a computer with a screen, run: python monitor.py --login and log in. "
                   f"If the monitor runs on a server, then copy the new {SESSION_FILE.name} to the server.")
            if send:
                send_email(f"{subject}: Instagram login needed", f"<p>{msg}</p>", msg)
            return 2
        finally:
            ctx.close()

    for sid, info in owners.items():
        if info.get("n") and info.get("u") in people and not names.get(info["u"]):
            names[info["u"]] = info["n"]
    new_list = build_snapshot(people, counts, old, names)
    product_rows, product_new = ({}, {})
    if products:
        product_rows, product_new = match_products(people, owners, new_list, products, today)
    incomplete = old and len(new_list) < 0.7 * len(old)
    if incomplete:
        notes.append(f"Only {len(new_list)} influencers were found (last time {len(old)}). "
                     "That looks like an incomplete check, so the saved snapshot was not replaced.")
    ch = diff(old, new_list) if old else {"new_people": [], "new_content": [], "followers": [], "gone": []}
    if not old:
        notes.append("This was the first check, so everything found is saved as the starting point.")

    html_body, text_body = render(account, ch, new_list, notes, today, product_rows, product_new)
    (REPORTS_DIR / f"report-{today}.html").write_text(html_body, encoding="utf-8")
    log(summary_line(ch))

    if send:
        send_email(subject, html_body, text_body)
    if not incomplete:
        SNAPSHOT.write_text(json.dumps(new_list, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Snapshot saved: {len(new_list)} influencers")
        if products:
            save_products(product_rows, today)
            log("Product list saved: " + ", ".join(f"{k} {len(v)}" for k, v in product_rows.items()))
        save_history(today, ch, new_list, product_rows, product_new, notes)
    return 0


def daemon():
    """Stay running and do one check every day at RUN_TIME (used on servers / Docker)."""
    from datetime import timedelta
    hh, mm = (int(x) for x in cfg("RUN_TIME", "13:00").split(":"))
    log(f"Monitor started, daily check at {hh:02d}:{mm:02d}")
    while True:
        now = datetime.now()
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        log(f"Next check: {nxt:%Y-%m-%d %H:%M}")
        time.sleep(max(1, (nxt - datetime.now()).total_seconds()))
        try:
            run(send=True)
        except Exception as e:  # never let one bad day stop the daily schedule
            log(f"Check failed: {e!r}")
            send_email("Influencer monitor: check failed",
                       f"<p>Today's check stopped with an error:</p><pre>{html.escape(repr(e))}</pre>",
                       f"Today's check stopped with an error: {e!r}")


def main():
    load_env()
    ap = argparse.ArgumentParser(description="Instagram influencer monitor")
    ap.add_argument("--login", action="store_true", help="open a browser window to log in to Instagram")
    ap.add_argument("--no-email", action="store_true", help="save the report without emailing it")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    ap.add_argument("--daemon", action="store_true", help="keep running and check every day at RUN_TIME")
    a = ap.parse_args()
    if a.daemon:
        daemon()
        return 0
    if a.login:
        do_login()
        return 0
    if a.test_email:
        ok = send_email("Influencer monitor test email",
                        "<p>Your influencer monitor can send email.</p>",
                        "Your influencer monitor can send email.")
        return 0 if ok else 1
    return run(send=not a.no_email)


if __name__ == "__main__":
    sys.exit(main())
