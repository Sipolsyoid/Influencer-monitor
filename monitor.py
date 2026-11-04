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


def open_browser(p, headless):
    PROFILE_DIR.mkdir(exist_ok=True)
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        args=["--lang=en-US"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def fetch(page, path, tries=4):
    """Fetch an Instagram page from inside the logged-in browser. Returns HTML."""
    for attempt in range(tries):
        status, text = page.evaluate(
            """async (p) => { const r = await fetch(p, {credentials: 'include'});
                              return [r.status, r.status === 200 ? await r.text() : '']; }""",
            path,
        )
        if status == 200:
            return text
        if status == 429:
            wait = 120 * (attempt + 1)
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


def scrape(page, account, delay):
    """Return ({username: {'reels': {id: date}, 'posts': {id: date}}}, notes)."""
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
    log(f"  {len(reel_links)} reels found, checking who posted each")
    for i, href in enumerate(reel_links, 1):
        _, _, sid = split_href(href)
        if not sid:
            continue
        try:
            owner, date = parse_owner(og_description(fetch(page, f"/reel/{sid}/")))
        except RateLimited:
            notes.append(f"Instagram blocked requests after {i - 1} of {len(reel_links)} reels; "
                         "the rest were not checked.")
            break
        add(owner, "reel", sid, date)
        time.sleep(delay)

    # 2. Tagged tab: owner is in the link itself.
    log("Collecting the Tagged tab")
    tagged = collect_links(page, f"https://www.instagram.com/{account}/tagged/")
    log(f"  {len(tagged)} tagged reels/posts found")
    for href in tagged:
        user, kind, sid = split_href(href)
        if sid:
            add(user, kind, sid)

    return people, notes


def follower_counts(page, users, delay, notes):
    counts = {}
    log(f"Checking follower counts for {len(users)} accounts")
    for i, u in enumerate(users, 1):
        try:
            counts[u] = parse_followers(og_description(fetch(page, f"/{u}/")))
        except RateLimited:
            notes.append(f"Instagram blocked follower lookups after {i - 1} of {len(users)} accounts; "
                         "the rest keep yesterday's numbers.")
            break
        time.sleep(delay)
    return counts


# ---------------------------------------------------------------- snapshot + diff

def load_snapshot():
    if not SNAPSHOT.exists():
        return {}
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return {e["username"]: e for e in data}


def build_snapshot(people, counts, old):
    out = []
    for u, e in people.items():
        followers = counts.get(u)
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


def render(account, ch, new_list, notes, today):
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
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
        server.starttls()
    with server:
        server.login(user, pw)
        server.sendmail(msg["From"], to, msg.as_string())
    log(f"Email sent to {', '.join(to)}")
    return True


# ---------------------------------------------------------------- main

def do_login():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx, page = open_browser(p, headless=False)
        page.goto("https://www.instagram.com/accounts/login/")
        print("\nLog in to Instagram in the browser window that just opened.")
        input("When you can see your Instagram feed, come back here and press Enter... ")
        ctx.close()
    print("Login saved in the browser-profile folder. You can now run: python monitor.py")


def run(send=True):
    from playwright.sync_api import sync_playwright
    account = cfg("INSTAGRAM_ACCOUNT", "editorialcuentosconvalores").lstrip("@")
    delay = float(cfg("REQUEST_DELAY_SECONDS", "1.0"))
    headless = cfg("HEADLESS", "true").lower() != "false"
    today = datetime.now().strftime("%Y-%m-%d")
    subject = f"@{account} influencer report {today}"
    DATA_DIR.mkdir(exist_ok=True)
    REPORTS_DIR.mkdir(exist_ok=True)

    old = load_snapshot()
    with sync_playwright() as p:
        ctx, page = open_browser(p, headless)
        try:
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            people, notes = scrape(page, account, delay)
            counts = follower_counts(page, sorted(people), delay, notes)
        except NotLoggedIn:
            log("Not logged in to Instagram")
            msg = ("The influencer check could not run because the Instagram login has expired. "
                   "Run setup again (or: python monitor.py --login) and log in.")
            if send:
                send_email(f"{subject}: Instagram login needed", f"<p>{msg}</p>", msg)
            return 2
        finally:
            ctx.close()

    new_list = build_snapshot(people, counts, old)
    incomplete = old and len(new_list) < 0.7 * len(old)
    if incomplete:
        notes.append(f"Only {len(new_list)} influencers were found (last time {len(old)}). "
                     "That looks like an incomplete check, so the saved snapshot was not replaced.")
    ch = diff(old, new_list) if old else {"new_people": [], "new_content": [], "followers": [], "gone": []}
    if not old:
        notes.append("This was the first check, so everything found is saved as the starting point.")

    html_body, text_body = render(account, ch, new_list, notes, today)
    (REPORTS_DIR / f"report-{today}.html").write_text(html_body, encoding="utf-8")
    log(summary_line(ch))

    if send:
        send_email(subject, html_body, text_body)
    if not incomplete:
        SNAPSHOT.write_text(json.dumps(new_list, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Snapshot saved: {len(new_list)} influencers")
    return 0


def main():
    load_env()
    ap = argparse.ArgumentParser(description="Instagram influencer monitor")
    ap.add_argument("--login", action="store_true", help="open a browser window to log in to Instagram")
    ap.add_argument("--no-email", action="store_true", help="save the report without emailing it")
    ap.add_argument("--test-email", action="store_true", help="send a test email and exit")
    a = ap.parse_args()
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
