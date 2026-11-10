# Billberries Mobile

Phone-first alternative to the [BSC Karlsruhe Billberries calendar](https://billberries.de/reservation/billard/0001/):

- **Pick a time quickly:** opens on tonight (from 18:00) or the next day with free tables; tap through half-hour start times, move the interval, adjust its length.
- **Your preferred table is preselected:** the first free table matching your preferred **size**, then your preferred **cloth**; ties are broken by your own **ranking** (by default 17 before 15, which have the same size and cloth). Table sizes, cloths, both preferences and the ranking are editable under *Tables & preferences* and stored on the server (`state.json`), so all devices share them. Defaults: tables 15 and 17 are 2.84 × 1.42 m with Simonis, 16 is 2.84 × 1.42 m with Royal Pro, 13 and 14 are 2.10 × 1.05 m.
- **Your reservations:** the app learns your Billberries player name (e.g. “A. U.”) from the reservation form after each Billberries login, lists your upcoming reservations under *My reservations* with a one-tap **Cancel** (in-app confirmation), and marks the times you already play in the timeline.
- **No overlapping bookings:** the page doesn't offer times that overlap one of your reservations, and the server independently rejects a booking that overlaps one of yours on any table or any existing booking on the same table — Billberries itself would accept both.
- **Solo training or a game with a partner** in one step, without the original site's extra round trip.
- **Partner-search e-mails:** once a day the server looks for new reservations where another player is looking for a partner (*Suche Spielpartner*) and e-mails them to you, with player, date, time and table including its size and cloth. Days with your own reservations, other players' reservations and partner searches are marked in the date row.
- **Languages:** English, German, French, Spanish, Italian, Portuguese and Dutch, following the browser's language; the chooser in the header overrides it per device. Dates and times use the chosen locale.

## Architecture

```
Phone ──HTTPS + Basic Auth──▶ reverse proxy (e.g. Apache, billberries.example.com)
      ──HTTP + proxy secret──▶ docker-host:3040 ──▶ container ──HTTPS──▶ billberries.de
```

- **`server.py`** (Python 3, standard library only) serves `index.html` and a narrow API: read the calendar, open the reservation form, create or cancel a reservation (rejecting overlaps), read and store table settings, return your Billberries player name, and log in to Billberries. It is not a general proxy; reservation form fields are allow-listed. Errors carry a stable `code` (e.g. `conflict-mine`) that the page translates.
- **Who may use it:** the reverse proxy authenticates one user (Basic Auth). It removes the `Authorization` header and instead sends a shared secret (`X-BBM-Proxy-Secret`) and the authenticated user name (`X-BBM-Remote-User`). The container refuses every request without the right secret and user (`PROXY_SECRET`, `ALLOWED_USER`), so direct LAN access to its port cannot book. API calls additionally require an `X-Requested-With` header and a same-origin request, because browsers attach Basic Auth credentials to cross-site requests too.
- **How it talks to Billberries:** the container stores **one Billberries session cookie** for the user's account, the detected player name, and the table settings in the Docker volume `billberries-data` (`state.json`, mode 0600). It never sees a Google password.

It is a single-user app: whoever passes the reverse proxy's authentication books in the name of the Billberries account the container is logged in with.

## Deployment

**Container** (tested with Docker 18.06 and docker-compose 1.29):

```sh
git clone https://github.com/axeluhl/billberries-mobile.git
cd billberries-mobile
umask 077
printf 'PROXY_SECRET=%s\nALLOWED_USER=alice\n' "$(head -c 32 /dev/urandom | base64 | tr -d '/+=')" > .env
docker-compose up -d --build
```

`ALLOWED_USER` must match the user name the reverse proxy authenticates. `docker-compose.yml` describes the one container: port 3040, the data volume, `restart: unless-stopped`, a 128 MB memory limit, a read-only root filesystem, no capabilities, `no-new-privileges`, and an unprivileged user. Update with `git pull && docker-compose up -d --build`.

**Reverse proxy:** see [docs/reverse-proxy-debian-apache.md](docs/reverse-proxy-debian-apache.md) for a complete Debian Apache setup: Basic Auth, sharing and rotating the proxy secret, and the Let's Encrypt certificate with `certbot --apache`. Any reverse proxy works that authenticates the user, sets the two `X-BBM-*` headers, removes `Authorization`, and preserves the `Host` header.

## Logging in to Billberries

Google only redirects to Billberries' registered address, `https://billberries.de/login/callback`. The app therefore asks Google to put the one-time code after the `#` in that address, which browsers never send to Billberries. Billberries shows *“User email not available or not verified by Google”* — **expected** — and the code stays in the address bar:

1. In the app, tap **Google-Anmeldung öffnen** and sign in (MFA if asked).
2. On the Billberries error page, copy the full address.
3. Back in the app, tap **Einfügen** (or paste into the field), then **Anmeldung abschließen**. The container exchanges the code with Billberries and stores the session.

Any browser signed into the Google account works for this, including a desktop. Billberries does **not** renew its session cookie on use, so a session lasts Billberries' fixed maximum (Flask's default is 31 days; their actual setting is unknown). The footer shows since when the session exists. The daily scan notices when Billberries rejects it and then sends one reminder e-mail (if e-mail is configured); the app asks for a new login as well. **Billberries-Anmeldung entfernen** deletes the stored session.

## E-mail notifications

The container loads the calendar **once a day** at `SCAN_TIME` (local time, Europe/Berlin) plus a new random offset of up to `SCAN_JITTER_MINUTES`. The next run is kept in `state.json`, so restarts don't shift it; a run missed while the container was down happens at startup. Each scan reports **new** reservations by other players marked *Suche Spielpartner* in one e-mail (each one only once; if sending fails, it is retried at the next scan). If the Billberries login has expired, the scan sends one reminder per expiry instead. *Tables & preferences* in the app shows the recipient and the last and next check, and offers **Check now** and **Send test e-mail**.

Configure it in `.env` next to `docker-compose.yml` (never committed), then `docker-compose up -d`:

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_TO` | – | Recipient(s), comma-separated. E-mail is off unless `NOTIFY_TO`, `SMTP_HOST` and `SMTP_FROM` are set. |
| `SMTP_HOST`, `SMTP_PORT` | –, `587` | SMTP server |
| `SMTP_SECURITY` | `starttls` | `starttls`, `ssl` (implicit TLS, usually port 465) or `none` (e.g. a relay in your own LAN on port 25) |
| `SMTP_USER`, `SMTP_PASSWORD` | – | Optional SMTP login |
| `SMTP_FROM` | – | Sender address |
| `NOTIFY_LANG` | `de` | E-mail language: `de` or `en` |
| `APP_URL` | – | Link to the app in e-mails, e.g. `https://billberries.example.com/` |
| `SCAN_TIME`, `SCAN_JITTER_MINUTES` | `08:00`, `180` | Daily scan time and maximum random delay |

## Limitations

Billberries has no public API; the app reads its server-rendered HTML and posts its reservation form. If Billberries changes that markup, the app stops rather than guessing. After a booking or cancellation the calendar is reloaded to confirm the result; on an ambiguous response, check the original calendar before trying again. Every booking and cancellation triggers Billberries' confirmation emails. Times use Europe/Berlin. The calendar covers about 14 days.
