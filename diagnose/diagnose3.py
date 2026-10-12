#!/usr/bin/env python3
"""Round 3 diagnostics: retest every retailer that isn't fully working yet.

Adds alternative URLs for each site and, for Amazon, sets a UK delivery
postcode through Amazon's own address API instead of clicking the menu.
Raw pages go to out/<method>/<target>.html and are published to the
`diag3-output` branch by the workflow.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
import diagnose as d  # noqa: E402
import diagnose2 as d2  # noqa: E402

TARGETS = {
    # Argos: round 2/live runs got real Argos pages back occasionally
    "argos_4440006": "https://www.argos.co.uk/product/4440006",
    "argos_7797426": "https://www.argos.co.uk/product/7797426",
    "argos_4350000": "https://www.argos.co.uk/product/4350000",
    "argos_search": "https://www.argos.co.uk/search/playstation-5-pro/",
    # Very / Littlewoods
    "very_1601238401": "https://www.very.co.uk/playstation-playstationreg5-pro-console-2tb-with-grand-theft-auto-v-amp-disc-drive/1601238401.prd",
    "very_search": "https://www.very.co.uk/search/playstation%205%20pro",
    "very_category": "https://www.very.co.uk/gaming/playstation-5/consoles/e/b/120318.end",
    "littlewoods_search": "https://www.littlewoods.com/search/playstation%205%20pro",
    # John Lewis (connection drops in Firefox)
    "johnlewis_search": "https://www.johnlewis.com/search?search-term=playstation%205%20pro%20console",
    "johnlewis_product": "https://www.johnlewis.com/playstation-5-pro-console-with-dualsense-controller/p114409149",
    # Ebuyer / Scan: pages load but listings/stock weren't read
    "ebuyer_search": "https://www.ebuyer.com/searchresults?descriptionfilter=playstation+5+pro",
    "scan_product": "https://www.scan.co.uk/products/sony-playstation-ps5-pro-b-chassis-slim-gaming-console-high-speed-2tb-ssd-ray-tracing-hdr",
    "scan_search": "https://www.scan.co.uk/search?q=playstation+5+pro+console",
    # Geo-blocked (GAME, ShopTo) and Cloudflare (Currys, AO, Hughes)
    "game_home": "https://www.game.co.uk/",
    "game_ps5": "https://www.game.co.uk/en/playstation/consoles/ps5/",
    "shopto_product": "https://www.shopto.net/en/ps5hw93-playstation5-pro-console-p1182475/",
    "currys_product": "https://www.currys.co.uk/products/sony-playstation-5-pro-10272138.html",
    "ao_home": "https://ao.com/",
    "hughes_home": "https://www.hughes.co.uk/",
    # Supermarkets (intermittent)
    "tesco_search": "https://www.tesco.com/groceries/en-GB/search?query=playstation%205%20pro",
    "asda_search": "https://www.asda.com/groceries/search/playstation%205%20pro",
    # Amazon
    "amazon": "https://www.amazon.co.uk/dp/B0DJH2NS6R",
}
d.TARGETS = TARGETS

AMAZON_POSTCODE_JS = """async (postcode) => {
    const out = [];
    // The location modal's own CSRF token is embedded in the nav bar.
    const holder = document.querySelector('#nav-global-location-data-modal-action');
    let token = '';
    if (holder) {
        const m = (holder.getAttribute('data-a-modal') || '').match(/anti-csrftoken-a2z"\\s*:\\s*"([^"]+)/);
        if (m) token = m[1];
    }
    out.push('nav token: ' + (token ? 'yes' : 'no'));
    const r1 = await fetch('/portal-migration/hz/glow/get-rendered-address-selections?deviceType=desktop&pageType=Detail&storeContext=NoStoreName&actionSource=desktop-modal',
        {credentials: 'include', headers: token ? {'anti-csrftoken-a2z': token} : {}});
    const html = await r1.text();
    const m2 = html.match(/CSRF_TOKEN\\s*[:=]\\s*["']([^"']+)/) || html.match(/anti-csrftoken-a2z["']?\\s*[:=]\\s*["']([^"']+)/);
    out.push('modal ' + r1.status + ', token: ' + (m2 ? 'yes' : 'no'));
    const csrf = m2 ? m2[1] : token;
    const r2 = await fetch('/portal-migration/hz/glow/address-change?actionSource=glow', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json', 'anti-csrftoken-a2z': csrf},
        body: JSON.stringify({locationType: 'LOCATION_INPUT', zipCode: postcode, deviceType: 'web',
                              storeContext: 'generic', pageType: 'Detail', actionSource: 'glow'})});
    out.push('change ' + r2.status + ': ' + (await r2.text()).slice(0, 150));
    return out.join(' | ');
}"""


def m_amazon_api():
    """Camoufox; set a UK postcode through Amazon's address API, then load the product."""
    with d2._camoufox() as browser:
        ctx = browser.new_context(locale="en-GB", timezone_id="Europe/London")
        page = ctx.new_page()
        page.goto("https://www.amazon.co.uk/dp/B0DJH2NS6R", wait_until="domcontentloaded", timeout=45000)
        try:
            cont = page.get_by_role("button", name="Continue shopping")
            if cont.count():
                cont.first.click()
                page.wait_for_load_state("domcontentloaded")
            if page.query_selector("#sp-cc-accept"):
                page.click("#sp-cc-accept")
            print("[amazon_api]", page.evaluate(AMAZON_POSTCODE_JS, "SW1A 1AA"), flush=True)
        except Exception as exc:
            print(f"[amazon_api] postcode step failed: {exc}", flush=True)
        page.close()
        yield from d2._browse(ctx, {k: v for k, v in d.TARGETS.items() if k == "amazon"})


METHODS = {
    "curl_ios": d2.m_curl_ios,
    "translate": d2.m_translate,
    "jina": d.m_jina,
    "webkit_iphone": d2.m_webkit_iphone,
    "firefox_wait": d2.m_firefox_wait,
    "camoufox_wait": d2.m_camoufox_wait,
    "amazon_api": m_amazon_api,
}
d.METHODS = METHODS


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--method":
        d.run_method_resilient(sys.argv[2])
        return
    methods = sys.argv[2].split(",") if len(sys.argv) > 2 else list(METHODS)
    procs = [subprocess.Popen([sys.executable, __file__, "--method", m]) for m in methods]
    for p in procs:
        try:
            p.wait(timeout=30 * 60)
        except subprocess.TimeoutExpired:
            p.kill()
    d.summary(methods)


if __name__ == "__main__":
    main()
