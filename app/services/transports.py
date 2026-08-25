"""Email transports (spec §9): how a rendered message actually leaves.

``Transport`` is the interface :mod:`app.services.mailer` sends through.
Two implementations:

* :class:`FakeTransport` -- in-memory, records every send. Used by tests and
  local dev. Never touches the network.
* :class:`BrevoTransport` -- the real Brevo transactional email API, called
  with :mod:`urllib.request` only (standard library, no third-party HTTP
  client). Selection is driven by ``Config.email_transport``
  (``"fake"`` | ``"brevo"``) via :func:`build_transport`.

Both return a :class:`ProviderResult` rather than raising, so a network or
provider failure is an ordinary retryable outcome for the caller (spec §9.4)
instead of an exception that would need to unwind through a transaction.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Config


@dataclass(frozen=True)
class Message:
    """A fully-rendered outbound email, ready to hand to a transport."""

    to_email: str
    subject: str
    html: str
    text: str
    to_name: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    message_id: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Transport(Protocol):
    def send(self, message: Message) -> ProviderResult: ...


class FakeTransport:
    """In-memory transport for tests and local dev.

    ``sent`` accumulates every message that was actually delivered (not ones
    made to fail via :meth:`fail_next`), in delivery order, so a test can
    assert on subject/body content directly.
    """

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self._fail_next = 0
        self._fail_error = "simulated failure"

    def fail_next(self, times: int = 1, *, error: str = "simulated failure") -> None:
        """Make the next ``times`` calls to :meth:`send` report failure.

        Used by retry tests (spec §12 D5) to simulate a flaky provider
        without any real network access.
        """
        self._fail_next = times
        self._fail_error = error

    def send(self, message: Message) -> ProviderResult:
        if self._fail_next > 0:
            self._fail_next -= 1
            return ProviderResult(message_id=None, error=self._fail_error)
        self.sent.append(message)
        return ProviderResult(message_id=f"fake-{len(self.sent)}")


class BrevoTransport:
    """Real transport against Brevo's transactional email API.

    Must never be exercised by the test suite (this machine has no outbound
    network) -- selection always resolves to :class:`FakeTransport` unless
    ``Config.email_transport == "brevo"``, which nothing in ``tests/`` sets.
    """

    API_URL = "https://api.brevo.com/v3/smtp/email"

    def __init__(self, config: Config) -> None:
        self._config = config

    def send(self, message: Message) -> ProviderResult:
        recipient: dict[str, str] = {"email": message.to_email}
        if message.to_name:
            recipient["name"] = message.to_name
        payload = {
            "sender": {
                "email": self._config.email_from,
                "name": self._config.email_from_name,
            },
            "to": [recipient],
            "subject": message.subject,
            "htmlContent": message.html,
            "textContent": message.text,
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.API_URL,
            data=body,
            method="POST",
            headers={
                "api-key": self._config.email_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                status = response.getcode()
                if not (200 <= status < 300):
                    return ProviderResult(
                        message_id=None, error=f"http_{status}: {raw[:500]!r}"
                    )
                data = json.loads(raw) if raw else {}
                return ProviderResult(message_id=data.get("messageId"))
        except urllib.error.HTTPError as exc:
            detail = exc.read()
            return ProviderResult(
                message_id=None, error=f"http_{exc.code}: {detail[:500]!r}"
            )
        except urllib.error.URLError as exc:
            return ProviderResult(message_id=None, error=f"network_error: {exc.reason}")
        except (TimeoutError, OSError) as exc:
            return ProviderResult(message_id=None, error=f"network_error: {exc}")
        except Exception as exc:  # noqa: BLE001 - a send must never raise
            return ProviderResult(message_id=None, error=f"unexpected_error: {exc}")


def build_transport(config: Config) -> Transport:
    """Select the transport per ``Config.email_transport``."""
    if config.email_transport == "brevo":
        return BrevoTransport(config)
    return FakeTransport()


__all__ = [
    "Message",
    "ProviderResult",
    "Transport",
    "FakeTransport",
    "BrevoTransport",
    "build_transport",
]
