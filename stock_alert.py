#!/usr/bin/env python3
"""PS5 Pro UK stock alert.

Checks the retailers and stock trackers in retailers.yaml (each with the
scraping engine that gets past its bot protection) and sends
an alert (ntfy push, email and/or SMS) when a PS5 Pro goes from unavailable to
in stock. Designed to run on a GitHub Actions schedule; state.json remembers what
was already alerted so you get one alert per restock, not one every 5 minutes.

Usage:
  python stock_alert.py                 # check everything, alert on restocks
  python stock_alert.py --dry-run       # check everything, print instead of alerting
  python stock_alert.py --only currys   # check retailers whose name contains "currys"
  python stock_alert.py --test-notify   # send a test alert to every configured channel
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

import requests
import yaml
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "retailers.yaml"
STATE_PATH = HERE / "state.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
CONCURRENCY = 5
DEFAULT_MAX_LINKS = 4

IN_STOCK_SCHEMA = {"instock", "limitedavailability", "onlineonly", "preorder", "presale"}
OUT_OF_STOCK_SCHEMA = {"outofstock", "soldout", "discontinued", "backorder", "instoreonly"}

BUY_BUTTON = re.compile(
    r"^\s*(add to (basket|trolley|cart|bag)|buy now|pre-?order( now)?|add for delivery|deliver to me)\b",
    re.I,
)
OUT_OF_STOCK_TEXT = re.compile(
    r"(out of stock|sold out|currently unavailable|temporarily unavailable|no longer available"
    r"|not available (online|for delivery)|coming soon|notify me when|email me when|"
    r"register your interest|join the queue|we don't know when or if this item will be back)",
    re.I,
)
BLOCKED_TEXT = re.compile(
    r"(captcha|are you a robot|verify you are (a )?human|access denied|pardon our interruption|"
    r"request unsuccessful|unusual traffic|enter the characters you see|bot protection|incapsula|"
    r"powered and protected by|just a moment|bear with us|checking your connection|"
    r"click the button below to continue shopping)",
    re.I,
)
NOT_UK_TEXT = re.compile(r"cannot be dispatched to your selected delivery location", re.I)


# --------------------------------------------------------------------------- models

@dataclass
class Result:
    retailer: str
    url: str
    status: str  # in_stock | out_of_stock | unknown | blocked | error
    title: str = ""
    price: float | None = None
    reason: str = ""
    key: str = ""  # identity for alert state; defaults to the URL

    def __post_init__(self):
        self.key = self.key or self.url


@dataclass
class Run:
    settings: dict
    checked: dict[str, Result] = field(default_factory=dict)
    sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(CONCURRENCY))


# --------------------------------------------------------------------------- page analysis

def _walk_jsonld(node, out: list):
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, out)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(str(t).lower() in ("product", "productgroup") for t in types):
            out.append(node)
        for key in ("@graph", "mainEntity", "itemListElement", "item", "hasVariant"):
            if key in node:
                _walk_jsonld(node[key], out)


def _parse_price(value) -> float | None:
    if value is None:
        return None
    m = re.search(r"\d[\d,]*(\.\d+)?", str(value))
    return float(m.group(0).replace(",", "")) if m else None


def parse_structured_data(blocks: list[str]) -> tuple[list[str], list[float], str]:
    """Return (availabilities, prices, product name) from JSON-LD script blocks."""
    products: list[dict] = []
    for raw in blocks:
        try:
            _walk_jsonld(json.loads(raw), products)
        except (json.JSONDecodeError, TypeError):
            continue
    avail, prices, name = [], [], ""
    for p in products:
        name = name or html.unescape(str(p.get("name") or ""))
        offers = p.get("offers") or []
        offers = offers if isinstance(offers, list) else [offers]
        for o in offers:
            if not isinstance(o, dict):
                continue
            nested = o.get("offers")
            for off in (nested if isinstance(nested, list) else [o]):
                if not isinstance(off, dict):
                    continue
                a = off.get("availability")
                if a:
                    avail.append(str(a).rsplit("/", 1)[-1].lower())
                price = _parse_price(off.get("price") or off.get("lowPrice"))
                if price:
                    prices.append(price)
    return avail, prices, name


def decide(schema_avail: list[str], buy_buttons: int, page_text: str) -> tuple[str, str]:
    """Combine structured data and visible page cues into a stock status."""
    text_out = bool(OUT_OF_STOCK_TEXT.search(page_text))
    schema_in = any(a in IN_STOCK_SCHEMA for a in schema_avail)
    schema_out = bool(schema_avail) and all(a in OUT_OF_STOCK_SCHEMA for a in schema_avail)

    if schema_in:
        # Some sites leave "InStock" in their markup when sold out; trust the page
        # if it says out of stock and offers no way to buy.
        if text_out and buy_buttons == 0:
            return "out_of_stock", "schema says in stock but page says unavailable"
        return "in_stock", "schema.org availability"
    if schema_out:
        return "out_of_stock", "schema.org availability"
    if buy_buttons and not text_out:
        return "in_stock", "buy button visible"
    if text_out:
        return "out_of_stock", "out-of-stock wording"
    if buy_buttons:
        return "in_stock", "buy button visible (page also mentions unavailability)"
    return "unknown", "no stock signal found"


def canonical_url(href: str, base: str) -> str:
    url = urljoin(base, href)
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    if m and "amazon." in url:
        return f"https://www.amazon.co.uk/dp/{m.group(1)}"
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def price_ok(price: float | None, settings: dict) -> bool:
    if price is None:
        return True
    lo, hi = settings.get("min_price") or 0, settings.get("max_price") or 0
    return (not lo or price >= lo) and (not hi or price <= hi)


# --------------------------------------------------------------------------- fetching
#
# Each retailer can pick the fetch "engine" that gets past its bot protection
# (see the diagnose/ workflows for how these were chosen):
#   chromium       Playwright headless Chromium (default)
#   firefox        Playwright headless Firefox
#   webkit_iphone  Playwright WebKit emulating an iPhone
#   camoufox       Camoufox, a Firefox build with fingerprint spoofing
#   curl_ios       curl_cffi impersonating iPhone Safari (no JavaScript)
#   curl_chrome    curl_cffi impersonating desktop Chrome (no JavaScript)
#   jina           fetched by the r.jina.ai reader service from its own servers

BROWSER_ENGINES = {"chromium", "firefox", "webkit_iphone", "camoufox"}
HTML_ENGINES = {"curl_ios", "curl_chrome", "jina"}
CHALLENGE_TITLES = ("just a moment", "attention required", "bear with us", "please wait", "one moment")

LINKS_JS = """els => els.map(a => ({
    href: a.getAttribute('href'),
    text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title')
           || (a.querySelector('img') && a.querySelector('img').alt) || '').trim()
}))"""

# A tracker "card" is the largest element around a retailer heading (h3) that
# contains no other retailer heading.
CARDS_JS = """() => [...document.querySelectorAll('h3')].map(h3 => {
    let el = h3;
    while (el.parentElement && el.parentElement.querySelectorAll('h3').length === 1) el = el.parentElement;
    const h4 = el.querySelector('h4');
    return {retailer: h3.innerText.trim() || h3.textContent.trim(), title: h4 ? h4.textContent.trim() : '',
            text: el.innerText, hrefs: [...el.querySelectorAll('a[href]')].map(a => a.href)};
})"""


@dataclass
class PageData:
    status: int | None
    title: str
    text: str
    ld_blocks: list[str]
    buy_buttons: int
    links: list[dict]
    cards: list[dict] = field(default_factory=list)
    price_text: str = ""


def parse_html(status: int | None, html: str, base_url: str, price_selector: str | None = None) -> PageData:
    """Build PageData from raw HTML (for the engines that don't run JavaScript)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    ld_blocks = [s.get_text() for s in soup.select('script[type="application/ld+json"]')]
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    buttons = 0
    for el in soup.select("button, input[type=submit], input[type=button], a[role=button]"):
        label = el.get_text(" ", strip=True) or el.get("value") or el.get("aria-label") or ""
        disabled = el.has_attr("disabled") or el.get("aria-disabled") == "true"
        if BUY_BUTTON.search(label) and not disabled:
            buttons += 1
    links = []
    for a in soup.select("a[href]"):
        img = a.find("img")
        label = a.get_text(" ", strip=True) or a.get("aria-label") or a.get("title") or (img.get("alt") if img else "") or ""
        links.append({"href": a["href"], "text": label})
    cards = []
    for h3 in soup.find_all("h3"):
        el = h3
        while el.parent is not None and el.parent.name and len(el.parent.find_all("h3")) == 1:
            el = el.parent
        h4 = el.find("h4")
        cards.append({
            "retailer": h3.get_text(" ", strip=True),
            "title": h4.get_text(" ", strip=True) if h4 else "",
            "text": el.get_text("\n", strip=True),
            "hrefs": [urljoin(base_url, a["href"]) for a in el.select("a[href]")],
        })
    price_el = soup.select_one(price_selector) if price_selector else None
    return PageData(
        status=status,
        title=soup.title.get_text(strip=True) if soup.title else "",
        text=soup.get_text("\n", strip=True),
        ld_blocks=ld_blocks,
        buy_buttons=buttons,
        links=links,
        cards=cards,
        price_text=price_el.get_text(" ", strip=True) if price_el else "",
    )


class Engines:
    """Lazily starts one browser/HTTP client per engine and shares it across checks."""

    def __init__(self, settings: dict):
        self.settings = settings
        self.stack = AsyncExitStack()
        self.contexts: dict[str, object] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.pw = None
        self.prepared: set[str] = set()

    async def close(self):
        await self.stack.aclose()

    async def _playwright(self):
        if self.pw is None:
            self.pw = await self.stack.enter_async_context(async_playwright())
        return self.pw

    async def context(self, engine: str):
        lock = self.locks.setdefault(engine, asyncio.Lock())
        async with lock:
            if engine not in self.contexts:
                self.contexts[engine] = await self._start(engine)
            return self.contexts[engine]

    async def _start(self, engine: str):
        london = {"locale": "en-GB", "timezone_id": "Europe/London"}
        if engine == "chromium":
            pw = await self._playwright()
            browser = await pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(
                user_agent=USER_AGENT, viewport={"width": 1366, "height": 900},
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"}, **london,
            )
            await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            # Skip heavy assets - we only need the HTML/JS.
            await ctx.route(re.compile(r"\.(png|jpe?g|gif|webp|avif|woff2?|ttf|mp4|webm)(\?|$)", re.I),
                            lambda route: route.abort())
            return ctx
        if engine == "firefox":
            pw = await self._playwright()
            browser = await pw.firefox.launch(headless=True)
            return await browser.new_context(**london)
        if engine == "webkit_iphone":
            pw = await self._playwright()
            browser = await pw.webkit.launch(headless=True)
            return await browser.new_context(**pw.devices["iPhone 15"], **london)
        if engine == "camoufox":
            from camoufox.async_api import AsyncCamoufox

            headless = self.settings.get("camoufox_headless", True)
            browser = await self.stack.enter_async_context(
                AsyncCamoufox(headless=headless, locale="en-GB", os="windows", humanize=True))
            return await browser.new_context(**london)
        if engine in ("curl_ios", "curl_chrome", "jina"):
            from curl_cffi.requests import AsyncSession

            imp = {"curl_ios": "safari_ios", "curl_chrome": "chrome", "jina": "chrome"}[engine]
            return await self.stack.enter_async_context(AsyncSession(impersonate=imp))
        raise ValueError(f"unknown engine {engine!r}")

    async def prepare(self, engine: str, setup: str | None):
        """One-off session setup, e.g. giving Amazon a UK delivery postcode."""
        if not setup or (engine, setup) in self.prepared:
            return
        ctx = await self.context(engine)
        async with self.locks[engine]:
            if (engine, setup) in self.prepared:
                return
            self.prepared.add((engine, setup))
            if setup == "amazon_uk_postcode":
                page = await ctx.new_page()
                try:
                    await page.goto("https://www.amazon.co.uk/", wait_until="domcontentloaded", timeout=45000)
                    for sel in ("#sp-cc-accept", "input[name='accept']"):
                        if await page.query_selector(sel):
                            await page.click(sel)
                            break
                    await page.click("#nav-global-location-popover-link", timeout=10000)
                    await page.fill("#GLUXZipUpdateInput", self.settings.get("postcode", "SW1A 1AA"), timeout=10000)
                    await page.click("#GLUXZipUpdate input, #GLUXZipUpdate", timeout=10000)
                    await page.wait_for_timeout(3000)
                except Exception as exc:
                    print(f"!! Amazon postcode setup failed: {exc}", file=sys.stderr)
                finally:
                    await page.close()

    async def fetch(self, engine: str, url: str, price_selector: str | None = None) -> PageData:
        timeout = self.settings.get("timeout", 45)
        ctx = await self.context(engine)
        if engine in HTML_ENGINES:
            target, headers = url, {"Accept-Language": "en-GB,en;q=0.9"}
            if engine == "jina":
                target = "https://r.jina.ai/" + url
                headers = {"X-Return-Format": "html"}
                if os.environ.get("JINA_API_KEY"):
                    headers["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
            r = await ctx.get(target, headers=headers, timeout=timeout + 15)
            return parse_html(r.status_code, r.text, url, price_selector)

        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # many retail sites never go fully idle
            await self._wait_out_challenge(page)
            buttons = 0
            for loc in await page.get_by_role("button", name=BUY_BUTTON).all():
                try:
                    if await loc.is_visible() and await loc.is_enabled():
                        buttons += 1
                except Exception:
                    pass
            price_text = ""
            if price_selector:
                el = await page.query_selector(price_selector)
                price_text = (await el.inner_text()) if el else ""
            return PageData(
                status=resp.status if resp else None,
                title=await page.title(),
                text=await page.inner_text("body"),
                ld_blocks=await page.eval_on_selector_all(
                    'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"),
                buy_buttons=buttons,
                links=await page.eval_on_selector_all("a[href]", LINKS_JS),
                cards=await page.evaluate(CARDS_JS),
                price_text=price_text,
            )
        finally:
            await page.close()

    async def _wait_out_challenge(self, page, max_secs: float | None = None):
        """Give JavaScript anti-bot challenges time to finish and redirect."""
        max_secs = max_secs if max_secs is not None else self.settings.get("challenge_wait", 20)
        waited = 0.0
        while waited < max_secs:
            try:
                title = (await page.title() or "").lower()
            except Exception:
                title = ""
            if not any(t in title for t in CHALLENGE_TITLES):
                return
            await page.wait_for_timeout(2000)
            waited += 2


# --------------------------------------------------------------------------- checks

def _error(name: str, url: str, exc: Exception, title: str = "") -> Result:
    return Result(name, url, "error", title, reason=f"{type(exc).__name__}: {' '.join(str(exc).split())[:120]}")


def _blocked(data: PageData) -> bool:
    if data.status in (401, 403, 429, 503):
        return True
    if data.ld_blocks and parse_structured_data(data.ld_blocks)[0]:
        return False
    return data.buy_buttons == 0 and bool(BLOCKED_TEXT.search(data.title + "\n" + data.text[:5000]))


async def check_product(run: Run, engines: Engines, retailer: dict, url: str, link_title: str = "") -> Result:
    if url in run.checked:
        return run.checked[url]
    name, settings = retailer["name"], run.settings
    engine = retailer.get("engine", settings.get("engine", "chromium"))
    async with run.sem:
        try:
            await engines.prepare(engine, retailer.get("setup"))
            data = await engines.fetch(engine, url, retailer.get("price_selector"))
        except Exception as exc:
            run.checked[url] = _error(name, url, exc, link_title)
            return run.checked[url]

    avail, prices, product_name = parse_structured_data(data.ld_blocks)
    title = product_name or link_title or data.title
    price = min(prices) if prices else _parse_price(data.price_text)

    if _blocked(data):
        res = Result(name, url, "blocked", title, price, f"HTTP {data.status} / bot check")
    elif NOT_UK_TEXT.search(data.text[:20000]):
        res = Result(name, url, "unknown", title, price, "site thinks we're outside the UK")
    elif data.status and data.status >= 400:
        res = Result(name, url, "error", title, price, f"HTTP {data.status} - product URL may have changed")
    else:
        status, reason = decide(avail, data.buy_buttons, data.text)
        res = Result(name, url, status, title, price, reason)
        if status == "in_stock":
            req = retailer.get("require_text")
            if req and req.lower() not in re.sub(r"\s+", " ", data.text).lower():
                res.status, res.reason = "out_of_stock", f'in stock but "{req}" not on page'
            elif not price_ok(price, settings):
                res.status, res.reason = "out_of_stock", f"in stock but price £{price:.2f} outside range"
    run.checked[url] = res
    return res


async def check_search(run: Run, engines: Engines, retailer: dict) -> list[Result]:
    settings, url = run.settings, retailer["url"]
    engine = retailer.get("engine", settings.get("engine", "chromium"))
    async with run.sem:
        try:
            await engines.prepare(engine, retailer.get("setup"))
            data = await engines.fetch(engine, url)
        except Exception as exc:
            return [_error(retailer["name"], url, exc)]

    title_re = re.compile(settings["title_pattern"], re.I)
    exclude_re = re.compile(settings["exclude_pattern"], re.I)
    link_re = re.compile(retailer["link_pattern"]) if retailer.get("link_pattern") else None
    base_host = urlsplit(url).netloc.removeprefix("www.")

    found: dict[str, str] = {}
    for link in data.links:
        href, label = link["href"] or "", " ".join((link["text"] or "").split())
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if not title_re.search(label) or exclude_re.search(label):
            continue
        if link_re and not link_re.search(href):
            continue
        full = canonical_url(href, url)
        if base_host not in urlsplit(full).netloc or full == canonical_url(url, url):
            continue
        found.setdefault(full, label)
        if len(found) >= retailer.get("max_links", DEFAULT_MAX_LINKS):
            break

    if not found:
        if _blocked(data):
            status, reason = "blocked", f"HTTP {data.status} / bot check"
        elif data.status and data.status >= 400:
            status, reason = "error", f"HTTP {data.status} - search URL may have changed"
        else:
            status, reason = "unknown", "no PS5 Pro listings found on search page"
        return [Result(retailer["name"], url, status, reason=reason)]
    return list(await asyncio.gather(*(check_product(run, engines, retailer, u, t) for u, t in found.items())))


AFFILIATE_PARAMS = ("p", "u", "url", "ued", "murl", "destination")


def real_link(hrefs: list[str], tracker_url: str) -> str:
    """Pull the retailer's own URL out of a tracker's affiliate links."""
    tracker_host = urlsplit(tracker_url).netloc
    for h in hrefs:
        q = parse_qs(urlsplit(h).query)
        for k in AFFILIATE_PARAMS:
            if q.get(k) and q[k][0].startswith("http"):
                return unquote(q[k][0])
        m = re.search(r"destination:(https?[^?#\s]+)", h) or re.search(r"/(https://www\.[^?#\s]+)", h)
        if m:
            return unquote(m.group(1))
    for h in hrefs:
        host = urlsplit(h).netloc
        if host and host != tracker_host and not re.search(r"awin|anrdoezrs|pxf\.io|prf\.hn|pclick|skimresources", h):
            return h
    return tracker_url


TRACKER_OUT = re.compile(r"\b(no stock|sold out|out of stock)\b", re.I)
TRACKER_IN = re.compile(r"\b(in stock|pre-?order( available)?)\b", re.I)
TRACKER_UNKNOWN = re.compile(r"check stock|tracking paused|paused", re.I)


async def check_tracker(run: Run, engines: Engines, entry: dict) -> list[Result]:
    """Read a stock-tracker site's table of retailers for the PS5 Pro."""
    settings, url, source = run.settings, entry["url"], entry["name"]
    engine = entry.get("engine", settings.get("engine", "chromium"))
    async with run.sem:
        try:
            data = await engines.fetch(engine, url)
        except Exception as exc:
            return [_error(source, url, exc)]
    if _blocked(data) and not data.cards:
        return [Result(source, url, "blocked", reason=f"HTTP {data.status} / bot check")]

    title_re = re.compile(settings["title_pattern"], re.I)
    exclude_re = re.compile(settings["exclude_pattern"], re.I)
    skip = {s.lower() for s in entry.get("exclude_retailers", [])}
    results = []
    for card in data.cards:
        retailer, title = card["retailer"].strip(), " ".join(card["title"].split())
        if not retailer or not title_re.search(title) or exclude_re.search(title) or retailer.lower() in skip:
            continue
        # Drop the product title so "£100 gift card" bundles aren't read as the price.
        text = " ".join(card["text"].split()).replace(title, " ")
        if TRACKER_OUT.search(text):
            status, said = "out_of_stock", TRACKER_OUT.search(text).group(0)
        elif TRACKER_IN.search(text):
            status, said = "in_stock", TRACKER_IN.search(text).group(0)
        elif TRACKER_UNKNOWN.search(text):
            status, said = "unknown", TRACKER_UNKNOWN.search(text).group(0)
        else:
            continue  # not a retailer row (e.g. the site's own widgets)
        price_m = re.search(r"£\s?(\d[\d,]*(?:\.\d\d)?)", text)
        price = float(price_m.group(1).replace(",", "")) if price_m else None
        link = real_link(card["hrefs"], url)
        if "amazon." in link:
            link = canonical_url(link, link)  # drop the tracker's affiliate tag
        key = canonical_url(link, link) if link != url else f"{url}#{re.sub(r'[^a-z0-9]+', '-', (retailer + ' ' + title).lower())}"
        res = Result(f"{retailer} (via {source})", link, status, title, price, f'{source} says "{said}"', key=key)
        if status == "in_stock" and not price_ok(price, settings):
            res.status, res.reason = "out_of_stock", f"{source} says in stock but price £{price:.2f} outside range"
        results.append(res)
    if not results:
        return [Result(source, url, "unknown", reason="no PS5 Pro rows found - tracker page layout may have changed")]
    return results


STATUS_RANK = {"in_stock": 0, "out_of_stock": 1, "unknown": 2, "blocked": 3, "error": 4}


async def check_all(config: dict, only: str | None) -> list[Result]:
    run = Run(settings=config["settings"])
    retailers = [r for r in config["retailers"] if r.get("enabled", True)]
    if only:
        retailers = [r for r in retailers if only.lower() in r["name"].lower()]

    engines = Engines(run.settings)

    async def one(r: dict) -> list[Result]:
        kind = r.get("type", "product")
        if kind == "tracker":
            return await check_tracker(run, engines, r)
        if kind == "search":
            return await check_search(run, engines, r)
        return [await check_product(run, engines, r, r["url"])]

    try:
        nested = await asyncio.gather(*(one(r) for r in retailers))
    finally:
        await engines.close()

    # The same product can be found by several entries (e.g. a retailer check and a
    # tracker). Keep the most useful answer for each: in stock beats out of stock
    # beats unknown/blocked, so a blocked direct check never hides a tracker alert.
    best: dict[str, Result] = {}
    for res in (r for group in nested for r in group):
        cur = best.get(res.key)
        if cur is None or STATUS_RANK[res.status] < STATUS_RANK[cur.status]:
            best[res.key] = res
    return list(best.values())


# --------------------------------------------------------------------------- notifications

def notify(alerts: list[Result], test: bool = False) -> list[str]:
    """Send alerts on every configured channel. Returns the channels that worked."""
    if test:
        subject = "PS5 Pro alert test"
        lines = ["Test alert: your PS5 Pro stock alert is set up correctly."]
    else:
        names = sorted({a.retailer for a in alerts})
        subject = f"PS5 Pro IN STOCK: {', '.join(names)}"
        lines = [
            f"{a.retailer}: {a.title or 'PS5 Pro'}"
            + (f" - £{a.price:.2f}" if a.price else "")
            + f"\n{a.url}"
            for a in alerts
        ]
    body = "\n\n".join(lines)
    sent = []

    for channel, fn in (("ntfy", _ntfy), ("email", _email), ("sms", _sms)):
        try:
            if fn(subject, body, alerts, test):
                sent.append(channel)
        except Exception as exc:  # one broken channel must not stop the others
            print(f"!! {channel} notification failed: {exc}", file=sys.stderr)
    return sent


def _ntfy(subject: str, body: str, alerts: list[Result], test: bool) -> bool:
    topic = (os.environ.get("NTFY_TOKEN") or "").strip().strip("/")  # the secret ntfy topic name
    if not topic:
        return False
    # Accept either the bare topic name or the full https://ntfy.sh/<topic> link.
    endpoint = topic if topic.startswith("http") else f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{topic}"
    headers = {"Priority": "urgent", "Tags": "video_game,rotating_light"}
    # One push per product so tapping the notification opens that product page.
    items = [None] if test else alerts
    for a in items:
        h = dict(headers)
        if a:
            h["Title"] = f"PS5 Pro in stock at {a.retailer}"
            h["Click"] = a.url
            msg = (a.title or "PS5 Pro") + (f" - £{a.price:.2f}" if a.price else "")
        else:
            h["Title"] = subject
            msg = body
        requests.post(endpoint, data=msg.encode("utf-8"), headers=h, timeout=20).raise_for_status()
    return True


def _email(subject: str, body: str, alerts: list[Result], test: bool) -> bool:
    user, password, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))
    if not (user and password and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or user
    msg["To"] = to
    msg.set_content(body + "\n\n-- PS5 Pro stock alert (GitHub Actions)")
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    return True


def _sms(subject: str, body: str, alerts: list[Result], test: bool) -> bool:
    sid, token, sender, to = (
        os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "SMS_TO")
    )
    if not (sid and token and sender and to):
        return False
    text = subject + "\n" + "\n".join(a.url for a in alerts[:3]) if alerts else subject
    for number in (n.strip() for n in to.split(",") if n.strip()):
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": sender, "To": number, "Body": text[:1500]},
            auth=(sid, token),
            timeout=20,
        ).raise_for_status()
    return True


