# Runs the influencer monitor on a Linux server. See "Run on a server" in README.md.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

WORKDIR /app
RUN pip install --no-cache-dir playwright==1.49.1 && apt-get update \
    && apt-get install -y --no-install-recommends xvfb tzdata && rm -rf /var/lib/apt/lists/*
COPY monitor.py ./

# A normal (non-headless) browser on a virtual screen: Instagram trusts it more than headless mode.
ENV BROWSER=chromium HEADLESS=false HIDE_WINDOW=false PYTHONUNBUFFERED=1
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x900x24", "python", "monitor.py", "--daemon"]
