#!/usr/bin/env python3
"""PS5 Pro UK stock alert.

Checks the retailers in retailers.yaml with a headless Chromium browser and sends
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
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
import yaml
from playwright.async_api import Page, async_playwright

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
    r"request unsuccessful|unusual traffic|enter the characters you see|bot protection)",
    re.I,
)


# --------------------------------------------------------------------------- models

@dataclass
class Result:
    retailer: str
    url: str
    status: str  # in_stock | out_of_stock | unknown | blocked | error
    title: str = ""
    price: float | None = None
    reason: str = ""


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
        name = name or str(p.get("name") or "")
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


# --------------------------------------------------------------------------- browser work

async def load(page: Page, url: str, timeout: int) -> int | None:
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass  # many retail sites never go fully idle
    return resp.status if resp else None


async def check_product(run: Run, ctx, retailer: dict, url: str, link_title: str = "") -> Result:
    if url in run.checked:
        return run.checked[url]
    name = retailer["name"]
    settings = run.settings
    async with run.sem:
        page = await ctx.new_page()
        try:
            status_code = await load(page, url, settings.get("timeout", 45))
            text = await page.inner_text("body")
            title = await page.title()
            blocks = await page.eval_on_selector_all(
                'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
            )
            buttons = 0
            for loc in await page.get_by_role("button", name=BUY_BUTTON).all():
                try:
                    if await loc.is_visible() and await loc.is_enabled():
                        buttons += 1
                except Exception:
                    pass
        except Exception as exc:
            res = Result(name, url, "error", link_title, reason=f"{type(exc).__name__}: {str(exc)[:120]}")
            run.checked[url] = res
            return res
        finally:
            await page.close()

    avail, prices, product_name = parse_structured_data(blocks)
    title = product_name or link_title or title
    price = min(prices) if prices else None

    if (status_code and status_code in (403, 429, 503)) or (
        not avail and buttons == 0 and BLOCKED_TEXT.search(text[:5000])
    ):
        res = Result(name, url, "blocked", title, price, f"HTTP {status_code} / bot check")
    else:
        status, reason = decide(avail, buttons, text)
        res = Result(name, url, status, title, price, reason)
        if status == "in_stock":
            req = retailer.get("require_text")
            if req and req.lower() not in re.sub(r"\s+", " ", text).lower():
                res.status, res.reason = "out_of_stock", f'in stock but "{req}" not on page'
            elif not price_ok(price, settings):
                res.status, res.reason = "out_of_stock", f"in stock but price £{price:.2f} outside range"
    run.checked[url] = res
    return res


async def check_search(run: Run, ctx, retailer: dict) -> list[Result]:
    settings = run.settings
    url = retailer["url"]
    async with run.sem:
        page = await ctx.new_page()
        try:
            status_code = await load(page, url, settings.get("timeout", 45))
            links = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map(a => ({
                    href: a.getAttribute('href'),
                    text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title')
                           || (a.querySelector('img') && a.querySelector('img').alt) || '').trim()
                }))""",
            )
            text = await page.inner_text("body")
        except Exception as exc:
            return [Result(retailer["name"], url, "error", reason=f"{type(exc).__name__}: {str(exc)[:120]}")]
        finally:
            await page.close()

    title_re = re.compile(settings["title_pattern"], re.I)
    exclude_re = re.compile(settings["exclude_pattern"], re.I)
    link_re = re.compile(retailer["link_pattern"]) if retailer.get("link_pattern") else None
    base_host = urlsplit(url).netloc.removeprefix("www.")

    found: dict[str, str] = {}
    for link in links:
        href, label = link["href"] or "", " ".join(link["text"].split())
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
        if status_code in (403, 429, 503) or BLOCKED_TEXT.search(text[:5000]):
            status, reason = "blocked", f"HTTP {status_code} / bot check"
        elif status_code and status_code >= 400:
            status, reason = "error", f"HTTP {status_code} - search URL may have changed"
        else:
            status, reason = "unknown", "no PS5 Pro listings found on search page"
        return [Result(retailer["name"], url, status, reason=reason)]
    return list(await asyncio.gather(*(check_product(run, ctx, retailer, u, t) for u, t in found.items())))


async def check_all(config: dict, only: str | None) -> list[Result]:
    run = Run(settings=config["settings"])
    retailers = [r for r in config["retailers"] if r.get("enabled", True)]
    if only:
        retailers = [r for r in retailers if only.lower() in r["name"].lower()]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            locale="en-GB",
            timezone_id="Europe/London",
            viewport={"width": 1366, "height": 900},
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
        )
        await ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        # Skip heavy assets - we only need the HTML/JS.
        await ctx.route(
            re.compile(r"\.(png|jpe?g|gif|webp|avif|svg|woff2?|ttf|mp4|webm)(\?|$)", re.I),
            lambda route: route.abort(),
        )

        async def one(r: dict) -> list[Result]:
            if r.get("type", "product") == "search":
                return await check_search(run, ctx, r)
            return [await check_product(run, ctx, r, r["url"])]

        nested = await asyncio.gather(*(one(r) for r in retailers))
        await browser.close()

    # De-duplicate (the same product can be found by several entries).
    seen, results = set(), []
    for res in (r for group in nested for r in group):
        if res.url not in seen:
            seen.add(res.url)
            results.append(res)
    return results


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
        prev = products.get(r.url)
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
            products[r.url] = {
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
