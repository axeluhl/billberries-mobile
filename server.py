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
import html as htmllib
import json
import logging
import os
import random
import re
import secrets
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

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
TZ = ZoneInfo("Europe/Berlin")
# Daily scan for partner searches: SCAN_TIME (local) plus a random offset of up to SCAN_JITTER_MINUTES.
SCAN_TIME = os.environ.get("SCAN_TIME", "08:00")
SCAN_JITTER_MINUTES = int(os.environ.get("SCAN_JITTER_MINUTES", "180"))
APP_URL = os.environ.get("APP_URL", "")
NOTIFY_LANG = os.environ.get("NOTIFY_LANG", "de")
SMTP = {
    "host": os.environ.get("SMTP_HOST", ""),
    "port": int(os.environ.get("SMTP_PORT", "587")),
    "security": os.environ.get("SMTP_SECURITY", "starttls").lower(),  # starttls | ssl | none
    "user": os.environ.get("SMTP_USER", ""),
    "password": os.environ.get("SMTP_PASSWORD", ""),
    "from": os.environ.get("SMTP_FROM", ""),
    "to": [a.strip() for a in os.environ.get("NOTIFY_TO", "").split(",") if a.strip()],
}
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

SLOT_SECONDS = 1800
# Billberries column order B01..B05 is tables 15, 16, 17, 13, 14 at BSC Karlsruhe.
DEFAULT_SETTINGS = {
    "tables": [
        {"id": "B01", "number": 15, "size": "2.84 × 1.42 m", "cloth": "Simonis"},
        {"id": "B02", "number": 16, "size": "2.84 × 1.42 m", "cloth": "Royal Pro"},
        {"id": "B03", "number": 17, "size": "2.84 × 1.42 m", "cloth": "Simonis"},
        {"id": "B04", "number": 13, "size": "2.10 × 1.05 m", "cloth": ""},
        {"id": "B05", "number": 14, "size": "2.10 × 1.05 m", "cloth": ""},
    ],
    # Preferred properties come first; "order" breaks ties between equally good tables
    # (e.g. 17 before 15, which have the same size and cloth).
    "size": "2.84 × 1.42 m",
    "cloth": "Simonis",
    "order": ["B03", "B01", "B02", "B04", "B05"],
}

CELL_RE = re.compile(
    r'<div class="bb_rs_table_cell" style="grid-area:\s*(\d+)\s*/\s*(\d+)\s*/\s*span\s*(\d+)[^"]*"([^>]*)>'
    r'\s*<div class="([^"]+)"[^>]*>(.*?)</div>', re.S)
CELL_ID_RE = re.compile(r'\bid="(\d+)_(B\d{2})"')
CELL_CLICK_RE = re.compile(r"doReservation\('(B\d{2})','(\d+)'\)")


class LoginRequired(Exception):
    pass


class AppError(Exception):
    """An error the page can translate: stable code, parameters, English fallback message."""

    def __init__(self, status, code, message, **params):
        super().__init__(message)
        self.status, self.code, self.params = status, code, params


class UpstreamError(AppError):
    def __init__(self, code, message, **params):
        super().__init__(502, code, message, **params)


class Conflict(AppError):
    def __init__(self, code, message, **params):
        super().__init__(409, code, message, **params)


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
        raise UpstreamError("upstream-unreachable", "Billberries is not reachable.") from e
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
        raise UpstreamError("upstream-status", f"Billberries answered with HTTP {result['status']}.", status=result["status"])
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
        raise AppError(400, "login-no-code", "No Google code found in the pasted address.")
    path = "/login/callback?" + urllib.parse.urlencode({"code": code, "scope": "openid email profile"})
    result = upstream("GET", path)
    if not result["cookie"] or result["status"] >= 400:
        raise AppError(400, "login-rejected", "Billberries rejected the code. Each code works only once and briefly; please repeat the Google login.")
    update_state(session=result["cookie"], loginAt=int(time.time()), loggedOutAt=None, player=None, loginMailAt=None)
    try:
        billberries("GET", OVERVIEW_PATH)
    except LoginRequired:
        raise AppError(400, "login-unconfirmed", "Billberries did not confirm the login. Please try again.") from None
    return load_state().get("user")


