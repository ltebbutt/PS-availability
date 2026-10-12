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

**1. Stock trackers.** [StockInformer](https://www.stockinformer.co.uk/checker-playstation-5-pro-console)
and [Stock Goblin](https://www.stockgoblin.co.uk/products/playstation-5-pro-console)
already monitor the big retailers from UK connections. Each retailer row on
their pages becomes its own alert. This is how **Currys, Argos, GAME, ShopTo
and Amazon** are covered, because those sites block or geo-restrict GitHub's
US servers. Resale sites (StockX, eBay) are ignored.

**2. Direct checks of retailer sites.** Many UK retailers use anti-bot
protection, so each site is fetched with the scraper that got through in
testing (see `diagnose/`):

| Retailer | Method | Status from GitHub (Oct 2026) |
|---|---|---|
| Smyths Toys | Camoufox (stealth Firefox) on a virtual screen | ✅ works |
| Box | Firefox | ✅ works |
| EE | Chromium | ✅ works |
| PlayStation Direct | Chromium | ✅ works |
| John Lewis | Firefox | ✅ works (occasional connection drops) |
| Tesco, Asda | Firefox | ⚠️ intermittent; neither sells the console online right now |
| Costco | Chromium | ✅ loads; doesn't list the PS5 Pro right now |
| Very, Littlewoods | iPhone WebKit / iPhone curl | ⚠️ intermittent; also covered by the trackers |
| Argos | iPhone WebKit / Camoufox | ❌ usually blocked; trackers show its status |
| Amazon | Camoufox | ❌ shows US availability; covered by the trackers |
| Scan | r.jina.ai reader | ⚠️ page loads but stock isn't readable yet |
| Ebuyer | Firefox | ⚠️ search page loads, listings not picked up yet |
| Currys, AO, Hughes | none worked | ❌ Currys covered by the trackers |
| GAME, ShopTo | none worked | ❌ block non-UK visitors; covered by the trackers |

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
- **GitHub's schedule is best-effort.** At busy times a "5 minute" schedule can
  slip to 10–20 minutes. Bots on stock-tracking Discord servers will sometimes
  be faster.
- **Some sites still block GitHub's servers** (see the table above). GitHub's
  runners are datacentre machines in the US, so Currys, Argos, GAME and ShopTo
  refuse them or treat them as overseas visitors. The trackers cover those. For
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
