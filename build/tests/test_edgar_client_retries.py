"""
Two review rounds in a row flagged the same gap: EdgarClient.get()'s retry
logic (timeout/connection-error retry, 5xx retry, 429 backoff, 403
fail-fast) had no dedicated tests, even though none of them need a
database or the live network -- mocking client.session.get is enough. The
third review round called this "cheap confidence" worth adding before
leaving the worker unattended. These four tests are exactly that.
"""
import sys
import os
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import edgar_ingest_worker as ew


def make_client(monkeypatch):
    # Don't actually wait during tests -- these tests care about retry
    # COUNT and control flow, not real timing.
    monkeypatch.setattr(ew.time, "sleep", lambda *_args: None)
    return ew.EdgarClient("Diffusion Experiment Tests test@example.com", min_interval=0.0)


def make_response(status_code, headers=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    if status_code < 400:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.HTTPError(f"{status_code} error")
    return resp


def test_timeout_is_retried_and_eventually_succeeds(monkeypatch):
    client = make_client(monkeypatch)
    ok_response = make_response(200)
    client.session.get = MagicMock(side_effect=[requests.Timeout("simulated timeout"), ok_response])

    result = client.get("https://example.test/x")

    assert result is ok_response
    assert client.session.get.call_count == 2


def test_connection_error_is_retried_and_eventually_succeeds(monkeypatch):
    client = make_client(monkeypatch)
    ok_response = make_response(200)
    client.session.get = MagicMock(side_effect=[requests.ConnectionError("simulated drop"), ok_response])

    result = client.get("https://example.test/x")

    assert result is ok_response
    assert client.session.get.call_count == 2


def test_503_is_retried_and_eventually_succeeds(monkeypatch):
    client = make_client(monkeypatch)
    error_response = make_response(503)
    ok_response = make_response(200)
    client.session.get = MagicMock(side_effect=[error_response, ok_response])

    result = client.get("https://example.test/x")

    assert result is ok_response
    assert client.session.get.call_count == 2


def test_429_backs_off_and_eventually_succeeds(monkeypatch):
    client = make_client(monkeypatch)
    throttled_response = make_response(429, headers={"Retry-After": "1"})
    ok_response = make_response(200)
    client.session.get = MagicMock(side_effect=[throttled_response, ok_response])

    result = client.get("https://example.test/x")

    assert result is ok_response
    assert client.session.get.call_count == 2


def test_403_fails_fast_without_retrying(monkeypatch):
    """SEC's 403 means the User-Agent/request pattern itself is the
    problem -- the module's own comment says retrying this blindly would
    make things worse, not better. Confirms it raises after exactly ONE
    request rather than burning through retries against a block."""
    client = make_client(monkeypatch)
    forbidden_response = make_response(403)
    client.session.get = MagicMock(return_value=forbidden_response)

    with pytest.raises(RuntimeError, match="403"):
        client.get("https://example.test/x")

    assert client.session.get.call_count == 1
