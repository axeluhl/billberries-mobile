#!/usr/bin/env python3
"""Billberries Mobile backend.

Serves the single-page app and a narrow API that talks to billberries.de on
behalf of one user. User authentication happens in the reverse proxy (e.g.
Apache Basic Auth). Requests must carry the proxy's shared secret and the
authenticated user name, so direct LAN access to the container is refused.
The container stores only the Billberries session cookie, never Google
credentials.
"""

import gzip
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BILLBERRIES = "https://billberries.de"
LOCATION = os.environ.get("BB_LOCATION", "0001")
OVERVIEW_PATH = f"/reservation/billard/{LOCATION}/"
POST_PATH = "/reservation/billard/"
GOOGLE_CLIENT_ID = "1005961156830-28lbss6viqob07jucsq9dao36tnqiu94.apps.googleusercontent.com"
CALLBACK = f"{BILLBERRIES}/login/callback"
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Mobile Safari/537.36 billberries-mobile"

DATA_DIR = os.environ.get("DATA_DIR", "/data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(APP_DIR, "index.html")
PROXY_SECRET = os.environ.get("PROXY_SECRET", "")
ALLOWED_USER = os.environ.get("ALLOWED_USER", "")
KEEPALIVE_SECONDS = float(os.environ.get("KEEPALIVE_HOURS", "4")) * 3600
PORT = int(os.environ.get("PORT", "8080"))
CLIENT_HEADER = "billberries-mobile"

# The reservation form has many fields; only these may be chosen by the client.
FORM_DEFAULTS = {
    "whatHasChanged": "", "newValue": "", "newValue2": "", "activeTabIdx": "n/a",
    "screen_avail_h": "", "screen_avail_w": "", "window_inner_h": "", "window_inner_w": "",
    "rowIdx": "", "rowIdxTo": "", "noOfSlots": "", "rsMatchID": "", "ctrlID": "n/a",
    "ctrlDevID": "None", "ctrlDevMappingID": "None", "htmlTemplateID": "", "matchStateID": "",
    "matchID": "", "roomID": f"{LOCATION}_any", "requestID": "", "sessionID": "",
    "locationID": LOCATION, "tableID": "None", "player1ID": "", "player2ID": "", "player3ID": "",
    "player4ID": "", "mpID": "", "videoMode": "", "refreshMode": "never", "refreshID": "refreshButton",
    "socketConnectAddress": f"{BILLBERRIES}/", "requestSite": "/reservation/billard/",
    "invokedFunction": "nop", "invokedFunction_p1": "noValue", "invokedFunction_p2": "noValue",
    "tournamentID": "", "url_parameter": "",
}
ALLOWED_FIELDS = {
    "whatHasChanged": re.compile(r"^(|updateReservation)$"),
    "newValue": re.compile(r"^(|New|Cancel)$"),
    "tableID": re.compile(r"^B\d{2}$"),
    "rowIdx": re.compile(r"^\d{0,5}$"),
    "rowIdxTo": re.compile(r"^\d{0,5}$"),
    "rsMatchID": re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$"),
    "player1ID": re.compile(r"^(\d{1,15}|response_request_[A-Za-z]{1,30})?$"),
    "player2ID": re.compile(r"^(\d{1,15}|response_request_[A-Za-z]{1,30})?$"),
    "mpID": re.compile(r"^[a-z0-9_]{0,64}$"),
}

log = logging.getLogger("billberries-mobile")
state_lock = threading.Lock()


class LoginRequired(Exception):
    pass


class UpstreamError(Exception):
    pass


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("state file unreadable; treating as logged out")
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def update_state(**changes):
    with state_lock:
        state = load_state()
        state.update(changes)
        save_state(state)
        return state


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def upstream(method, path, cookie=None, form=None, timeout=30):
    """One request to Billberries without following redirects."""
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    body = None
    if cookie:
        headers["Cookie"] = "session=" + cookie
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(BILLBERRIES + path, data=body, headers=headers, method=method)
    try:
        response = _opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        response = e  # 3xx and error statuses still carry headers and body
    except (urllib.error.URLError, OSError) as e:
        raise UpstreamError(f"Billberries nicht erreichbar: {e}") from e
    with response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        elif response.headers.get("Content-Encoding") == "deflate":
            raw = zlib.decompress(raw)
        new_cookie = None
        for header in response.headers.get_all("Set-Cookie") or []:
            match = re.match(r"\s*session=([^;]*)", header)
            if match:
                new_cookie = match.group(1)
        return {
            "status": response.status if hasattr(response, "status") else response.code,
            "location": response.headers.get("Location", ""),
            "cookie": new_cookie,
            "text": raw.decode("utf-8", "replace"),
        }


def logged_out(result):
    return (
        "accounts.google.com" in result["location"]
        or 'id="login_google"' in result["text"]
        or (result["status"] in (301, 302, 303, 307, 308) and "/login" in result["location"])
    )


def user_name(html):
    match = re.search(r"User:\s*([^<\n]+?)\s*<", html)
    return match.group(1).strip() if match else None


def billberries(method, path, form=None):
    """Request with the stored session; persists renewed cookies."""
    state = load_state()
    cookie = state.get("session")
    if not cookie:
        raise LoginRequired()
    result = upstream(method, path, cookie=cookie, form=form)
    if logged_out(result):
        log.warning("Billberries session no longer valid")
        update_state(session=None, loggedOutAt=int(time.time()))
        raise LoginRequired()
    if result["status"] >= 400:
        raise UpstreamError(f"Billberries antwortete mit HTTP {result['status']}")
    changes = {"lastOkAt": int(time.time())}
    if result["cookie"] and result["cookie"] != cookie:
        changes["session"] = result["cookie"]
        changes["renewedAt"] = int(time.time())
    name = user_name(result["text"])
    if name:
        changes["user"] = name
    update_state(**changes)
    return result["text"]


def google_url():
    query = urllib.parse.urlencode({
        # "code id_token" makes Google return the code in the URL fragment, which
        # Billberries never receives, so the code stays usable for us.
        "response_type": "code id_token",
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": CALLBACK,
        "scope": "openid email profile",
        "nonce": secrets.token_urlsafe(16),
    }, quote_via=urllib.parse.quote)
    return "https://accounts.google.com/o/oauth2/v2/auth?" + query


def login_with_code(text):
    match = re.search(r"(?:^|[#?&\s])code=([^&#\s]+)", text.strip())
    code = urllib.parse.unquote(match.group(1)) if match else text.strip()
    if not re.fullmatch(r"[\w/.\-]{10,512}", code):
        raise ValueError("Kein Google-Code in der eingefügten Adresse gefunden.")
    path = "/login/callback?" + urllib.parse.urlencode({"code": code, "scope": "openid email profile"})
    result = upstream("GET", path)
    if not result["cookie"] or result["status"] >= 400:
        raise ValueError("Billberries hat den Code abgelehnt. Bitte die Google-Anmeldung wiederholen; jeder Code gilt nur einmal und nur kurz.")
    update_state(session=result["cookie"], loginAt=int(time.time()), loggedOutAt=None)
    try:
        billberries("GET", OVERVIEW_PATH)
    except LoginRequired:
        raise ValueError("Billberries hat keine Anmeldung bestätigt. Bitte erneut versuchen.") from None
    return load_state().get("user")


def keepalive():
    while True:
        time.sleep(KEEPALIVE_SECONDS)
        if not load_state().get("session"):
            continue
        try:
            billberries("GET", OVERVIEW_PATH)
            log.info("keepalive ok")
        except LoginRequired:
            log.warning("keepalive: login required")
        except Exception:
            log.exception("keepalive failed")


class Handler(BaseHTTPRequestHandler):
    server_version = "billberries-mobile"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Paths only: never log query strings, cookies or bodies.
        log.info("%s %s %s", self.command, self.path.split("?")[0], args[1] if len(args) > 1 else "")

    def send(self, status, body, content_type, extra=None):
        data = body.encode() if isinstance(body, str) else body
        headers = {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            **(extra or {}),
        }
        if len(data) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            data = gzip.compress(data, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, status, payload):
        self.send(status, json.dumps(payload), "application/json; charset=utf-8")

    def trusted(self):
        """The request came through the authenticating reverse proxy."""
        secret = self.headers.get("X-BBM-Proxy-Secret", "")
        user = self.headers.get("X-BBM-Remote-User", "")
        return bool(PROXY_SECRET) and bool(ALLOWED_USER) and hmac.compare_digest(secret, PROXY_SECRET) and user == ALLOWED_USER

    def api_request_allowed(self):
        """Browsers attach Basic Auth to cross-site requests; require same-origin API calls."""
        if self.headers.get("X-Requested-With") != CLIENT_HEADER:
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            return False
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host", ""):
            return False
        return True

    def read_body(self, limit=16384):
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise ValueError("Anfrage zu groß")
        return self.rfile.read(length).decode("utf-8", "replace")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if not self.trusted():
            return self.send(403, "Forbidden", "text/plain")
        if path in ("/", "/index.html"):
            with open(INDEX_FILE, "rb") as f:
                html = f.read()
            csp = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                   "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            return self.send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": csp, "Cache-Control": "no-cache"})
        if not path.startswith("/api/"):
            return self.send(404, "Not found", "text/plain")
        if not self.api_request_allowed():
            return self.json(403, {"error": "forbidden"})
        self.api("GET", path)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.trusted():
            return self.send(403, "Forbidden", "text/plain")
        if not path.startswith("/api/") or not self.api_request_allowed():
            return self.json(403, {"error": "forbidden"})
        self.api("POST", path)

    def api(self, method, path):
        try:
            if method == "GET" and path == "/api/status":
                state = load_state()
                return self.json(200, {
                    "loggedIn": bool(state.get("session")),
                    "user": state.get("user"),
                    "loginAt": state.get("loginAt"),
                    "lastOkAt": state.get("lastOkAt"),
                    "renewedAt": state.get("renewedAt"),
                })
            if method == "GET" and path == "/api/calendar":
                return self.send(200, billberries("GET", OVERVIEW_PATH), "text/html; charset=utf-8")
            if method == "POST" and path == "/api/reservation":
                fields = urllib.parse.parse_qs(self.read_body(), keep_blank_values=True)
                form = dict(FORM_DEFAULTS)
                for key, values in fields.items():
                    if key not in ALLOWED_FIELDS or len(values) != 1 or not ALLOWED_FIELDS[key].match(values[0]):
                        return self.json(400, {"error": f"Ungültiges Feld: {key}"})
                    form[key] = values[0]
                return self.send(200, billberries("POST", POST_PATH, form=form), "text/html; charset=utf-8")
            if method == "GET" and path == "/api/google-url":
                return self.json(200, {"url": google_url()})
            if method == "POST" and path == "/api/google-code":
                payload = json.loads(self.read_body() or "{}")
                user = login_with_code(str(payload.get("text", "")))
                return self.json(200, {"ok": True, "user": user})
            if method == "POST" and path == "/api/billberries-logout":
                update_state(session=None, loggedOutAt=int(time.time()))
                return self.json(200, {"ok": True})
            return self.json(404, {"error": "not found"})
        except LoginRequired:
            return self.json(401, {"error": "billberries-login"})
        except ValueError as e:
            return self.json(400, {"error": str(e)})
        except UpstreamError as e:
            return self.json(502, {"error": str(e)})
        except Exception:
            log.exception("request failed")
            return self.json(500, {"error": "Interner Fehler"})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not PROXY_SECRET or not ALLOWED_USER:
        log.error("PROXY_SECRET or ALLOWED_USER is not set; all requests except /healthz will be refused")
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=keepalive, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("listening on port %d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
