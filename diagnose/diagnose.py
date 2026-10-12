#!/usr/bin/env python3
"""Scraping diagnostics: try every retailer with several fetch methods.

Each method runs in its own process over all targets. Raw pages are saved to
out/<method>/<target>.html and a summary table to out/summary.md, so we can see
exactly what each site returns to GitHub's runners and which method gets through.

  python diagnose.py                 # run all methods available on this OS
  python diagnose.py --method curl_chrome
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(os.environ.get("DIAG_OUT", "out"))

TARGETS = {
    # retailers
    "amazon": "https://www.amazon.co.uk/dp/B0DM8SFRJV",
    "argos": "https://www.argos.co.uk/product/4440006",
    "currys": "https://www.currys.co.uk/products/sony-playstation-5-pro-10272138.html",
    "smyths": "https://www.smythstoys.com/uk/en-gb/gaming-and-tech/playstation-5/playstation-5-consoles/playstation-5-pro-console-2tb-digital-edition-slim/p/244166",
    "very": "https://www.very.co.uk/playstation-playstationreg5-pro-console-2tb-with-grand-theft-auto-v-amp-disc-drive/1601238401.prd",
    "littlewoods": "https://www.littlewoods.com/search/playstation%205%20pro%20console",
    "johnlewis": "https://www.johnlewis.com/search?search-term=playstation%205%20pro%20console",
    "game": "https://www.game.co.uk/search?q=playstation%205%20pro%20console",
    "tesco": "https://www.tesco.com/groceries/en-GB/search?query=playstation%205%20pro%20console",
    "asda": "https://www.asda.com/groceries/search/playstation%205%20pro",
    "costco": "https://www.costco.co.uk/search?text=playstation%205%20pro",
    "ao": "https://ao.com/l/search/101/99/?search=playstation%205%20pro",
    "ebuyer": "https://www.ebuyer.com/search?q=playstation+5+pro+console",
    "scan": "https://www.scan.co.uk/search?q=playstation+5+pro+console",
    "hughes": "https://www.hughes.co.uk/search?q=playstation+5+pro",
    "box": "https://www.box.co.uk/",
    "psdirect": "https://direct.playstation.com/en-gb/buy-consoles/playstation5-pro-console-2-tb",
    # stock trackers / price comparison (option 2)
    "stockinformer": "https://www.stockinformer.co.uk/checker-playstation-5-pro-console",
    "stockgoblin": "https://www.stockgoblin.co.uk/products/playstation-5-pro-console",
    "nowinstock": "https://www.nowinstock.net/uk/videogaming/consoles/sonyps5pro/",
    "pricespy": "https://pricespy.co.uk/search?search=playstation%205%20pro",
    "idealo": "https://www.idealo.co.uk/mscat.html?q=playstation+5+pro",
    "pricerunner": "https://www.pricerunner.com/results?q=playstation%205%20pro",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
TIMEOUT = 30


# --------------------------------------------------------------------------- fetchers
# Each yields (name, status, html) for every target.

def m_requests():
    import requests
    s = requests.Session()
    for name, url in TARGETS.items():
        r = s.get(url, headers=HEADERS, timeout=TIMEOUT)
        yield name, r.status_code, r.text


def _curl(imp):
    from curl_cffi import requests as cr
    s = cr.Session(impersonate=imp)
    for name, url in TARGETS.items():
        r = s.get(url, timeout=TIMEOUT, headers={"Accept-Language": "en-GB,en;q=0.9"})
        yield name, r.status_code, r.text


def m_curl_chrome():
    yield from _curl("chrome")


def m_curl_safari_ios():
    yield from _curl("safari_ios")


def m_jina():
    """r.jina.ai fetches the page from its own servers and returns the HTML."""
    import requests
    for name, url in TARGETS.items():
        r = requests.get("https://r.jina.ai/" + url, headers={"X-Return-Format": "html"}, timeout=60)
        yield name, r.status_code, r.text
        time.sleep(3)  # free tier rate limit


def _playwright(mod, browser_name, headless, channel=None, stealth_ctx=True):
    with mod.sync_playwright() as pw:
        bt = getattr(pw, browser_name)
        kwargs = {"headless": headless}
        if channel:
            kwargs["channel"] = channel
        browser = bt.launch(**kwargs)
        ctx_kwargs = {"locale": "en-GB", "timezone_id": "Europe/London"}
        if stealth_ctx:
            ctx_kwargs.update(user_agent=UA, viewport={"width": 1366, "height": 900})
        ctx = browser.new_context(**ctx_kwargs)
        for name, url in TARGETS.items():
            page = ctx.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                # Give JS challenges (Cloudflare/Akamai) a chance to solve and redirect.
                page.wait_for_timeout(4000)
                yield name, resp.status if resp else None, page.content()
            finally:
                page.close()
        browser.close()


def m_pw_chromium_headless():
    import playwright.sync_api as m
    yield from _playwright(m, "chromium", True)


def m_pw_firefox_headless():
    import playwright.sync_api as m
    yield from _playwright(m, "firefox", True)


def m_pw_webkit_headless():
    import playwright.sync_api as m
    yield from _playwright(m, "webkit", True)


def m_chrome_headed():
    """Real Google Chrome with a visible window (Xvfb on Linux)."""
    import playwright.sync_api as m
    yield from _playwright(m, "chromium", False, channel="chrome")


def m_patchright_headed():
    """Patchright = Playwright patched to hide automation; real Chrome, visible."""
    import patchright.sync_api as m
    yield from _playwright(m, "chromium", False, channel="chrome", stealth_ctx=False)


def m_camoufox():
    """Camoufox = Firefox build with fingerprint spoofing, built for anti-bot."""
    from camoufox.sync_api import Camoufox
    with Camoufox(headless=True, locale="en-GB", os="windows") as browser:
        for name, url in TARGETS.items():
            page = browser.new_page()
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(4000)
                yield name, resp.status if resp else None, page.content()
            finally:
                page.close()


METHODS = {k[2:]: v for k, v in globals().items() if k.startswith("m_")}


# --------------------------------------------------------------------------- analysis

BLOCK_RE = re.compile(
    r"(access denied|captcha|are you a robot|verify you are (a )?human|pardon our interruption|"
    r"request unsuccessful|just a moment|attention required|unusual traffic|"
    r"enter the characters you see|bot protection|incapsula|perimeterx|px-captcha|"
    r"_abck|akamai|cf-chl|challenge-platform|datadome)",
    re.I,
)
BUY_RE = re.compile(r"add to (basket|trolley|cart|bag)|buy now|pre-?order", re.I)
OOS_RE = re.compile(r"out of stock|sold out|currently unavailable|no longer available|coming soon", re.I)
PS5_RE = re.compile(r"(ps5|playstation\s*(®|&reg;)?\s*5)\s*(™|&trade;)?\s*pro", re.I)


def analyse(status, html):
    title = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    avail = re.findall(r'"availability"\s*:\s*"(?:https?://schema\.org/)?(\w+)"', html or "")
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html or "", flags=re.S | re.I)
    return {
        "status": status,
        "bytes": len(html or ""),
        "title": " ".join((title.group(1) if title else "").split())[:60],
        "blocked_marker": (BLOCK_RE.search(html[:20000]).group(0) if html and BLOCK_RE.search(html[:20000]) else ""),
        "schema_availability": sorted(set(avail))[:4],
        "ps5_pro_mentions": len(PS5_RE.findall(text)),
        "buy_text": bool(BUY_RE.search(text)),
        "oos_text": bool(OOS_RE.search(text)),
    }


def verdict(a):
    if a.get("error"):
        return "ERROR"
    if a["status"] and a["status"] >= 400:
        return "BLOCKED" if a["status"] in (401, 403, 429, 503) else f"HTTP{a['status']}"
    if a["schema_availability"] or (a["ps5_pro_mentions"] and (a["buy_text"] or a["oos_text"])):
        return "OK"
    if a["blocked_marker"] and a["bytes"] < 60000:
        return "BLOCKED"
    if a["ps5_pro_mentions"]:
        return "PARTIAL"
    return "NO-DATA"


def run_method(method):
    out = OUT / method
    out.mkdir(parents=True, exist_ok=True)
    results = {}
    try:
        it = METHODS[method]()
        while True:
            t0 = time.time()
            try:
                name, status, html = next(it)
            except StopIteration:
                break
            (out / f"{name}.html").write_text((html or "")[:2_000_000], encoding="utf-8")
            a = analyse(status, html)
            a["secs"] = round(time.time() - t0, 1)
            results[name] = a
            print(f"[{method}] {name}: {verdict(a)} {a['status']} {a['bytes']}b {a['title']!r}", flush=True)
    except Exception as exc:
        # A failure mid-way (e.g. navigation error) aborts the generator; restart
        # from the next target so one bad site doesn't hide the rest.
        results["_fatal"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        print(f"[{method}] FAILED: {exc}", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=1))


def run_method_resilient(method):
    """Run a method one target at a time so a single error can't stop the rest."""
    global TARGETS
    all_targets = dict(TARGETS)
    merged = {}
    for name, url in all_targets.items():
        TARGETS = {name: url}
        run_method(method)
        res = json.loads((OUT / method / "results.json").read_text())
        if "_fatal" in res:
            merged[name] = {**res["_fatal"], "status": None}
            print(f"[{method}] {name}: ERROR {res['_fatal']['error'][:100]}", flush=True)
        else:
            merged.update(res)
    TARGETS = all_targets
    (OUT / method / "results.json").write_text(json.dumps(merged, indent=1))


