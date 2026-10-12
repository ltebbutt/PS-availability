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

## Retailers checked

Amazon UK (only when sold by Amazon), Argos (also covers Sainsbury's, which sells
consoles through Argos), Currys, Smyths Toys, Very, Littlewoods, John Lewis, GAME,
PlayStation Direct, Tesco, Asda, Costco UK, AO.com, Ebuyer, Scan, ShopTo, Box,
Hughes and EE.

Most entries are **search pages**. The checker finds every PS5 Pro console
listing on them (new bundles included) and then checks each product page. To
decide whether a product is in stock it uses the page's structured data
(`schema.org` availability), whether an "Add to basket" button can be clicked,
and wording like "Out of stock".

It ignores:
- listings priced above £900, which filters out marketplace scalpers
- listings priced below £400, which filters out accessories
- covers, stands and similar accessories

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
3. Add the secret `NTFY_TOPIC` with that exact topic name.
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
- **Some sites block GitHub's servers.** Retailers such as Amazon, Argos, Currys
  and Very use bot protection, and GitHub's runners are datacentre IPs in the US.
  Some checks will therefore show 🚫 *blocked*. A blocked check never sends a
  false alarm or a false "out of stock". It is just skipped. For the best
  coverage, run the workflow on a **self-hosted runner** at home: change
  `runs-on: ubuntu-latest` to `runs-on: self-hosted` and set up a runner on any
  always-on PC or Raspberry Pi
  (Settings → Actions → Runners → New self-hosted runner). Your home UK IP is
  far less likely to be blocked.
- **Product URLs change.** Search-page entries keep finding new listings by
  themselves, but if a retailer's search URL changes it will show ⚠️ in the
  summary. Update it in `retailers.yaml`.
- `state.json` is committed by the workflow only when stock actually changes,
  plus a heartbeat about once a month. GitHub disables schedules in repos with
  no activity for 60 days, and the heartbeat stops that happening.

## Running it locally

```bash

pip install -r requirements.txt
python -m playwright install chromium
python stock_alert.py --dry-run          # check everything, print results, no alerts
python stock_alert.py --dry-run --only smyths
NTFY_TOPIC=your-topic python stock_alert.py --test-notify
```
