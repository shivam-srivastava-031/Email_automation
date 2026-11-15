"""python manage.py run_worker

Keeps running: lets the AI write drafts and sends queued emails one at a
time, within the daily limit and SEND_INTERVAL_SECONDS apart. Stop with Ctrl+C.
Use --once to do a single pass (handy for a scheduled task).
"""
import time

from django.core.management.base import BaseCommand

from mailer.campaigns import tick


class Command(BaseCommand):
    help = "Write AI drafts and send queued emails, pacing them to protect the Gmail account."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Do one pass and exit.")
        parser.add_argument("--sleep", type=float, default=5.0, help="Seconds between passes (default 5).")

    def handle(self, *args, **opts):
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
                time.sleep(opts["sleep"])
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")

    def _report(self, result):
        if result["written"]:
            self.stdout.write(f"AI wrote {result['written']} email(s).")
        msg = result["sent"]
        if msg is not None:
            label = {"sent": "Sent", "failed": "Failed", "skipped": "Skipped"}.get(msg.status, "Tried")
            line = f"{label}: {msg.contact.email} ({msg.campaign.name})"
            if msg.error:
                line += f" - {msg.error}"
            self.stdout.write(line)
