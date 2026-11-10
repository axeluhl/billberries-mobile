"""Isolated tests: no Billberries requests, SMTP delivery, or real state files.

Run with: python3 -m unittest discover -s tests -v
"""

import copy
from datetime import datetime
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import server


START = int(datetime(2027, 1, 15, 18, tzinfo=server.TZ).timestamp())
PLAYER = {"id": "123456", "name": "Example E."}
SMTP = {"host": "smtp.example.org", "port": 587, "security": "starttls",
        "user": "user", "password": "test-password", "from": "sender@example.org", "to": ["calendar@example.org"]}


def calendar_html(booked=False, player="Example E.", span=6):
    cells = []
    for k in range(6):
        if booked and k:
            continue
        css = "rs-item-booked" if booked else "rs-item-not-booked"
        body = f"{player}<br>None<br>Training-1-Person<br>" if booked else "click me<br>"
        cells.append(f'<div class="bb_rs_table_cell" style="grid-area: {k+1} / 4 / span {span if booked else 1} / span 1;" '
                     f'id="{START+k*1800}_B03" onclick="doReservation(\'B03\',\'{100+k}\')">'
                     f'<div class="{css}">{body}</div></div>')
    return "\n".join(cells)


def form():
    return dict(server.FORM_DEFAULTS, whatHasChanged="updateReservation", newValue="New",
                tableID="B03", rowIdx="100", rowIdxTo="105", player1ID=PLAYER["id"],
                rsMatchID="00000000-0000-0000-0000-000000000001", mpID="billard_training1")


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.enterContext(patch.object(server, "STATE_FILE", str(Path(self.tmp.name) / "state.json")))
        self.enterContext(patch.object(server, "SMTP", copy.deepcopy(SMTP)))
        self.enterContext(patch.object(server, "APP_URL", "https://billberries.example.org/"))
        self.enterContext(patch.object(server, "NOTIFY_LANG", "en"))
        server.save_state({"session": "fake-session", "player": PLAYER})


class SettingsTests(StateTest):
    def test_existing_settings_get_default_and_preserve_values(self):
        settings = copy.deepcopy(server.DEFAULT_SETTINGS)
        settings.pop("calendarReminderMinutes")
        settings["order"] = ["B01", "B03", "B02", "B04", "B05"]
        server.update_state(settings=settings)
        self.assertEqual(server.load_settings()["calendarReminderMinutes"], 60)
        self.assertEqual(server.load_settings()["order"], settings["order"])

    def test_persistence_and_older_clients(self):
        settings = server.load_settings()
        settings["calendarReminderMinutes"] = 90
        server.update_state(settings=server.validate_settings(settings))
        self.assertEqual(server.load_settings()["calendarReminderMinutes"], 90)
        settings.pop("calendarReminderMinutes")
        self.assertEqual(server.validate_settings(settings)["calendarReminderMinutes"], 90)
        self.assertEqual(server.load_state()["session"], "fake-session")

    def test_bounds(self):
        for value in (0, 60, 10080):
            with self.subTest(value=value):
                settings = dict(server.load_settings(), calendarReminderMinutes=value)
                self.assertEqual(server.validate_settings(settings)["calendarReminderMinutes"], value)
        for value in (-1, 10081, "60", 1.5, None, True):
            with self.subTest(value=value), self.assertRaises(server.AppError):
                server.validate_settings(dict(server.load_settings(), calendarReminderMinutes=value))