def norm_name(name):
    return re.sub(r"\s+", "", name or "").lower()


def parse_calendar(page):
    """Bookings, free cells and the rowIdx->epoch mapping from the overview HTML."""
    bookings, free, row_epoch = [], set(), {}
    for _row, _col, span, attrs, cls, inner in CELL_RE.findall(page):
        cell_id = CELL_ID_RE.search(attrs)
        click = CELL_CLICK_RE.search(attrs)
        if not cell_id or not click:
            continue  # headers and past ("too late") cells
        epoch, table, row, span = int(cell_id.group(1)), cell_id.group(2), int(click.group(2)), int(span)
        for k in range(span):
            row_epoch[row + k] = epoch + k * SLOT_SECONDS
        if "rs-item-booked" in cls:
            lines = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", part))).strip()
                     for part in re.split(r"<br\s*/?>", inner)]
            bookings.append({"table": table, "row": row, "epoch": epoch, "end": epoch + span * SLOT_SECONDS,
                             "lines": [line for line in lines if line]})
        elif "rs-item-not-booked" in cls:
            free.add((table, row))
    return {"bookings": bookings, "free": free, "rowEpoch": row_epoch}


def is_mine(booking, player):
    me = norm_name(player.get("name")) if player else ""
    return bool(me) and any(norm_name(line) == me for line in booking["lines"][:2])


def get_player(calendar=None):
    """The logged-in user's Billberries player name and ID, as preselected in the reservation form."""
    player = load_state().get("player")
    if player:
        return player
    calendar = calendar or parse_calendar(billberries("GET", OVERVIEW_PATH))
    if not calendar["free"]:
        return None
    table, row = min(calendar["free"], key=lambda cell: cell[1])
    form = dict(FORM_DEFAULTS, tableID=table, rowIdx=str(row))
    dialog = billberries("POST", POST_PATH, form=form)
    match = re.search(r'id="player1Select".*?<option value="([^"]+)" selected>\s*([^<]+?)\s*</option>', dialog, re.S)
    if not match or match.group(1).startswith("response_request_"):
        return None
    player = {"id": match.group(1), "name": htmllib.unescape(match.group(2))}
    update_state(player=player)
    return player


def check_new_booking(table, row_from, row_to):
    """Billberries itself accepts overlapping reservations; refuse them here."""
    calendar = parse_calendar(billberries("GET", OVERVIEW_PATH))
    start, last = calendar["rowEpoch"].get(row_from), calendar["rowEpoch"].get(row_to)
    if start is None or last is None or row_to < row_from or last - start != (row_to - row_from) * SLOT_SECONDS:
        raise Conflict("conflict-unavailable", "This period can no longer be booked. Please refresh the calendar.")
    end = last + SLOT_SECONDS
    player = get_player(calendar)
    for booking in calendar["bookings"]:
        if booking["epoch"] < end and start < booking["end"]:
            number = table_number(booking["table"])
            if booking["table"] == table:
                raise Conflict("conflict-table", f"Table {number} is already booked in this period.", table=number)
            if is_mine(booking, player):
                raise Conflict("conflict-mine", f"You already have table {number} booked in this period.", table=number)


def load_settings():
    return load_state().get("settings") or DEFAULT_SETTINGS


def table_number(table_id):
    return next((t["number"] for t in load_settings()["tables"] if t["id"] == table_id), table_id)


def validate_settings(data):
    ids = [t["id"] for t in DEFAULT_SETTINGS["tables"]]
    tables = data.get("tables")
    if not isinstance(tables, list) or sorted(t.get("id") for t in tables if isinstance(t, dict)) != sorted(ids):
        raise AppError(400, "invalid-settings", "Invalid table list.")
    clean = []
    for t in tables:
        number = t.get("number")
        if not isinstance(number, int) or not 1 <= number <= 99:
            raise AppError(400, "invalid-settings", "Invalid table number.")
        clean.append({"id": t["id"], "number": number,
                      "size": str(t.get("size", ""))[:40].strip(),
                      "cloth": str(t.get("cloth", ""))[:40].strip()})
    order = data.get("order")
    if not isinstance(order, list) or sorted(order) != sorted(ids):
        raise AppError(400, "invalid-settings", "Invalid table order.")
    return {"tables": clean, "order": order,
            "size": str(data.get("size", ""))[:40], "cloth": str(data.get("cloth", ""))[:40]}


