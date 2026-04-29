"""
Email backends for production-like environments where real SMTP must stay off.
"""

import logging

from django.core.mail.backends.base import BaseEmailBackend

log = logging.getLogger("attendee.email")


class ConsoleLogEmailBackend(BaseEmailBackend):
    """
    Do not send mail over the network. Log the full message (headers + body) on the
    root/django logging stack (typically StreamHandler -> container stdout) so
    `docker logs` and signup/reset flows are debuggable without Mailgun.
    """

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        num_sent = 0
        for message in email_messages:
            num_sent += 1
            try:
                raw = message.message().as_string()
            except Exception as exc:  # noqa: BLE001
                log.exception("Failed to serialize email for logging: %s", exc)
                raw = f"Subject: {getattr(message, 'subject', '')!r}\nTo: {getattr(message, 'to', '')!r}\n"
            if len(raw) > 64000:
                raw = raw[:64000] + "\n... [truncated by ConsoleLogEmailBackend]\n"
            log.info("Email (not sent via SMTP; console log only)\n%s", raw)
        return num_sent
