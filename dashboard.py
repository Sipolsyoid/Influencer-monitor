"""
Influencer dashboard: a small web server that shows the monitor's data in a browser.

It serves web/index.html and the JSON files the daily scan writes to data/, so the page
always shows the latest scan. Protected by a password when DASHBOARD_PASSWORD is set.

    python dashboard.py            # then open http://localhost:8080
"""

import base64
import hmac
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
# Only these files can be downloaded; nothing else in the folder is reachable.
FILES = {
    "/": (WEB / "index.html", "text/html; charset=utf-8"),
    "/data/influencers.json": (DATA / "influencers_reels_posts.json", "application/json"),
    "/data/products.json": (DATA / "product_influencers.json", "application/json"),
    "/data/history.json": (DATA / "history.json", "application/json"),
    "/data/posts.json": (DATA / "reel_owners.json", "application/json"),
}


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Handler(BaseHTTPRequestHandler):
    def allowed(self):
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not password:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            return hmac.compare_digest(given, password)
        return False

    def do_GET(self):
        if not self.allowed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Influencer dashboard"')
            self.end_headers()
            return
        path = self.path.split("?")[0]
        if path not in FILES:
            self.send_error(404)
            return
        file, ctype = FILES[path]
        body = file.read_bytes() if file.exists() else (b"[]" if path == "/data/history.json" else b"{}")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep the logs quiet
        pass


if __name__ == "__main__":
    load_env()
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    print(f"Dashboard on http://0.0.0.0:{port}" + (" (password protected)" if os.environ.get("DASHBOARD_PASSWORD") else ""),
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