# ---------- e-mail notifications ----------

MAIL_TEXT = {
    "de": {
        "days": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"],
        "subject_one": "Billberries: {player} sucht einen Spielpartner ({when})",
        "subject_many": "Billberries: {n} Spielpartner-Suchen",
        "intro": "Neu im Kalender des BSC Karlsruhe – jemand sucht einen Spielpartner:",
        "item": "• {player} — {when} — Tisch {number} ({size}, {cloth})",
        "unknown": "unbekannt", "join": "Mitspielen: {url}", "original": "Original-Kalender: {url}",
        "footer": "– Billberries Mobile (automatische Nachricht, Prüfung einmal täglich)",
        "login_subject": "Billberries Mobile: Bitte erneut bei Billberries anmelden",
        "login_body": "Die Billberries-Anmeldung des Servers ist abgelaufen. Bis zur neuen Anmeldung kann Billberries Mobile "
                      "weder buchen noch Spielpartner-Suchen melden.\n\nNeu anmelden: {url}",
        "test_subject": "Billberries Mobile: Test-E-Mail",
        "test_body": "Der E-Mail-Versand von Billberries Mobile funktioniert.",
    },
    "en": {
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "subject_one": "Billberries: {player} is looking for a partner ({when})",
        "subject_many": "Billberries: {n} players looking for a partner",
        "intro": "New in the BSC Karlsruhe calendar – someone is looking for a partner:",
        "item": "• {player} — {when} — table {number} ({size}, {cloth})",
        "unknown": "unknown", "join": "Join: {url}", "original": "Original calendar: {url}",
        "footer": "– Billberries Mobile (automatic message, checked once a day)",
        "login_subject": "Billberries Mobile: please log in to Billberries again",
        "login_body": "The server's Billberries login has expired. Until you log in again, Billberries Mobile can neither "
                      "book nor report partner searches.\n\nLog in: {url}",
        "test_subject": "Billberries Mobile: test e-mail",
        "test_body": "Sending e-mail from Billberries Mobile works.",
    },
}


def mail_text():
    return MAIL_TEXT.get(NOTIFY_LANG, MAIL_TEXT["en"])


def mail_configured():
    return bool(SMTP["host"] and SMTP["from"] and SMTP["to"])


def send_mail(subject, body):
    if not mail_configured():
        raise AppError(409, "mail-not-configured", "E-mail is not configured (SMTP_HOST, SMTP_FROM, NOTIFY_TO).")
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, SMTP["from"], ", ".join(SMTP["to"])
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=SMTP["from"].rpartition("@")[2] or None)
    message.set_content(body)
    context = ssl.create_default_context()
    try:
        if SMTP["security"] == "ssl":
            server = smtplib.SMTP_SSL(SMTP["host"], SMTP["port"], timeout=30, context=context)
        else:
            server = smtplib.SMTP(SMTP["host"], SMTP["port"], timeout=30)
        with server:
            if SMTP["security"] == "starttls":
                server.starttls(context=context)
            if SMTP["user"]:
                server.login(SMTP["user"], SMTP["password"])
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as e:
        log.warning("sending mail failed: %s", e)
        raise AppError(502, "mail-failed", f"Sending the e-mail failed: {e}", reason=str(e)) from e
    log.info("mail sent: %s", subject)


def when_text(start, end, with_year=True):
    a, b = datetime.fromtimestamp(start, TZ), datetime.fromtimestamp(end, TZ)
    date = a.strftime("%d.%m.%Y" if with_year else "%d.%m.")
    return f"{mail_text()['days'][a.weekday()]}, {date}, {a:%H:%M}–{b:%H:%M}"


