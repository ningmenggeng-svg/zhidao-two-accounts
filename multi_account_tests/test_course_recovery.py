import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from test_local_multi import Manager, Playback, Store, add_ready, wait_for
from app.local_multi import service
from app.local_multi.failures import CourseReadError, classify_failure
from app.local_multi.service import friendly_error
from app.services.course.zhihuishu.errors import PlatformResponseError
from app.services.course.zhihuishu.learning import _response_data


class Faults:
    def __init__(self, playback, outcomes):
        self.playback, self.outcomes = playback, outcomes
        self.calls, self.closed = [], []
        self.lock = threading.Lock()

    def factory(self):
        adapter = self.playback.factory()
        original = adapter.get_courses

        def courses():
            owner = adapter.owner
            with self.lock:
                self.calls.append(owner)
                errors = self.outcomes.setdefault(owner, [])
                error = errors.pop(0) if errors else None
            if error:
                raise RuntimeError(
                    "REMOTE-SECRET https://example.invalid/?cookie=secret-cookie"
                ) from error
            return original()

        adapter.get_courses = courses
        adapter.learning = SimpleNamespace(
            session=SimpleNamespace(close=lambda: self.closed.append(adapter.owner))
        )
        return adapter


@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "COURSE_RETRY_DELAYS", (0, 0))
    playback = Playback(duration=0.1)
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    account = add_ready(manager, "alice")
    return manager, playback, account


def test_wrapped_timeouts_recover_with_fresh_sessions_and_keep_selection(ready, caplog):
    manager, playback, account = ready
    private_before = manager.store.account(account["id"], private=True)
    faults = Faults(
        playback,
        {
            "alice": [
                requests.ReadTimeout("secret-cookie"),
                requests.ConnectionError("SECRET"),
            ]
        },
    )
    manager.factory = faults.factory
    # An old stop flag must not cancel an idle manual course refresh.
    manager.stop_event.set()
    with caplog.at_level(logging.WARNING):
        result = manager.refresh_courses(account["id"])
    assert result["selected"] == account["selected"]
    assert faults.calls == ["alice"] * 3 and faults.closed == ["alice"] * 3
    assert (
        manager.store.account(account["id"], private=True)["credentials"]
        == private_before["credentials"]
    )
    assert "category=network_timeout" in caplog.text
    assert "category=network_connection" in caplog.text
    assert all(
        secret not in caplog.text
        for secret in ("REMOTE-SECRET", "secret-cookie", "example.invalid", "SECRET")
    )


def test_timeout_exhaustion_does_not_claim_expired_or_erase_courses(ready, caplog):
    manager, playback, account = ready
    before = manager.store.account(account["id"], private=True)
    faults = Faults(playback, {"alice": [requests.ReadTimeout("private-response")] * 3})
    manager.factory = faults.factory
    with caplog.at_level(logging.WARNING), pytest.raises(ValueError) as error:
        manager.refresh_courses(account["id"])
    assert "重试 2 次" in str(error.value) and "超时" in str(error.value)
    assert "过期" not in str(error.value) and "登录" not in str(error.value)
    assert manager.store.account(account["id"], private=True) == before
    assert len(faults.calls) == len(faults.closed) == 3
    assert "private-response" not in caplog.text


@pytest.mark.parametrize(
    ("error", "word"),
    [
        (PlatformResponseError("http_status", http_status=401), "过期"),
        (PlatformResponseError("platform_code", code=401), "过期"),
        (PlatformResponseError("http_status", http_status=403), "验证"),
        (PlatformResponseError("http_status", http_status=429), "频率"),
        (requests.exceptions.SSLError("private TLS details"), "安全连接"),
        (RuntimeError("unexpected private-response"), "暂不能确认"),
    ],
)
def test_nontransient_failures_do_not_retry_or_misreport(ready, error, word):
    manager, playback, account = ready
    faults = Faults(playback, {"alice": [error]})
    manager.factory = faults.factory
    with pytest.raises(ValueError) as failure:
        manager.refresh_courses(account["id"])
    assert word in str(failure.value)
    assert len(faults.calls) == len(faults.closed) == 1
    assert "private" not in str(failure.value)
    if word != "过期":
        assert "过期" not in str(failure.value)


