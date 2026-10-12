#!/usr/bin/env python3
"""Round 2 diagnostics: harder techniques against the sites that blocked round 1.

Methods (each runs in its own process over all targets):
  curl_ios / curl_android  - curl_cffi impersonating mobile Safari / Chrome on Android
  translate                - fetch through Google Translate's proxy (translate.goog)
  webkit_iphone            - Playwright WebKit emulating an iPhone
  firefox_wait             - Playwright Firefox, waits up to 25s for JS challenges
  patchright_wait          - Patchright + real Chrome (visible), waits for challenges
  camoufox_wait            - Camoufox (visible on Xvfb), waits for challenges
  amazon_uk                - Camoufox with a UK delivery postcode set on Amazon
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(__file__))
import diagnose as d  # noqa: E402

TARGETS = {
    "argos_4440006": "https://www.argos.co.uk/product/4440006",
    "argos_4350000": "https://www.argos.co.uk/product/4350000",
    "currys": "https://www.currys.co.uk/products/sony-playstation-5-pro-10272138.html",
    "very": "https://www.very.co.uk/playstation-5-pro-console/1601081144.prd",
    "very_search": "https://www.very.co.uk/search/playstation%205%20pro%20console",
    "littlewoods": "https://www.littlewoods.com/playstation-5-pro-console/1601081144.prd",
    "smyths": "https://www.smythstoys.com/uk/en-gb/gaming-and-tech/playstation-5/playstation-5-consoles/playstation-5-pro-console-2tb-digital-edition-slim/p/244166",
    "ao": "https://ao.com/",
    "hughes": "https://www.hughes.co.uk/",
    "johnlewis": "https://www.johnlewis.com/playstation-5-pro-console-with-dualsense-controller/p112736639",
    "game": "https://www.game.co.uk/en/playstation/consoles/ps5/",
    "ebuyer": "https://www.ebuyer.com/searchresults?descriptionfilter=playstation+5+pro",
    "box": "https://box.co.uk/p5hehwsny59577-sony-playstation-5-pro-console",
    "scan": "https://www.scan.co.uk/products/sony-playstation-ps5-pro-b-chassis-slim-gaming-console-high-speed-2tb-ssd-ray-tracing-hdr",
    "shopto": "https://www.shopto.net/en/ps5hw93-playstation5-pro-console-p1182475/",
    "ee": "https://store.ee.co.uk/products/sony-playstation-5-pro-1000046525-HW84.html",
    "amazon": "https://www.amazon.co.uk/dp/B0DJH2NS6R",
}
d.TARGETS = TARGETS

CHALLENGE = ("just a moment", "access denied", "attention required", "request unsuccessful",
             "pardon our interruption", "bear with us", "powered and protected", "please wait")


def translate_url(url):
    p = urlsplit(url)
    host = p.netloc.replace("-", "--").replace(".", "-")
    sep = "&" if p.query else "?"
    return f"https://{host}.translate.goog{p.path}{('?' + p.query) if p.query else ''}{sep}_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"


def _curl(imp, via_translate=False):
    from curl_cffi import requests as cr
    s = cr.Session(impersonate=imp)
    for name, url in d.TARGETS.items():
        r = s.get(translate_url(url) if via_translate else url, timeout=40,
                  headers={"Accept-Language": "en-GB,en;q=0.9"})
        yield name, r.status_code, r.text


def m_curl_ios():
    yield from _curl("safari_ios")


def m_curl_android():
    yield from _curl("chrome_android")


def m_translate():
    yield from _curl("chrome", via_translate=True)


def _wait_out_challenge(page, max_secs=25):
    """Let JS anti-bot challenges run; reload once halfway if still stuck."""
    t0, reloaded = time.time(), False
    while time.time() - t0 < max_secs:
        try:
            title = (page.title() or "").lower()
            body = page.inner_text("body")[:3000].lower() if page.query_selector("body") else ""
        except Exception:
            title, body = "", ""
        stuck = (not title and len(body) < 400) or any(c in title or c in body[:600] for c in CHALLENGE)
        if not stuck:
            return
        if not reloaded and time.time() - t0 > max_secs / 2:
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            reloaded = True
        page.wait_for_timeout(2500)


def _browse(ctx, targets=None):
    for name, url in (targets or d.TARGETS).items():
        page = ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            _wait_out_challenge(page)
            yield name, resp.status if resp else None, page.content()
        finally:
            page.close()


def m_webkit_iphone():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.webkit.launch(headless=True)
        ctx = b.new_context(**pw.devices["iPhone 15"], locale="en-GB", timezone_id="Europe/London")
        yield from _browse(ctx)
        b.close()


def m_firefox_wait():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.firefox.launch(headless=True)
        ctx = b.new_context(locale="en-GB", timezone_id="Europe/London")
        yield from _browse(ctx)
        b.close()


def m_patchright_wait():
    from patchright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=os.path.abspath("pr-profile"), channel="chrome", headless=False,
            no_viewport=True, locale="en-GB", timezone_id="Europe/London",
        )
        yield from _browse(ctx)
        ctx.close()


def _camoufox():
    from camoufox.sync_api import Camoufox
    headless = "virtual" if sys.platform.startswith("linux") else False
    return Camoufox(headless=headless, locale="en-GB", os="windows", humanize=True)


def m_camoufox_wait():
    with _camoufox() as browser:
        ctx = browser.new_context(locale="en-GB", timezone_id="Europe/London")
        yield from _browse(ctx)


def m_amazon_uk():
    """Set a UK delivery postcode first, so Amazon shows UK availability."""
    with _camoufox() as browser:
        ctx = browser.new_context(locale="en-GB", timezone_id="Europe/London")
        page = ctx.new_page()
        page.goto("https://www.amazon.co.uk/", wait_until="domcontentloaded", timeout=45000)
        for sel in ("#sp-cc-accept", "input[name='accept']"):
            if page.query_selector(sel):
                page.click(sel)
                break
        try:
            page.click("#nav-global-location-popover-link", timeout=10000)
            page.fill("#GLUXZipUpdateInput", "SW1A 1AA", timeout=10000)
            page.click("#GLUXZipUpdate input, #GLUXZipUpdate", timeout=10000)
            page.wait_for_timeout(3000)
        except Exception as exc:
            print(f"[amazon_uk] postcode step failed: {exc}", flush=True)
        page.close()
        yield from _browse(ctx, {k: v for k, v in d.TARGETS.items() if k == "amazon"})


METHODS = {k[2:]: v for k, v in globals().items() if k.startswith("m_")}
d.METHODS = METHODS


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--method":
        d.run_method_resilient(sys.argv[2])
        return
    methods = sys.argv[2].split(",") if len(sys.argv) > 2 else list(METHODS)
    procs = [subprocess.Popen([sys.executable, __file__, "--method", m]) for m in methods]
    for p in procs:
        try:
            p.wait(timeout=40 * 60)
        except subprocess.TimeoutExpired:
            p.kill()
    d.summary(methods)


if __name__ == "__main__":
    main()
