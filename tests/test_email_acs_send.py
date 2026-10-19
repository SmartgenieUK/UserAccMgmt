from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services.email_service import EmailService

# BRIEF-018 item 1: the ACS send returns once ACS has accepted the message (begin_send's 202). Waiting on the
# delivery poller held the portal's sign-up callback for ~21 s, past the portal's 3 s deadline.


class _BlockingPoller:
    def result(self, *args, **kwargs):
        raise AssertionError("the send waited on the delivery poller")


class _StubClient:
    calls: list = []
    reject = False

    def begin_send(self, message, **kwargs):
        if self.reject:
            raise RuntimeError("ACS refused the message")
        _StubClient.calls.append((message, kwargs))
        return _BlockingPoller()


@pytest.fixture()
def acs(monkeypatch):
    from azure.communication.email import EmailClient

    _StubClient.calls = []
    _StubClient.reject = False
    monkeypatch.setattr(EmailClient, "from_connection_string", classmethod(lambda cls, conn: _StubClient()))
    settings = get_settings().model_copy(
        update={"EMAIL_PROVIDER": "acs", "ACS_CONNECTION_STRING": "endpoint=https://x/;accesskey=k", "ALLOWED_EMAIL_DOMAINS": []}
    )
    return EmailService(settings)


async def test_the_acs_send_returns_on_acceptance_without_waiting_for_delivery(acs):
    await acs.send_verification_email("grace@example.com", "123456")
    [(message, kwargs)] = _StubClient.calls
    assert message["recipients"]["to"] == [{"address": "grace@example.com"}]
    assert kwargs.get("polling") is False  # no background poller thread left running per email


async def test_a_rejected_acs_send_still_raises(acs):
    _StubClient.reject = True
    with pytest.raises(RuntimeError):
        await acs.send_verification_email("grace@example.com", "123456")