def is_seeking(booking):
    return "Suche Spielpartner" in booking["lines"]


def seeking_key(booking):
    return f'{booking["table"]}|{booking["epoch"]}|{norm_name(booking["lines"][0] if booking["lines"] else "")}'


def maybe_send_login_mail():
    """One reminder per expired login; a new login resets it."""
    state = load_state()
    sent, logged_out = state.get("loginMailAt") or 0, state.get("loggedOutAt") or 0
    if not mail_configured() or (sent and sent >= logged_out):
        return False
    text = mail_text()
    send_mail(text["login_subject"], text["login_body"].format(url=APP_URL or BILLBERRIES))
    update_state(loginMailAt=int(time.time()))
    return True


def scan_once(reason):
    """Report partner searches by other players that appeared since the last scan."""
    log.info("scan (%s)", reason)
    result = {"at": int(time.time()), "reason": reason, "loggedIn": True, "open": 0, "new": 0, "mailed": False}
    try:
        if not load_state().get("session"):
            raise LoginRequired()
        calendar = parse_calendar(billberries("GET", OVERVIEW_PATH))
    except LoginRequired:
        result["loggedIn"] = False
        try:
            result["mailed"] = maybe_send_login_mail()
        except AppError as e:
            result["error"] = e.code
        update_state(lastScan=result)
        return result
    player = get_player(calendar)
    now = time.time()
    open_searches = sorted((b for b in calendar["bookings"]
                            if is_seeking(b) and b["end"] > now and not is_mine(b, player)), key=lambda b: b["epoch"])
    seen = {k: v for k, v in (load_state().get("seenSeeking") or {}).items() if v > now}
    new = [b for b in open_searches if seeking_key(b) not in seen]
    result.update(open=len(open_searches), new=len(new))
    if new and mail_configured():
        text = mail_text()
        tables = {t["id"]: t for t in load_settings()["tables"]}
        items = []
        for b in new:
            t = tables.get(b["table"], {})
            items.append(text["item"].format(player=b["lines"][0], when=when_text(b["epoch"], b["end"]), number=t.get("number", b["table"]),
                                             size=t.get("size") or text["unknown"], cloth=t.get("cloth") or text["unknown"]))
        links = [text["join"].format(url=APP_URL)] if APP_URL else []
        links.append(text["original"].format(url=BILLBERRIES + OVERVIEW_PATH))
        subject = (text["subject_one"].format(player=new[0]["lines"][0], when=when_text(new[0]["epoch"], new[0]["end"], with_year=False))
                   if len(new) == 1 else text["subject_many"].format(n=len(new)))
        try:
            send_mail(subject, "\n".join([text["intro"], "", *items, "", *links, "", text["footer"]]))
            result["mailed"] = True
        except AppError as e:
            result["error"] = e.code  # not marked as seen: retried at the next scan
    elif new:
        log.info("%d new partner search(es); e-mail not configured", len(new))
    if result["mailed"]:
        seen.update({seeking_key(b): b["end"] for b in new})
    update_state(seenSeeking=seen, lastScan=result)
    return result


def scan_time_on(day):
    hour, minute = (int(x) for x in SCAN_TIME.split(":"))
    base = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    return base.timestamp() + random.uniform(0, SCAN_JITTER_MINUTES * 60)


