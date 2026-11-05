# Instagram Influencer Monitor

Watches an Instagram account (by default **@editorialcuentosconvalores**) and finds every creator who made reels or posts with it. Once a day it emails a report of what changed:

- **New influencers** who made their first reel or post with the account
- **New reels and posts** from known influencers, with links
- **Follower changes** of 10% or more, or 1,000+ followers
- **Influencers who no longer show up** (content deleted, tag removed or account made private)

It runs on your own computer, uses a normal Chrome-based browser logged in to your Instagram, and sends email through your own email account. Nothing else is needed.

## What it checks

| Source | What it finds |
|---|---|
| The account's **Reels** tab | Collab reels (posted together with the account) |
| The account's **Tagged** tab | Reels and posts where creators tagged the account |
| Each creator's profile | Their current follower count |

Results are saved in `data/influencers_reels_posts.json`, and each day's report is also saved in `reports/`.

## Setup (one time, about 10 minutes)

You need **Python 3.9 or newer** ([download](https://www.python.org/downloads/); on Windows tick **"Add python.exe to PATH"** while installing) and an Instagram account to log in with.

### 1. Download

```bash
git clone https://github.com/Sipolsyoid/gg.git influencer-monitor
cd influencer-monitor
```

Or download the ZIP from GitHub and unzip it.

### 2. Get an email password for sending

The report is sent from an email account you own.

- **Gmail:** turn on 2-Step Verification, then create an **App password** at <https://myaccount.google.com/apppasswords>. Use that 16-letter password, not your normal one.
- **Outlook / Microsoft 365:** use `smtp.office365.com`, port `587`. Your organisation must allow SMTP sending.

### 3. Run the setup

- **Windows:** double-click `setup.bat`
- **macOS / Linux:** run `bash setup.sh`

The setup does five things:

1. Installs the packages and a private browser.
2. Opens `.env` so you can fill in the email settings (see below), then sends a test email.
3. Opens a browser window where you **log in to Instagram**. Come back to the setup window and press Enter when you see your feed.
4. Saves that login in the `browser-profile` folder, so you don't need to log in again.
5. Schedules the daily check: Windows Task Scheduler, or `cron` on macOS/Linux.

### Settings (`.env`)

| Setting | Meaning |
|---|---|
| `INSTAGRAM_ACCOUNT` | Account to monitor, without `@` |
| `EMAIL_TO` | Who receives the report (comma-separate several) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | The email account that sends the report |
| `RUN_TIME` | Daily time, e.g. `13:00` (run the schedule script again after changing it) |
| `REQUEST_DELAY_SECONDS` | Pause between Instagram requests; raise it if Instagram blocks you |
| `HIDE_WINDOW` | `true` (default) runs Chrome off-screen so no window appears; `false` shows it |
| `HEADLESS` | `true` uses Chrome's invisible mode instead (Instagram may log it out, so keep `false`) |

## Everyday use

There's nothing to do. The check runs every day at `RUN_TIME` while the computer is on and you're signed in to it. A run takes about 10 to 20 minutes, because it pauses between requests so Instagram doesn't block it.

| To... | Do this |
|---|---|
| Run a check now | Windows: double-click `run.bat`. Others: `.venv/bin/python monitor.py` |
| Run without sending email | `python monitor.py --no-email` |
| Log in again (if the report says the login expired) | `python monitor.py --login` |
| Test the email settings | `python monitor.py --test-email` |
| See what happened | `logs/monitor.log` |
| Stop the daily check | Windows: delete "Instagram Influencer Monitor" in Task Scheduler. Others: `crontab -e` and remove the line |

## Run on a server (recommended for teams)

The monitor can run on any Linux server, such as a small VPS (Hetzner, DigitalOcean, OVH, about EUR 4-6 per month) or a company server. It then works around the clock without anyone's computer being on.

A server has no screen, so you can't log in to Instagram on it. Instead, **log in on your own computer once and copy the login to the server**.

### 1. On your own computer

1. Do the normal setup above, and run `python monitor.py --login`.
2. After the login, the project folder contains **`instagram-session.json`**. This is your Instagram login, so treat it like a password: never upload it to GitHub (it is in `.gitignore`) and never send it to anyone.

### 2. On the server

You need [Docker](https://docs.docker.com/engine/install/) installed on the server.

```bash
git clone https://github.com/Sipolsyoid/gg.git influencer-monitor
cd influencer-monitor
cp .env.example .env
nano .env            # fill in EMAIL_TO, SMTP_USER, SMTP_PASSWORD and RUN_TIME
```

Copy the login file from your computer to the server, into the same folder. For example, run this from your own computer:

```bash
scp instagram-session.json user@your-server:influencer-monitor/
```

The file must be on the server **before** the next step. Otherwise Docker creates an empty folder with that name.

Start it:

```bash
docker compose up -d --build
```

That's it. The monitor now checks every day at `RUN_TIME` (Riga time; change `TZ` in `docker-compose.yml` for another time zone) and emails the report. It restarts by itself after a server reboot.

| To... | Run on the server |
|---|---|
| See what it is doing | `docker compose logs -f` |
| Run a check right now | `docker compose exec monitor xvfb-run -a python monitor.py` |
| Test the email | `docker compose exec monitor python monitor.py --test-email` |
| Update to the newest version | `git pull && docker compose up -d --build` |
| Stop it | `docker compose down` |

**When Instagram logs out the server** (the email will say so): on your own computer run `python monitor.py --login` again, copy the new `instagram-session.json` to the server with `scp`, and run `docker compose restart`.

**Good to know:**
- Instagram sometimes asks for a security check when a login suddenly appears on a server in another country. Choose a server in your own country (or near it) to reduce this. If it keeps happening, approve the "Was this you?" notice in the Instagram app.
- **Use a separate Instagram account** for the monitor, not your personal one. Then a security check or a temporary block never affects your own account.

## Things to know

- **The computer must be on at the scheduled time.** On Windows, a missed run doesn't catch up later. The next day compares against the last saved data, so nothing is lost.
- **Instagram rounds follower counts above 10K** (for example 17K), so small changes for large accounts don't show.
- **If Instagram rate-limits the check,** the report says what couldn't be checked. If a check finds far fewer influencers than last time, it doesn't overwrite the saved data.
- **Your settings stay private.** `.env` (your email password) and `browser-profile/` (your Instagram login) are listed in `.gitignore`, so they never get uploaded to GitHub. Don't share them.
- **The tool only reads public pages** that you could open yourself while logged in. It never posts, likes or messages anything. Automated browsing can go against Instagram's terms, so keep the request delay reasonable.