def test_expired_qr_session_keeps_qr_relogin_instruction(ready):
    manager, playback, account = ready
    manager.store.save_cookies(account["id"], {"owner": "alice"}, qr_only=True)
    manager.factory = Faults(
        playback, {"alice": [PlatformResponseError("http_status", http_status=401)]}
    ).factory
    with pytest.raises(ValueError, match="扫码登录状态已过期"):
        manager.refresh_courses(account["id"])


def test_transient_platform_error_recovers_without_relogin(ready):
    manager, playback, account = ready
    faults = Faults(
        playback, {"alice": [PlatformResponseError("http_status", http_status=503)]}
    )
    manager.factory = faults.factory
    assert manager.refresh_courses(account["id"])["courses"]
    assert len(faults.calls) == 2 and not manager.password_login.active


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (401, "session_invalid", False),
        (403, "access_denied", False),
        (429, "rate_limited", False),
        (503, "platform_unavailable", True),
    ],
)
def test_response_metadata_survives_wrapper_without_body(status, kind, retryable):
    response = SimpleNamespace(
        status_code=status, json=lambda: {"token": "SECRET-REMOTE"}
    )
    try:
        try:
            _response_data(response)
        except ValueError as error:
            raise RuntimeError("outer wrapper") from error
    except RuntimeError as error:
        failure = classify_failure(error)
    assert failure.kind == kind and failure.retryable is retryable
    assert failure.http_status == status
    assert "SECRET" not in friendly_error(CourseReadError(failure, 1))


def test_malformed_json_can_retry_and_valid_empty_enrollment_stays_empty():
    def malformed():
        raise json.JSONDecodeError("private-response", "", 0)

    with pytest.raises(PlatformResponseError) as error:
        _response_data(SimpleNamespace(status_code=200, json=malformed))
    assert classify_failure(error.value).kind == "invalid_response"
    assert classify_failure(error.value).retryable
    response = SimpleNamespace(
        status_code=200, json=lambda: {"code": 200, "result": {"totalCount": 0}}
    )
    assert (
        _response_data(response, required_key="courseOpenDtos")["courseOpenDtos"] == []
    )


def test_stopping_during_backoff_cancels_without_another_request(ready, monkeypatch):
    manager, playback, account = ready
    monkeypatch.setattr(service, "COURSE_RETRY_DELAYS", (3, 3))
    faults = Faults(playback, {"alice": [requests.ReadTimeout("private")]})
    manager.factory = faults.factory
    manager.start()
    wait_for(
        lambda: (
            bool(manager.state["logs"])
            and any("重试" in row["message"] for row in manager.state["logs"])
        )
    )
    manager.control("stop")
    wait_for(lambda: not manager.active, timeout=1)
    assert faults.calls == ["alice"] and not playback.calls
    assert manager.state["status"] == "cancelled"
    assert all(job["status"] == "skipped" for job in manager.state["jobs"])


def test_pause_during_backoff_waits_for_resume(ready, monkeypatch):
    manager, playback, account = ready
    monkeypatch.setattr(service, "COURSE_RETRY_DELAYS", (0.15, 0))
    faults = Faults(playback, {"alice": [requests.ReadTimeout("private")]})
    manager.factory = faults.factory
    manager.start()
    wait_for(lambda: len(faults.calls) == 1)
    manager.control("pause")
    time.sleep(0.25)
    assert len(faults.calls) == 1 and not playback.calls
    manager.control("resume")
    wait_for(lambda: not manager.active)
    assert manager.state["status"] == "completed"


def test_both_accounts_retry_independently_without_mixing_sessions(ready):
    manager, playback, account = ready
    add_ready(manager, "bob")
    faults = Faults(
        playback,
        {
            "alice": [requests.ReadTimeout("private")],
            "bob": [requests.ConnectionError("private")],
        },
    )
    manager.factory = faults.factory
    manager.update_settings(
        {
            **manager.store.settings(),
            "execution_mode": "parallel",
            "parallel_accounts": 2,
        }
    )
    manager.start()
    wait_for(lambda: not manager.active)
    assert manager.state["status"] == "completed" and playback.peak == 2
    assert faults.calls.count("alice") == faults.calls.count("bob") == 2
    assert len(playback.calls) == 4