class CalendarTests(StateTest):
    def booking(self):
        return {"table": "B03", "epoch": START, "end": START + 10800,
                "lines": [PLAYER["name"], "None", "Training-1-Person"]}

    def test_winter_summer_dst_and_custom_reminder(self):
        cases = [((2027, 1, 15, 18), "20270115T170000Z"),
                 ((2027, 7, 15, 18), "20270715T160000Z"),
                 ((2027, 3, 28, 1), "20270328T000000Z")]
        for parts, expected in cases:
            booking = self.booking()
            booking["epoch"] = int(datetime(*parts, tzinfo=server.TZ).timestamp())
            booking["end"] = booking["epoch"] + 10800
            data = server.booking_calendar(booking, PLAYER, 90, "Billiards", "Training", "BSC Karlsruhe")
            self.assertIn("DTSTART:" + expected, data)
            self.assertIn("TRIGGER;RELATED=START:-PT90M", data)
            self.assertIn("ACTION:DISPLAY", data)
            self.assertIn("METHOD:PUBLISH", data)
            self.assertTrue(data.endswith("END:VCALENDAR\r\n"))
            self.assertNotIn("\n", data.replace("\r\n", ""))

    def test_uid_is_stable_and_distinct_for_another_table_or_player(self):
        booking = self.booking()
        def uid(b, p):
            data = server.booking_calendar(b, p, 60, "Title", "Description", "Location")
            return next(line for line in data.split("\r\n") if line.startswith("UID:"))
        self.assertEqual(uid(booking, PLAYER), uid(dict(booking, end=START+7200), PLAYER))
        self.assertNotEqual(uid(booking, PLAYER), uid(dict(booking, table="B01"), PLAYER))
        self.assertNotEqual(uid(booking, PLAYER), uid(booking, dict(PLAYER, id="654321")))

    def test_text_escaping_and_utf8_folding(self):
        text = "Ä😀" * 60 + ", semi; slash\\ CR\r\nBEGIN:VEVENT"
        data = server.booking_calendar(self.booking(), PLAYER, 0, text, text, text)
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in data.split("\r\n")))
        unfolded = data.replace("\r\n ", "")
        self.assertIn("SUMMARY:" + server.ics_text(text), unfolded)
        self.assertIn("TRIGGER;RELATED=START:-PT0M", unfolded)
        self.assertEqual(data.split("\r\n").count("BEGIN:VEVENT"), 1)

    def test_smtp_mime_attachment_and_configured_recipient(self):
        settings = dict(server.load_settings(), calendarReminderMinutes=120)
        server.update_state(settings=settings)
        with patch.object(server.smtplib, "SMTP") as smtp:
            server.send_booking_mail(self.booking(), PLAYER)
        client = smtp.return_value
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("user", "test-password")
        message = client.send_message.call_args.args[0]
        parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
        self.assertEqual(parsed["To"], "calendar@example.org")
        attachment, = parsed.iter_attachments()
        self.assertEqual(attachment.get_filename(), "reservation.ics")
        self.assertEqual(attachment.get_content_type(), "text/calendar")
        self.assertEqual(attachment.get_param("method"), "PUBLISH")
        self.assertIn("TRIGGER;RELATED=START:-PT120M", attachment.get_content())
        self.assertIn("Simonis", parsed.get_body(preferencelist=("plain",)).get_content())

    def test_existing_plain_mail_stays_plain(self):
        with patch.object(server.smtplib, "SMTP") as smtp:
            server.send_mail("Test", "A plain notification")
        message = smtp.return_value.send_message.call_args.args[0]
        self.assertFalse(message.is_multipart())