def scanner():
    """Once a day at SCAN_TIME plus a random offset; the next run survives restarts, a missed run happens at startup."""
    time.sleep(10)
    while True:
        now = time.time()
        due = load_state().get("nextScanAt")
        if not due:
            today = datetime.fromtimestamp(now, TZ).date()
            due = scan_time_on(today)
            if due <= now:
                due = scan_time_on(today + timedelta(days=1))
            update_state(nextScanAt=int(due))
            log.info("next scan at %s", datetime.fromtimestamp(due, TZ).isoformat())
        if due > now:
            time.sleep(min(300, due - now))
            continue
        try:
            scan_once("scheduled")
        except Exception:
            log.exception("scan failed")
        tomorrow = datetime.fromtimestamp(time.time(), TZ).date() + timedelta(days=1)
        due = scan_time_on(tomorrow)
        update_state(nextScanAt=int(due))
        log.info("next scan at %s", datetime.fromtimestamp(due, TZ).isoformat())


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
            raise AppError(413, "request-too-large", "Request too large.")
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
            return self.json(403, {"error": "Forbidden", "code": "forbidden"})
        self.api("GET", path)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.trusted():
            return self.send(403, "Forbidden", "text/plain")
        if not path.startswith("/api/") or not self.api_request_allowed():
            return self.json(403, {"error": "Forbidden", "code": "forbidden"})
        self.api("POST", path)

    def api(self, method, path):
        try:
            if method == "GET" and path == "/api/status":
                state = load_state()
                return self.json(200, {
                    "loggedIn": bool(state.get("session")),
                    "user": state.get("user"),
                    "player": state.get("player"),
                    "loginAt": state.get("loginAt"),
                    "lastOkAt": state.get("lastOkAt"),
                    "renewedAt": state.get("renewedAt"),
                    "notify": {"configured": mail_configured(), "to": SMTP["to"],
                               "nextScanAt": state.get("nextScanAt"), "lastScan": state.get("lastScan")},
                })
            if method == "POST" and path == "/api/admin/scan":
                return self.json(200, scan_once("manual"))
            if method == "POST" and path == "/api/admin/test-mail":
                text = mail_text()
                send_mail(text["test_subject"], text["test_body"])
                return self.json(200, {"ok": True, "to": SMTP["to"]})
            if method == "GET" and path == "/api/me":
                return self.json(200, {"player": get_player()})
            if method == "GET" and path == "/api/settings":
                return self.json(200, load_settings())
            if method == "POST" and path == "/api/settings":
                settings = validate_settings(json.loads(self.read_body() or "{}"))
                update_state(settings=settings)
                return self.json(200, settings)
            if method == "GET" and path == "/api/calendar":
                return self.send(200, billberries("GET", OVERVIEW_PATH), "text/html; charset=utf-8")
            if method == "POST" and path == "/api/reservation":
                fields = urllib.parse.parse_qs(self.read_body(), keep_blank_values=True)
                form = dict(FORM_DEFAULTS)
                for key, values in fields.items():
                    if key not in ALLOWED_FIELDS or len(values) != 1 or not ALLOWED_FIELDS[key].match(values[0]):
                        raise AppError(400, "invalid-field", f"Invalid field: {key}", field=key)
                    form[key] = values[0]
                if form["whatHasChanged"] == "updateReservation" and form["newValue"] == "New":
                    check_new_booking(form["tableID"], int(form["rowIdx"] or -1), int(form["rowIdxTo"] or -1))
                return self.send(200, billberries("POST", POST_PATH, form=form), "text/html; charset=utf-8")
            if method == "GET" and path == "/api/google-url":
                return self.json(200, {"url": google_url()})
            if method == "POST" and path == "/api/google-code":
                payload = json.loads(self.read_body() or "{}")
                user = login_with_code(str(payload.get("text", "")))
                return self.json(200, {"ok": True, "user": user})
            if method == "POST" and path == "/api/billberries-logout":
                update_state(session=None, player=None, loggedOutAt=int(time.time()))
                return self.json(200, {"ok": True})
            return self.json(404, {"error": "Not found", "code": "not-found"})
        except LoginRequired:
            return self.json(401, {"error": "Billberries login required", "code": "billberries-login"})
        except AppError as e:
            return self.json(e.status, {"error": str(e), "code": e.code, "params": e.params})
        except ValueError:
            return self.json(400, {"error": "Bad request", "code": "bad-request"})
        except Exception:
            log.exception("request failed")
            return self.json(500, {"error": "Internal error", "code": "internal"})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not PROXY_SECRET or not ALLOWED_USER:
        log.error("PROXY_SECRET or ALLOWED_USER is not set; all requests except /healthz will be refused")
    os.makedirs(DATA_DIR, exist_ok=True)
    threading.Thread(target=scanner, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log.info("listening on port %d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