def summary(methods):
    rows = {}
    for m in methods:
        p = OUT / m / "results.json"
        rows[m] = json.loads(p.read_text()) if p.exists() else {}
    lines = [f"## Scraping diagnostics on {platform.system()} ({os.environ.get('RUNNER_OS', 'local')})", "",
             "| site | " + " | ".join(methods) + " |", "|---|" + "---|" * len(methods)]
    for name in TARGETS:
        cells = []
        for m in methods:
            a = rows[m].get(name)
            if not a:
                cells.append("–")
            elif a.get("error"):
                cells.append("ERROR")
            else:
                v = verdict(a)
                extra = ",".join(a["schema_availability"]) or ("buy" if a["buy_text"] else "") or ("oos" if a["oos_text"] else "")
                cells.append(f"{v} {a['status']} {extra}".strip())
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    md = "\n".join(lines)
    (OUT / "summary.md").write_text(md + "\n")
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--method")
    ap.add_argument("--methods", help="comma-separated list to run in parallel")
    args = ap.parse_args()
    if args.method:
        run_method_resilient(args.method)
        return
    methods = args.methods.split(",") if args.methods else list(METHODS)
    procs = [subprocess.Popen([sys.executable, __file__, "--method", m]) for m in methods]
    for p in procs:
        try:
            p.wait(timeout=40 * 60)
        except subprocess.TimeoutExpired:
            p.kill()
    summary(methods)


if __name__ == "__main__":
    main()
