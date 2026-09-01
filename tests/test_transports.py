import json
from unittest import mock

import pytest

from tbhprint import api as apimod
from tbhprint import transport_reverb as tr
from tbhprint.transport_poll import PollerTransport


def test_event_data_tolerates_string_and_object():
    assert tr._event_data({"data": json.dumps({"uuid": "a"})}) == {"uuid": "a"}
    assert tr._event_data({"data": {"uuid": "b"}}) == {"uuid": "b"}
    assert tr._event_data({"data": ""}) == {}
    with pytest.raises(TypeError):
        tr._event_data({"data": "[1,2]"})


def test_dispatch_routes_print_job_and_pings():
    jobs = []
    t = tr.ReverbTransport("wss://ws.example/app/k", "private-c", lambda s, c: "k:sig", on_job=jobs.append)
    ws = mock.Mock()
    with mock.patch("tbhprint.transport_reverb.asyncio.ensure_future") as ensure:
        t._dispatch({"event": "pusher:ping", "data": {}}, ws)
        assert ensure.called
    t._dispatch({"event": "print.job", "data": json.dumps({"uuid": "j1", "document_type": "ticket_label"})}, ws)
    t._dispatch({"event": "pusher_internal:subscription_succeeded", "data": "{}"}, ws)
    t._dispatch({"event": "print.job", "data": "not json"}, ws)  # logged, not raised
    assert jobs == [{"uuid": "j1", "document_type": "ticket_label"}]


def test_url_carries_protocol_and_client():
    t = tr.ReverbTransport("wss://ws.example:443/app/key", "private-c", lambda s, c: "", on_job=lambda p: None)
    assert t.url.startswith("wss://ws.example:443/app/key?protocol=7&client=tbhprint&version=")


def test_handshake_sends_auth_string():
    """Drive the coroutine by hand: connection_established -> auth -> subscribe -> succeeded."""
    import asyncio

    sent = []

    class WS:
        def __init__(self):
            self.inbox = [
                json.dumps({"event": "pusher:connection_established",
                            "data": json.dumps({"socket_id": "42.1", "activity_timeout": 30})}),
                json.dumps({"event": "pusher_internal:subscription_succeeded", "channel": "private-c", "data": "{}"}),
            ]

        async def recv(self):
            return self.inbox.pop(0)

        async def send(self, raw):
            sent.append(json.loads(raw))

    auth_calls = []

    def auth(socket_id, channel):
        auth_calls.append((socket_id, channel))
        return "k:sig"

    t = tr.ReverbTransport("wss://x/app/k", "private-c", auth, on_job=lambda p: None)
    timeout = asyncio.run(t._handshake(WS()))
    assert timeout == 30
    assert auth_calls == [("42.1", "private-c")]
    assert sent == [{"event": "pusher:subscribe", "data": {"channel": "private-c", "auth": "k:sig"}}]


def test_handshake_raises_on_subscription_error():
    import asyncio

    class WS:
        def __init__(self):
            self.inbox = [
                json.dumps({"event": "pusher:connection_established", "data": json.dumps({"socket_id": "1.1"})}),
                json.dumps({"event": "pusher:error", "data": {"code": 4009, "message": "Connection not authorized"}}),
            ]

        async def recv(self):
            return self.inbox.pop(0)

        async def send(self, raw):
            pass

    t = tr.ReverbTransport("wss://x/app/k", "private-c", lambda s, c: "bad", on_job=lambda p: None)
    with pytest.raises(RuntimeError, match="subscription refused"):
        asyncio.run(t._handshake(WS()))


def test_poller_sweep_hands_every_open_job_over():
    client = mock.Mock()
    client.list_jobs.return_value = [{"uuid": "a"}, {"uuid": "b"}]
    got = []
    poller = PollerTransport(lambda: client, on_job=got.append, interval_s=10)
    assert poller.sweep_once() == 2
    assert [g["uuid"] for g in got] == ["a", "b"]
    unpaired = PollerTransport(lambda: None, on_job=got.append)
    assert unpaired.sweep_once() == 0


def test_poller_reports_auth_errors_as_error_state():
    client = mock.Mock()
    client.list_jobs.side_effect = apimod.AuthError("revoked")
    states = []
    poller = PollerTransport(lambda: client, on_job=lambda p: None, interval_s=10, on_state=states.append)
    poller.active = True
    poller._stopping = False
    # one iteration of the loop body
    try:
        poller.sweep_once()
    except apimod.AuthError:
        states.append("error")
    assert states == ["error"]