class SubmissionTests(StateTest):
    def test_confirmed_booking_sends_exactly_one_mail(self):
        with patch.object(server, "billberries", side_effect=[calendar_html(), calendar_html(True)]) as upstream, \
                patch.object(server, "send_booking_mail") as mail:
            page, status = server.submit_reservation(form())
        self.assertEqual(status, "sent")
        self.assertEqual(page, calendar_html(True))
        mail.assert_called_once()
        booking, player = mail.call_args.args
        self.assertEqual((booking["epoch"], booking["end"], player), (START, START+10800, PLAYER))
        self.assertEqual(upstream.call_count, 2)

    def test_form_response_requires_fresh_calendar_verification(self):
        with patch.object(server, "billberries", side_effect=[calendar_html(), "form", calendar_html(True)]) as upstream, \
                patch.object(server, "send_booking_mail") as mail:
            self.assertEqual(server.submit_reservation(form()), ("form", "sent"))
        self.assertEqual(upstream.call_count, 3)
        mail.assert_called_once()

    def test_mismatch_unconfirmed_and_post_verification_failure_never_mail(self):
        for response in (calendar_html(), calendar_html(True, player="Other O."), calendar_html(True, span=2)):
            with self.subTest(response=response), \
                    patch.object(server, "billberries", side_effect=[calendar_html(), response, response]), \
                    patch.object(server, "send_booking_mail") as mail:
                self.assertEqual(server.submit_reservation(form())[1], "unconfirmed")
                mail.assert_not_called()
        with self.assertLogs(server.log, level="ERROR"), \
                patch.object(server, "billberries", side_effect=[calendar_html(), "ok", server.LoginRequired()]), \
                patch.object(server, "send_booking_mail") as mail:
            self.assertEqual(server.submit_reservation(form()), ("ok", "unconfirmed"))
            mail.assert_not_called()

    def test_no_smtp_still_returns_booking(self):
        with patch.object(server, "SMTP", dict(SMTP, host="")), \
                patch.object(server, "billberries", side_effect=[calendar_html(), calendar_html(True)]), \
                patch.object(server, "send_booking_mail") as mail:
            self.assertEqual(server.submit_reservation(form()), (calendar_html(True), "not-configured"))
            mail.assert_not_called()

    def test_smtp_failure_does_not_retry_or_fail_the_booking(self):
        with self.assertLogs(server.log, level="ERROR"), \
                patch.object(server, "billberries", side_effect=[calendar_html(), calendar_html(True)]) as upstream, \
                patch.object(server, "send_booking_mail", side_effect=server.AppError(502, "mail-failed", "test")):
            self.assertEqual(server.submit_reservation(form()), (calendar_html(True), "failed"))
        self.assertEqual(sum(call.args[0] == "POST" for call in upstream.call_args_list), 1)

    def test_conflict_prevents_post_and_mail(self):
        with patch.object(server, "billberries", return_value=calendar_html(True)) as upstream, \
                patch.object(server, "send_booking_mail") as mail, self.assertRaises(server.Conflict):
            server.submit_reservation(form())
        self.assertEqual(upstream.call_count, 1)
        mail.assert_not_called()

    def test_dialog_and_cancel_send_no_calendar_mail(self):
        for request in (dict(server.FORM_DEFAULTS, tableID="B03", rowIdx="100"), dict(form(), newValue="Cancel")):
            with patch.object(server, "billberries", return_value="ok") as upstream, \
                    patch.object(server, "send_booking_mail") as mail:
                self.assertEqual(server.submit_reservation(request), ("ok", None))
                upstream.assert_called_once_with("POST", server.POST_PATH, form=request)
                mail.assert_not_called()


class ApiTests(StateTest):
    def test_reminder_settings_and_booking_response_header(self):
        self.enterContext(patch.object(server, "PROXY_SECRET", "test-proxy-secret"))
        self.enterContext(patch.object(server, "ALLOWED_USER", "test-user"))
        http = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        url = f"http://127.0.0.1:{http.server_port}"
        headers = {"X-BBM-Proxy-Secret": "test-proxy-secret", "X-BBM-Remote-User": "test-user",
                   "X-Requested-With": "billberries-mobile"}
        settings = dict(server.load_settings(), calendarReminderMinutes=45)
        request = urllib.request.Request(url + "/api/settings", json.dumps(settings).encode(),
                                         {**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)["calendarReminderMinutes"], 45)
        with urllib.request.urlopen(urllib.request.Request(url + "/api/settings", headers=headers)) as response:
            self.assertEqual(json.load(response)["calendarReminderMinutes"], 45)
        self.assertEqual(server.load_state()["settings"]["calendarReminderMinutes"], 45)
        with patch.object(server, "submit_reservation", return_value=("<html>confirmed</html>", "failed")):
            request = urllib.request.Request(url + "/api/reservation", urllib.parse.urlencode(form()).encode(), headers)
            # Only the client-allowed fields, not the full internal form.
            request.data = urllib.parse.urlencode({k: v for k, v in form().items() if k in server.ALLOWED_FIELDS}).encode()
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["X-BBM-Calendar-Mail"], "failed")


if __name__ == "__main__":
    unittest.main()
