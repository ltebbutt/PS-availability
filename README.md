# PS5 Pro stock alert (UK)

A GitHub Action that checks UK retailers for PS5 Pro stock every 5 minutes and
alerts your iPhone when one comes back in stock. It can send:

| Channel | Cost | What you get on the iPhone |
|---|---|---|
| **ntfy push** (recommended) | Free | Instant push notification. Tap it to open the product page. |
| **Email** (Gmail) | Free | Normal Mail app notification. |
| **SMS text** (Twilio) | About £1/month for the number plus ~4p per text | A real text message. |

You can turn on any mix of these. You get one alert per restock, not one every
5 minutes. If the item is still in stock 6 hours later you get a reminder.

## What's checked, and how

Two kinds of source are checked every run:

**1. A stock tracker.** [Stock Goblin](https://www.stockgoblin.co.uk/products/playstation-5-pro-console)
monitors the big retailers from UK connections. It is used only for retailers we
can't read directly (Currys, GAME, ShopTo, Amazon), and its alerts say
*"reported by Stock Goblin, not confirmed on the retailer's own site"*. Whenever
we *can* read the retailer's own page, that answer overrides the tracker.
Resale sites (StockX, eBay) are ignored. (StockInformer was tried and removed:
it reported Box, GAME and Currys in stock when they weren't.)

**2. Direct checks of retailer sites.** Many UK retailers use anti-bot
protection, so each site is fetched with the scraper that got through in
testing (see `diagnose/`):

| Retailer | Methods tried in order | Status from GitHub (Oct 2026) |
|---|---|---|
| Argos (and Sainsbury's) | iPhone curl → Camoufox → iPhone WebKit | ✅ reads Argos's own `globallyOutOfStock` flag when not blocked (varies by run) |
| Smyths Toys | Camoufox (stealth Firefox) on a virtual screen | ✅ works |
| John Lewis | iPhone curl → Firefox → r.jina.ai reader | ✅ works |
| Box | Firefox → iPhone curl | ✅ works |
| EE | Chromium | ✅ works |
| PlayStation Direct | Chromium | ✅ works |
| Very | iPhone curl → iPhone WebKit | ⚠️ works on some runs, blocked on others |
| GAME | iPhone curl → Camoufox → Firefox | ⚠️ pages load; PS5 Pro listing not found yet |
| Hughes | iPhone curl → Firefox → Camoufox | ⚠️ homepage loads, search often blocked |
| Tesco, Asda, Costco, Ebuyer | various | ✅ load; none of them list the PS5 Pro right now |
| Scan | r.jina.ai reader → Google Translate proxy | ⚠️ loads, but stock is added later by JavaScript |
| Currys, Amazon, ShopTo | none worked directly | ❌ covered by Stock Goblin only |
| Littlewoods, AO | none worked | ❌ blocked in every test |

A product counts as in stock if its structured data (`schema.org`) says so, or
a clickable "Add to basket" button is showing. Out-of-stock wording counts
against it. Blocked or redirected pages are skipped, so they never send a false
alert. The same product seen by several sources only alerts once.

It ignores:
- listings priced above £900, which filters out marketplace scalpers
- listings priced below £400, which filters out accessories
- covers, stands and similar accessories, plus refurbished or "good as new" units

All of this can be changed in [`retailers.yaml`](retailers.yaml).

## Setup (about 10 minutes)

### 1. Choose how you want to be alerted, then add the GitHub secrets

Go to the repo on GitHub, then **Settings → Secrets and variables → Actions → New
repository secret**. Add the secrets for the channels you want.

**ntfy push notifications (free, recommended)**
1. Install the **ntfy** app from the App Store.
2. Open it, tap **+**, and subscribe to a topic with a long, unguessable name such
   as `ps5pro-alert-8f3k29xq`. Anyone who knows the name can read the topic, so
   don't use something guessable.
3. Add the secret `NTFY_TOKEN` with that exact topic name.
4. In iPhone **Settings → Notifications → ntfy**, set alerts to Immediate/Time
   Sensitive so they get through Focus modes.

**Email (Gmail)**
1. Turn on 2-Step Verification for your Google account, then create an **App
   Password** at https://myaccount.google.com/apppasswords.
2. Add these secrets:
   - `SMTP_USER`: your Gmail address
   - `SMTP_PASSWORD`: the 16-character app password, not your normal password
   - `EMAIL_TO`: where to send alerts. Your own address is fine. Separate several addresses with commas.

   If you don't use Gmail, also set `SMTP_HOST` and `SMTP_PORT`. Port 465 uses
   SSL and anything else uses STARTTLS.

**SMS text messages (Twilio, paid)**
1. Sign up at https://www.twilio.com and buy a UK number, or use an Alphanumeric Sender ID.
2. Add these secrets:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_FROM`: your Twilio number, e.g. `+447...`
   - `SMS_TO`: your mobile number, e.g. `+447...`

   Trial accounts can only text numbers you have verified with Twilio.

### 2. Send a test alert
Go to **Actions → PS5 Pro stock alert → Run workflow**, tick **"Just send a test
alert"**, and click Run. You should get a notification on every channel you set
up within a few seconds.

### 3. That's it
The schedule starts by itself once the workflow is on the default branch
(`main`). Each run's **Summary** tab shows a table of every retailer and product
that was checked:

- 🟢 in stock
- 🔴 out of stock
- 🚫 blocked by the site's bot protection
- ⚪ ⚠️ couldn't tell

Check the summary now and then to see whether a retailer needs its URL updating.

## Good to know

- **The repo must stay public** for this to be free, because public repos get
  unlimited Actions minutes. In a private repo, a run every 5 minutes would use
  up the 2,000 free minutes in about a week. Your secrets stay encrypted either
  way.
- **How the 5-minute checks keep running.** GitHub's built-in schedule is
  best-effort and can be late or skip runs, so each run also queues the next one
  for 5 minutes after it started. The schedule is kept as a backup that restarts
  the chain if it ever stops. GitHub never runs more than one check at a time
  plus one waiting. Scheduled runs act as whoever last edited the `cron:` line,
  so edit `.github/workflows/ps5-stock-alert.yml` from your own GitHub account
  (e.g. in the web editor).
- **Some sites still block GitHub's servers** (see the table above). GitHub's
  runners are datacentre machines in the US, so Currys, Argos, GAME and ShopTo
  refuse them or treat them as overseas visitors. Stock Goblin covers some of those. For
  direct checks of every site, run the workflow on a **self-hosted runner** at
  home: change `runs-on: ubuntu-latest` to `runs-on: self-hosted` and set up a
  runner on any always-on PC or Raspberry Pi
  (Settings → Actions → Runners → New self-hosted runner). A home UK connection
  is far less likely to be blocked.
- **Dry runs.** **Run workflow** with **"Check every site but don't send
  alerts"** ticked shows the full results table in the run's Summary tab without
  alerting you.
- **Re-testing scrapers.** The **Scraping diagnostics** workflows (manual only)
  try every site with about ten different methods and save the raw pages to the
  `diag-output` / `diag2-output` branches. Use them if a site starts failing.
- **Product URLs change.** Search-page entries keep finding new listings by
  themselves, but if a retailer's search URL changes it will show ⚠️ in the
  summary. Update it in `retailers.yaml`.
- `state.json` is committed by the workflow only when stock actually changes,
  plus a heartbeat about once a month. GitHub disables schedules in repos with
  no activity for 60 days, and the heartbeat stops that happening.

## Running it locally

```bash

pip install -r requirements.txt
python -m playwright install chromium firefox webkit
python -m camoufox fetch
python stock_alert.py --dry-run          # check everything, print results, no alerts
python stock_alert.py --dry-run --only smyths
NTFY_TOKEN=your-topic python stock_alert.py --test-notify
```
