"""Thin client for Resend (resend.com) - transactional email, used today
only for self-serve signup's verification link (auth/email_verification.py).
Plain REST via httpx rather than the `resend` package - one endpoint, not
worth a dependency for.

API: https://resend.com/docs/api-reference/emails/send-email - a flat
Bearer-token POST, JSON in, JSON out.
"""

import httpx

_TIMEOUT_SECONDS = 15
_API_URL = "https://api.resend.com/emails"


class ResendError(Exception):
    pass


class ResendClient:
    def __init__(self, api_key: str, from_email: str):
        self.api_key = api_key
        self.from_email = from_email

    def send(self, to: str, subject: str, html: str) -> dict:
        response = httpx.post(
            _API_URL,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"from": self.from_email, "to": [to], "subject": subject, "html": html},
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise ResendError(f"Resend send failed ({response.status_code}): {detail}")
        return response.json()
