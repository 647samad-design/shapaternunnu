"""SMTP backend that tries again when the mail server drops the line.

Gmail (and most providers) now and then close a connection in the middle
of a send ("Connection unexpectedly closed", 421/451/454 "try again
later", a reset or timeout). Those are not real failures - the same email
goes through a second later. This backend reconnects and resends that one
message up to two more times before giving up, so short hiccups never
reach customers or Admin > System check. Wrong passwords, refused
recipients and other permanent errors are not retried.
"""
import smtplib
import socket
import time

from django.core.mail.backends.smtp import EmailBackend

RETRIES = 2
WAIT = (1.5, 4.0)


def transient(exc):
    if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout,
                        ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return False
    code = getattr(exc, "smtp_code", None)
    return isinstance(code, int) and 400 <= code < 500


class RetrySMTPBackend(EmailBackend):

    def _pause(self, attempt):
        time.sleep(WAIT[min(attempt, len(WAIT) - 1)])

    def open(self):
        for attempt in range(RETRIES + 1):
            try:
                return super().open()
            except Exception as exc:
                if attempt == RETRIES or not transient(exc):
                    raise
                self.connection = None
                self._pause(attempt)

    def _send(self, email_message):
        for attempt in range(RETRIES + 1):
            failed = None
            try:
                # fail_silently is forced off for the attempt so a dropped
                # line comes back here instead of being swallowed.
                silent, self.fail_silently = self.fail_silently, False
                try:
                    return super()._send(email_message)
                finally:
                    self.fail_silently = silent
            except Exception as exc:
                failed = exc
            if attempt == RETRIES or not transient(failed):
                if self.fail_silently:
                    return False
                raise failed
            try:
                self.close()
            except Exception:
                pass
            self.connection = None
            self._pause(attempt)
            self.open()
        return False
