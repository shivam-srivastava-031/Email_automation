"""python manage.py run_worker

Keeps running and does everything on its own: lets the AI write emails and
follow-ups, sends them one at a time (business hours, daily limit, pacing),
checks and sorts replies, and protects the account. Stop with Ctrl+C.
Use --once to do a single pass (handy for a scheduled task).

Only one worker may run at a time (a PostgreSQL advisory lock), so the
background task and a worker started by hand can never double-send: the
second one waits until the first stops.
"""
import time

from django.core.management.base import BaseCommand
from django.db import connection

from mailer.campaigns import tick

LOCK_KEY = 815_204_031  # any constant; identifies "the email worker"


class Command(BaseCommand):
    help = "Write, send, follow up and sort replies automatically, protecting the Gmail account."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Do one pass and exit.")
        parser.add_argument("--sleep", type=float, default=5.0, help="Seconds between passes (default 5).")

    def _lock(self):
        if connection.vendor != "postgresql":
            return True
        with connection.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
            return bool(cur.fetchone()[0])

    def handle(self, *args, **opts):
        if not self._lock():
            self.stdout.write("Another worker is already running; this one is on standby.")
            while not self._lock():
                time.sleep(15)
            self.stdout.write("The other worker stopped; taking over.")
        if opts["once"]:
            self._report(tick())
            return
        self.stdout.write("Worker running. Press Ctrl+C to stop.")
        try:
            while True:
                try:
                    self._report(tick())
                except Exception as exc:  # keep the worker alive; the next pass retries
                    self.stderr.write(f"Worker error: {exc}")
                    connection.close_if_unusable_or_obsolete()
                time.sleep(opts["sleep"])
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")

    def _report(self, result):
        if result.get("followups"):
            self.stdout.write(f"Queued {result['followups']} follow-up(s).")
        if result.get("replies"):
            self.stdout.write(f"New replies: {result['replies']} (see the Inbox tab).")
        if result["written"]:
            self.stdout.write(f"AI wrote {result['written']} email(s).")
        msg = result["sent"]
        if msg is not None:
            label = {"sent": "Sent", "failed": "Failed", "skipped": "Skipped"}.get(msg.status, "Tried")
            what = f"follow-up {msg.step} " if msg.step else ""
            line = f"{label} {what}to {msg.contact.email} ({msg.campaign.name})"
            if msg.error:
                line += f" - {msg.error}"
            self.stdout.write(line)
