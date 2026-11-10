# Reverse proxy on Debian Apache (generic)

This describes how to put Billberries Mobile behind an Apache 2.4 reverse proxy on Debian, with Basic Auth, a proxy secret and a Let's Encrypt certificate. Names used:

| Name | Meaning |
|---|---|
| `billberries.example.com` | public host name of the app |
| `proxy-host` | Debian machine running Apache, reachable from the internet on ports 80 and 443 |
| `docker-host` | machine running the container (may be the same as `proxy-host`); the container is published on port 3040 |
| `alice` | the one Basic Auth user allowed to use the app (the app books in this person's name) |

```
Browser ──HTTPS + Basic Auth──▶ proxy-host (Apache)
        ──HTTP + X-BBM-Proxy-Secret + X-BBM-Remote-User──▶ docker-host:3040 (container)
```

## Prerequisites

- **DNS:** an A (and optionally AAAA) record for `billberries.example.com` pointing to `proxy-host` (a wildcard record works too).
- **Firewall/NAT:** ports **80 and 443** on `proxy-host` reachable from the internet. Port 80 is required: Let's Encrypt validates the domain with an HTTP-01 challenge on port 80, both for the first certificate and every renewal.
- `proxy-host` can reach `docker-host:3040`.
- Packages: `apache2`, `certbot` and `python3-certbot-apache` (on old releases the plugin may be called `python-certbot-apache`).
- Apache modules:
  ```sh
  a2enmod proxy proxy_http headers ssl rewrite auth_basic authn_file authz_user
  ```
  (`auth_basic`, `authn_file` and `authz_user` are enabled by default on Debian.)
- A password file containing `alice`. Either reuse an existing one, or create one:
  ```sh
  htpasswd -c -B /etc/apache2/billberries.htpasswd alice   # omit -c if the file exists
  ```

## 1. The proxy secret

`docker-host:3040` is reachable from the whole LAN. The container therefore only answers requests that carry a secret header that only Apache knows, plus the user name Apache authenticated. Both sides store the same random value:

| | `proxy-host` (Apache) | `docker-host` (container) |
|---|---|---|
| File | `/etc/apache2/billberries-proxy-secret.conf`, `root:root`, mode `0600` | `.env` next to `docker-compose.yml`, mode `0600`, never committed |
| Content | `RequestHeader set X-BBM-Proxy-Secret "<secret>"` | `PROXY_SECRET=<secret>` and `ALLOWED_USER=alice` |
| Used by | the vhost via `Include` (Apache reads configuration as root before dropping privileges, so the file can stay root-only) | `docker-compose` passes `.env` to the container as environment variables |

Generate the secret on `docker-host` and copy it to `proxy-host` without printing or storing it elsewhere (run from a machine with SSH access to both; the secret is alphanumeric, so it is safe inside the quoted Apache directive):

```sh
ssh docker-host 'cd /path/to/billberries-mobile && umask 077 &&
  printf "PROXY_SECRET=%s\nALLOWED_USER=alice\n" "$(head -c 32 /dev/urandom | base64 | tr -d "/+=")" > .env'

ssh docker-host 'sed -n "s/^PROXY_SECRET=//p" /path/to/billberries-mobile/.env' \
  | ssh root@proxy-host 'umask 077; read -r s;
      printf "RequestHeader set X-BBM-Proxy-Secret \"%s\"\n" "$s" > /etc/apache2/billberries-proxy-secret.conf'
```

`ALLOWED_USER` must be exactly the Basic Auth user name. The container refuses all requests (except `/healthz`) if `PROXY_SECRET` or `ALLOWED_USER` is missing.

**Rotation:** repeat both commands, then `docker-compose up -d` on `docker-host` (recreates the container so it reads the new environment) and `apache2ctl configtest && apache2ctl graceful` on `proxy-host`.

**Accepted limitation:** between `proxy-host` and `docker-host` the secret and all traffic travel unencrypted. Anyone able to sniff that network segment could copy the secret. If both run on the same machine, publish the port on `127.0.0.1` only (`"127.0.0.1:3040:8080"` in `docker-compose.yml`) and proxy to `http://127.0.0.1:3040/`.

## 2. Port-80 vhost, initially empty

`certbot --apache` creates the HTTPS vhost by **copying** an existing port-80 vhost for the same `ServerName`. Create a deliberately empty one first:

- Basic Auth or a `ProxyPass` in it would intercept the HTTP-01 challenge under `/.well-known/acme-challenge/`, and validation would fail.
- Whatever it contains is copied into the HTTPS vhost and is live there between certificate issuance and your edit in step 4. An empty document root exposes nothing in that window.

```sh
mkdir -p /var/www/billberries-empty
```

`/etc/apache2/sites-available/billberries.conf`:

```apache
<VirtualHost *:80>
    ServerName billberries.example.com
    # Empty until certbot adds the HTTPS redirect; the app is served only by billberries-le-ssl.conf.
    DocumentRoot /var/www/billberries-empty
    ErrorLog ${APACHE_LOG_DIR}/billberries-error.log
    CustomLog ${APACHE_LOG_DIR}/billberries-access.log common
</VirtualHost>
```

```sh
a2ensite billberries        # or: ln -s ../sites-available/billberries.conf /etc/apache2/sites-enabled/
apache2ctl configtest && apache2ctl graceful
```

## 3. Let's Encrypt certificate

```sh
certbot --apache -d billberries.example.com --redirect
```

certbot (with an existing account; add `--email` and `--agree-tos` on first use):

1. answers the HTTP-01 challenge through a temporary Apache configuration on port 80;
2. stores the certificate under `/etc/letsencrypt/live/billberries.example.com/`;
3. writes `/etc/apache2/sites-available/billberries-le-ssl.conf` (a copy of the port-80 vhost plus the `SSLCertificate*` lines and `Include /etc/letsencrypt/options-ssl-apache.conf`) and enables it;
4. adds an HTTP→HTTPS redirect (`RewriteRule … [END,NE,R=permanent]`) to the port-80 vhost.

**Renewal** is automatic: Debian's `certbot.timer` (or `/etc/cron.d/certbot`) runs `certbot renew` twice a day and renews certificates within 30 days of expiry, again via the Apache plugin and port 80. Renewal only replaces the files under `/etc/letsencrypt/live/…`, which the vhost references; your edits from step 4 are kept. Test with `certbot renew --dry-run`. The port-80 vhost must keep existing with its `ServerName` for renewals to work.

## 4. HTTPS vhost: Basic Auth, trust headers, proxy

Replace the document root in `billberries-le-ssl.conf` with the proxy configuration, keeping certbot's three certificate lines:

```apache
<IfModule mod_ssl.c>
<VirtualHost *:443>
    ServerName billberries.example.com
    ErrorLog ${APACHE_LOG_DIR}/billberries-error.log
    CustomLog ${APACHE_LOG_DIR}/billberries-access.log common

    # Only alice may use the app: it books and cancels in alice's name.
    <Location />
        AuthType Basic
        AuthName "Billberries Mobile"
        AuthUserFile /etc/apache2/billberries.htpasswd
        Require user alice
    </Location>

    # These run in the fixup phase, i.e. after authentication succeeded.
    # Don't forward the Basic Auth password to the container ...
    RequestHeader unset Authorization
    # ... tell it who authenticated instead. "set" replaces any same-named header from the client.
    RequestHeader set X-BBM-Remote-User expr=%{REMOTE_USER}
    RequestHeader set X-Forwarded-Proto "https"
    # Root-only file: RequestHeader set X-BBM-Proxy-Secret "<secret>"
    Include /etc/apache2/billberries-proxy-secret.conf

    ProxyPass "/" "http://docker-host:3040/"
    ProxyPassReverse "/" "http://docker-host:3040/"
    # The container compares the browser's Origin header with Host to block cross-site requests.
    ProxyPreserveHost On

    SSLCertificateFile /etc/letsencrypt/live/billberries.example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/billberries.example.com/privkey.pem
    Include /etc/letsencrypt/options-ssl-apache.conf
</VirtualHost>
</IfModule>
```

```sh
apache2ctl configtest && apache2ctl graceful
```

`expr=` in `RequestHeader` needs Apache 2.4.10 or newer. Don't enable `ProxyRequests` (forward proxying); `ProxyPass` alone is a reverse proxy.

## 5. Verify

```sh
curl -sI http://billberries.example.com/                       # 301 to https://
curl -s -o /dev/null -w '%{http_code}\n' https://billberries.example.com/                  # 401
curl -s -o /dev/null -w '%{http_code}\n' -u alice:wrong https://billberries.example.com/   # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-BBM-Proxy-Secret: guess' \
     -H 'X-BBM-Remote-User: alice' https://billberries.example.com/                        # 401
curl -s -o /dev/null -w '%{http_code}\n' http://docker-host:3040/                          # 403 (no secret)
curl -s http://docker-host:3040/healthz                                                    # ok
```

Then open `https://billberries.example.com/` in a browser, log in as `alice`, and log in to Billberries as described in the [README](../README.md#logging-in-to-billberries).