# --------------------------------------------------------------------------- state & reporting

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"products": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def find_alerts(results: list[Result], state: dict, remind_hours: float) -> list[Result]:
    """Update state and return results that should trigger an alert.

    Only real in/out transitions are stored, so a blocked or failed check never
    resets a product and the state file only changes when stock actually moves.
    """
    now = datetime.now(timezone.utc)
    products = state.setdefault("products", {})
    alerts = []
    for r in results:
        if r.status not in ("in_stock", "out_of_stock"):
            continue
        prev = products.get(r.key)
        if r.status == "out_of_stock":
            if prev and prev.get("in_stock"):
                prev.update(in_stock=False, since=now.isoformat(timespec="minutes"))
            continue
        last_alert = prev and prev.get("last_alert")
        due = (
            not prev
            or not prev.get("in_stock")
            or not last_alert
            or now - datetime.fromisoformat(last_alert) >= timedelta(hours=remind_hours)
        )
        if due:
            alerts.append(r)
            products[r.key] = {
                "retailer": r.retailer,
                "title": r.title,
                "in_stock": True,
                "since": prev["since"] if prev and prev.get("in_stock") else now.isoformat(timespec="minutes"),
                "last_alert": now.isoformat(timespec="minutes"),
            }
    # Touch the file about once a month so GitHub doesn't disable the schedule
    # for inactivity (it does that to public repos after 60 days without commits).
    beat = state.get("heartbeat")
    if not beat or now - datetime.fromisoformat(beat) > timedelta(days=30):
        state["heartbeat"] = now.isoformat(timespec="minutes")
    return alerts


