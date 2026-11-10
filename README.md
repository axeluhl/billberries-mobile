# Billberries Mobile

Phone-first alternative to the [BSC Karlsruhe Billberries calendar](https://billberries.de/reservation/billard/0001/):

- **Pick a time quickly:** opens on tonight (from 18:00) or the next day with free tables; tap through half-hour start times, move the interval, adjust its length.
- **Your preferred table is preselected:** the first free table matching your preferred **size**, then your preferred **cloth**; ties are broken by your own **ranking** (by default 17 before 15, which have the same size and cloth). Table sizes, cloths, both preferences and the ranking are editable under *Tables & preferences* and stored on the server (`state.json`), so all devices share them. Defaults: tables 15 and 17 are 2.84 × 1.42 m with Simonis, 16 is 2.84 × 1.42 m with Royal Pro, 13 and 14 are 2.10 × 1.05 m.
- **Your reservations:** the app learns your Billberries player name (e.g. “A. U.”) from the reservation form after each Billberries login, lists your upcoming reservations under *My reservations* with a one-tap **Cancel** (in-app confirmation), and marks the times you already play in the timeline.
- **No overlapping bookings:** the page doesn't offer times that overlap one of your reservations, and the server independently rejects a booking that overlaps one of yours on any table or any existing booking on the same table — Billberries itself would accept both.
- **Solo training or a game with a partner** in one step, without the original site's extra round trip.
- **Partner-search e-mails:** once a day the server looks for new reservations where another player is looking for a partner (*Suche Spielpartner*) and e-mails them to you, with player, date, time and table including its size and cloth. Days with your own reservations, other players' reservations and partner searches are marked in the date row.
- **Calendar e-mails:** new reservations made through this app are e-mailed with an importable `.ics` file and a calendar alarm, by default 60 minutes before the start. Change the reminder under *Tables & preferences*; it is saved on the server for all devices.
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

### What is sent, and when

- **Your reservations:** after Billberries confirms a new reservation made through this app, an e-mail is sent immediately to **`NOTIFY_TO`**, using the same SMTP configuration as the notifications below. It contains the booking's date, start/end time, players, table number, size and cloth, plus a **`reservation.ics` attachment** for import into a calendar. This is separate from Billberries' own confirmation e-mail and is not sent for bookings made on the original site or for old reservations found by the daily scan.
- **Partner searches:** the container loads the Billberries calendar **once a day** at `SCAN_TIME` (local time, Europe/Berlin) plus a new random delay of up to `SCAN_JITTER_MINUTES`. If it finds **new** future reservations by other players marked *Suche Spielpartner*, it sends **one** e-mail listing them: player name, weekday, date, time range, and table number with the size and cloth recorded under *Tables & preferences*, plus links to the app (`APP_URL`) and the original calendar.
- Each partner search is reported **only once**. If sending fails, it is not marked as reported and is retried at the next scan.
- The **first scan** after e-mail is configured reports **all** partner searches currently in the calendar; later scans only report new ones.
- **Login reminder:** if the scan finds that the server's Billberries login has expired, it sends **one** reminder e-mail per expiry (logging in again resets it).
- The next scan time is kept in `state.json`, so restarts don't shift it; a scan missed while the container was down runs right after startup.
- Without e-mail configuration the scan still runs and only logs what it found.

### Calendar attachment and reminder

Under **Tables & preferences → Calendar reminder: minutes before the reservation**, choose the alarm lead time. It defaults to **60 minutes**, accepts whole minutes from **0 to 10080** (seven days), and **0** means an alarm at the start. This is `calendarReminderMinutes` in `state.json`, not an environment variable, so changing it in the web UI needs no container restart. Existing installations pick up the 60-minute default without losing their other settings.

The attachment is a standard iCalendar event (`METHOD:PUBLISH`) with a `VALARM` display reminder. Import it into your calendar to get the alarm; **the calendar app triggers it, not this server**. Clients may override or ignore imported alarms depending on their settings and notification permissions. Event times use UTC instants corresponding to the Europe/Berlin reservation, so daylight-saving changes and other viewing time zones are handled correctly. The event also contains the table properties and players. Each reservation has a stable event UID; importing the same file repeatedly may still create duplicates in some calendar clients.

Changing the reminder only affects **future booking e-mails**; it cannot change files already sent or events already imported. The attachment is an export, **not a live calendar subscription**: if you cancel a booking, also remove the imported event yourself. No cancellation or update attachment is sent automatically.

An SMTP failure **does not undo a successful booking**. The app shows that the booking exists but its calendar e-mail could not be sent; **do not book again** to try to resend it. This immediate booking e-mail is not retried automatically. If the server cannot verify the exact booking, it sends no calendar event and warns you to check the original calendar. An email being accepted by SMTP does not guarantee delivery to the inbox.

### Where and how to configure

SMTP and recipient settings live in **`.env` next to `docker-compose.yml`** on the Docker host, in the same file as `PROXY_SECRET` and `ALLOWED_USER`. The file is excluded from Git; keep it readable only by its owner (`chmod 600 .env`). The calendar reminder is instead managed through the web UI and saved in `state.json`, as described above.

`docker-compose` copies the variables from `.env` into the container's environment **when it creates the container**, and the server reads them once at startup. After editing `.env`, run:

```sh
docker-compose up -d    # recreates the container with the new environment
```

`docker-compose restart` is **not** enough: it restarts the existing container with its old environment. Recreating is safe; the Billberries login, settings and scan schedule are kept in the `billberries-data` volume.

**Format** (docker-compose reads the file literally):

- one `NAME=value` per line, **no spaces** around `=`, **no quotes** (they would become part of the value);
- lines starting with `#` are comments;
- several recipients are separated by commas: `NOTIFY_TO=a@example.org,b@example.org`.

### Variables

| Variable | Default | Meaning |
|---|---|---|
| `NOTIFY_TO` | – | Recipient address(es) for booking/calendar e-mails, partner searches and login reminders, comma-separated. **Any address** – it doesn't have to be the one of the Google account used to log in to Billberries. |
| `SMTP_HOST` | – | SMTP server host name or IP address, as reachable **from inside the container** |
| `SMTP_PORT` | `587` | SMTP port: usually `587` with `starttls`, `465` with `ssl`, `25` for a relay without encryption |
| `SMTP_SECURITY` | `starttls` | `starttls` (upgrade to TLS after connecting), `ssl` (TLS from the start) or `none` (no encryption; only for a trusted relay in your own network) |
| `SMTP_USER`, `SMTP_PASSWORD` | – | SMTP login; leave both out for a relay that doesn't require one |
| `SMTP_FROM` | – | Sender address. Providers that require a login usually only accept your own address here. |
| `NOTIFY_LANG` | `de` | Language of the e-mails: `de` or `en` |
| `APP_URL` | – | Link to the app included in e-mails, e.g. `https://billberries.example.com/` |
| `SCAN_TIME` | `08:00` | Earliest daily scan time, `HH:MM`, Europe/Berlin |
| `SCAN_JITTER_MINUTES` | `180` | Maximum random delay after `SCAN_TIME`, chosen anew each day |

E-mail is **enabled only if `NOTIFY_TO`, `SMTP_HOST` and `SMTP_FROM` are all set**.

### Examples

Mail provider with login (recommended for external mailboxes; host and port from your provider's documentation; Gmail needs an *app password*, not the account password):

```
NOTIFY_TO=you@example.org
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=you@example.org
SMTP_PASSWORD=app-specific-password
SMTP_FROM=you@example.org
APP_URL=https://billberries.example.com/
NOTIFY_LANG=de
```

Mail relay in your own network that accepts mail from the Docker host without login (e.g. Exim or Postfix with the Docker host in its relay networks):

```
NOTIFY_TO=you@example.org
SMTP_HOST=mail.lan.example
SMTP_PORT=25
SMTP_SECURITY=none
SMTP_FROM=billberries@example.org
APP_URL=https://billberries.example.com/
```

A relay that delivers directly from a residential internet connection is often rejected or filed as spam by large providers; it works best for addresses the relay delivers itself. Check the spam folder after the first test.

### Verify

Open the app, expand **Tables & preferences → E-mail notifications**. It shows the recipient(s), the last and the next check. **Send test e-mail** sends a short message immediately and shows the SMTP error if it fails; **Check now** runs a scan right away (and e-mails new partner searches). The container log also records scans and sending errors: `docker logs billberries-mobile`.

### Security

`SMTP_PASSWORD` is stored in plain text in `.env` and is visible to anyone who can run `docker inspect` on the Docker host (root and the `docker` group). Prefer an app-specific password that can be revoked independently, never put it into Git, and don't use the password of your Google account.

## Limitations

Billberries has no public API; the app reads its server-rendered HTML and posts its reservation form. If Billberries changes that markup, the app stops rather than guessing. After a booking or cancellation the calendar is reloaded to confirm the result; on an ambiguous response, check the original calendar before trying again. Every booking and cancellation triggers Billberries' confirmation emails. Times use Europe/Berlin. The calendar covers about 14 days.

## Tests

Run `python3 -m unittest discover -s tests -v`. The tests use fake calendars, isolated temporary state files and mocked SMTP/upstream requests. They never create real reservations or send real mail. They cover reminder settings and migration, iCalendar escaping/line folding and daylight-saving time, MIME attachments, verified booking submission, and failures that must not trigger a repeat booking.
