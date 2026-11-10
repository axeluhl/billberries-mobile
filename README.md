# Billberries Mobile

Phone-first alternative to the [BSC Karlsruhe Billberries calendar](https://billberries.de/reservation/billard/0001/). Pick a day, tap through half-hour start times, move the interval, adjust its length, and choose a free table. Defaults to **2.84 × 1.42 m / Simonis** (tables 15 and 17); table 16 has Royal Pro, tables 13 and 14 are 2.10 × 1.05 m. Table attributes and preferences are editable in the app and stored in the browser. Book solo training or a game with a second player; inspect bookings and cancel your own.

## Architecture

```
Phone ──HTTPS + Basic Auth──▶ reverse proxy (e.g. Apache, billberries.example.com)
      ──HTTP + proxy secret──▶ docker-host:3040 ──▶ container ──HTTPS──▶ billberries.de
```

- **`server.py`** (Python 3, standard library only) serves `index.html` and a narrow API: read the calendar, open the reservation form, create or cancel a reservation, and log in to Billberries. It is not a general proxy; reservation form fields are allow-listed.
- **Who may use it:** the reverse proxy authenticates one user (Basic Auth). It removes the `Authorization` header and instead sends a shared secret (`X-BBM-Proxy-Secret`) and the authenticated user name (`X-BBM-Remote-User`). The container refuses every request without the right secret and user (`PROXY_SECRET`, `ALLOWED_USER`), so direct LAN access to its port cannot book. API calls additionally require an `X-Requested-With` header and a same-origin request, because browsers attach Basic Auth credentials to cross-site requests too.
- **How it talks to Billberries:** the container stores **one Billberries session cookie** for the user's account in the Docker volume `billberries-data` (`state.json`, mode 0600). It never sees a Google password.

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

Any browser signed into the Google account works for this, including a desktop. Billberries does **not** renew its session cookie on use, so a session lasts Billberries' fixed maximum (Flask's default is 31 days; their actual setting is unknown). The footer shows since when the session exists; the container checks it every few hours (`KEEPALIVE_HOURS`) and the app asks for a new login once Billberries rejects it. **Billberries-Anmeldung entfernen** deletes the stored session.

## Limitations

Billberries has no public API; the app reads its server-rendered HTML and posts its reservation form. If Billberries changes that markup, the app stops rather than guessing. After a booking or cancellation the calendar is reloaded to confirm the result; on an ambiguous response, check the original calendar before trying again. Every booking and cancellation triggers Billberries' confirmation emails. Times use Europe/Berlin. The calendar covers about 14 days.