ICONS = {"in_stock": "🟢", "out_of_stock": "🔴", "unknown": "⚪", "blocked": "🚫", "error": "⚠️"}


def report(results: list[Result], alerts: list[Result], sent: list[str]) -> None:
    rows = sorted(results, key=lambda r: (r.status != "in_stock", r.retailer))
    lines = [
        "## PS5 Pro stock check",
        f"Checked {len(results)} pages at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
        f"New alerts: **{len(alerts)}**" + (f" (sent via {', '.join(sent)})" if sent else ""),
        "",
        "| | Retailer | Product | Price | Why |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        title = (r.title or r.url)[:70].replace("|", "/")
        price = f"£{r.price:.2f}" if r.price else ""
        lines.append(f"| {ICONS.get(r.status, '')} {r.status} | {r.retailer} | [{title}]({r.url}) | {price} | {r.reason} |")
    md = "\n".join(lines)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md + "\n")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="don't send notifications or save state")
    ap.add_argument("--only", help="only check retailers whose name contains this text")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    if args.test_notify:
        sent = notify([], test=True)
        print(f"Test notification sent via: {', '.join(sent) or 'NOTHING - no channels configured'}")
        return 0 if sent else 1

    config = yaml.safe_load(CONFIG_PATH.read_text())
    results = asyncio.run(check_all(config, args.only))
    state = load_state()
    alerts = find_alerts(results, state, config["settings"].get("remind_after_hours", 6))

    sent: list[str] = []
    if alerts and not args.dry_run:
        sent = notify(alerts)
    report(results, alerts, sent)
    if alerts and not sent and not args.dry_run:
        # Don't record the alert as sent, so the next run tries again.
        print("!! Stock found but no notification channel is configured or working", file=sys.stderr)
        return 1
    if not args.dry_run and not args.only:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
