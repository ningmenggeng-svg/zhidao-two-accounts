import json
from types import SimpleNamespace

import pytest
import requests

from test_local_multi import Manager, Playback, Store, add_ready
from app.local_multi import service
from app.local_multi.failures import classify_failure
from app.services.course.zhihuishu import auth as auth_module
from app.services.course.zhihuishu.auth import ZhihuishuAuth
from app.services.course.zhihuishu.errors import PlatformResponseError

IDENTITY = '{"uuid":"same-test-account"}'
ORIGINAL = {
    "CASLOGC": IDENTITY,
    "CASTGC": "private-sso-ticket",
    "SESSION": "expired-api-session",
}


class RedirectSession:
    def __init__(self, plans):
        self.plans = list(plans)
        self.cookies = requests.cookies.RequestsCookieJar()
        self.calls = []
        self.closed = False
        self.first_cookie_names = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def get(self, url, **kwargs):
        if self.first_cookie_names is None:
            self.first_cookie_names = {cookie.name for cookie in self.cookies}
        self.calls.append((url, kwargs))
        status, location, cookies = self.plans.pop(0)
        for name, value, domain in cookies:
            self.cookies.set(name, value, domain=domain, secure=True)
        return SimpleNamespace(
            status_code=status, headers={"Location": location} if location else {}
        )


@pytest.fixture
def auth():
    auth = ZhihuishuAuth()
    auth.cookies = ORIGINAL
    yield auth
    auth.session.close()


def ready_redirects(*, identity=IDENTITY):
    return [
        (
            302,
            "https://onlineservice-api.zhihuishu.com/callback?ticket=private-test-ticket",
            [],
        ),
        (
            302,
            "https://onlineweb.zhihuishu.com/",
            [
                ("SESSION", "fresh-api-session", "onlineservice-api.zhihuishu.com"),
                ("SESSION", "foreign-study-session", "studyservice-api.zhihuishu.com"),
                ("CASLOGC", identity, ".zhihuishu.com"),
            ],
        ),
        (200, None, []),
    ]


def test_official_sso_restores_api_session_without_password_or_browser(
    auth, monkeypatch
):
    session = RedirectSession(ready_redirects())
    monkeypatch.setattr(auth_module.requests, "Session", lambda: session)
    result = auth.refresh_session()
    assert result["SESSION"] == "fresh-api-session"
    assert result["CASTGC"] == ORIGINAL["CASTGC"]
    assert auth.uuid == "same-test-account"
    assert "SESSION" not in session.first_cookie_names
    assert "CASTGC" in session.first_cookie_names
    assert len(session.calls) == 3 and session.closed
    assert all(kwargs["allow_redirects"] is False for _, kwargs in session.calls)
    assert all(
        "password=" not in url and "account=" not in url and "pwd=" not in url
        for url, _ in session.calls
    )


def test_expired_sso_ticket_does_not_destroy_saved_cookies(auth, monkeypatch):
    before = dict(auth.cookies)
    session = RedirectSession([(200, None, [])])
    monkeypatch.setattr(auth_module.requests, "Session", lambda: session)
    with pytest.raises(ValueError, match="login_sso_expired"):
        auth.refresh_session()
    assert auth.cookies == before and session.closed


def test_refresh_cannot_change_the_account_identity(auth, monkeypatch):
    before = dict(auth.cookies)
    session = RedirectSession(ready_redirects(identity='{"uuid":"different-account"}'))
    monkeypatch.setattr(auth_module.requests, "Session", lambda: session)
    with pytest.raises(ValueError) as error:
        auth.refresh_session()
    assert classify_failure(error.value).kind == "identity_changed"
    assert auth.cookies == before and session.closed


@pytest.mark.parametrize(
    "location",
    [
        "https://unrelated.invalid/steal",
        "http://passport.zhihuishu.com/login",
        "https://zhihuishu.com.unrelated.invalid/login",
        "https://user:password@passport.zhihuishu.com/login",
        "https://passport.zhihuishu.com:444/login",
    ],
)
def test_refresh_rejects_unsafe_redirect_before_sending_any_cookies(
    auth, monkeypatch, location
):
    session = RedirectSession([(302, location, [])])
    monkeypatch.setattr(auth_module.requests, "Session", lambda: session)
    with pytest.raises(ValueError, match="login_redirect_rejected"):
        auth.refresh_session()
    assert len(session.calls) == 1 and session.closed
    assert auth.cookies["SESSION"] == ORIGINAL["SESSION"]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "session_invalid"),
        (403, "access_denied"),
        (429, "rate_limited"),
        (503, "platform_unavailable"),
    ],
)
def test_sso_failure_keeps_specific_status_category(auth, monkeypatch, status, kind):
    session = RedirectSession([(status, None, [])])
    monkeypatch.setattr(auth_module.requests, "Session", lambda: session)
    with pytest.raises(PlatformResponseError) as error:
        auth.refresh_session()
    assert classify_failure(error.value).kind == kind
    assert auth.cookies["SESSION"] == ORIGINAL["SESSION"]


def test_redirect_loop_is_bounded_and_can_be_cancelled(auth, monkeypatch):
    plans = [(302, "https://passport.zhihuishu.com/login", [])] * 8
    session = RedirectSession(plans)
    monkeypatch.setattr(auth_module.requests, "Session", lambda: session)
    with pytest.raises(ValueError, match="login_sso_expired"):
        auth.refresh_session()
    assert len(session.calls) == 8
    session = RedirectSession(plans)
    with pytest.raises(InterruptedError):
        auth.refresh_session(is_cancelled=lambda: bool(session.calls))
    assert len(session.calls) == 1 and session.closed


def test_new_official_response_cookies_are_kept_but_other_sessions_are_ignored(auth):
    jar = requests.cookies.RequestsCookieJar()
    jar.set("SESSION", "new-api", domain="onlineservice-api.zhihuishu.com")
    jar.set("SESSION", "study-only", domain="studyservice-api.zhihuishu.com")
    jar.set("CASTGC", "foreign", domain="unrelated.invalid")
    assert auth.capture_session_cookies(jar)["SESSION"] == "new-api"
    assert auth.cookies["CASTGC"] == ORIGINAL["CASTGC"]
    jar.set("CASLOGC", '{"uuid":"different-account"}', domain=".zhihuishu.com")
    with pytest.raises(ValueError, match="login_identity_changed"):
        auth.capture_session_cookies(jar)
    assert auth.uuid == "same-test-account"


def make_restoring_factory(
    playback, counters, *, first_refresh_timeout=False, expired_sso=False
):
    def factory():
        adapter = playback.factory()
        original = adapter.get_courses

        def courses():
            counters["reads"] += 1
            if adapter.auth.cookies.get("SESSION") != "fresh":
                raise PlatformResponseError("http_status", http_status=401)
            return original()

        def refresh(*, is_cancelled=None):
            counters["refreshes"] += 1
            if expired_sso:
                raise ValueError("login_sso_expired")
            if first_refresh_timeout and counters["refreshes"] == 1:
                raise requests.ReadTimeout("private-sso-ticket")
            updated = {**adapter.auth.cookies, "SESSION": "fresh"}
            adapter.auth.cookies = updated
            return updated

        adapter.get_courses = courses
        adapter.auth.refresh_session = refresh
        adapter.session_cookies = lambda: dict(adapter.auth.cookies)
        return adapter

    return factory


def test_recovered_session_is_encrypted_and_reused_after_restart(tmp_path):
    playback = Playback()
    store = Store(tmp_path)
    manager = Manager(store, playback.factory)
    account = add_ready(manager, "alice")
    store.save_cookies(
        account["id"],
        {"owner": "alice", "SESSION": "expired", "CASTGC": "private-sso-ticket"},
        browser_only=True,
    )
    counters = {"reads": 0, "refreshes": 0}
    manager.factory = make_restoring_factory(playback, counters)
    manager.refresh_courses(account["id"])
    assert counters == {"reads": 3, "refreshes": 1}
    saved = store.account(account["id"], private=True)
    assert saved["credentials"]["cookies"]["SESSION"] == "fresh"
    assert (
        saved["credentials"]["browser_only"]
        and saved["selected"] == account["selected"]
    )
    assert b"private-sso-ticket" not in store.path.read_bytes()
    restarted = Manager(Store(tmp_path), make_restoring_factory(playback, counters))
    restarted.refresh_courses(account["id"])
    assert counters == {"reads": 4, "refreshes": 1}
    assert "private-sso-ticket" not in json.dumps(restarted.snapshot())


def test_transient_sso_failure_retries_without_asking_for_password(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(service, "COURSE_RETRY_DELAYS", (0, 0))
    playback = Playback()
    manager = Manager(Store(tmp_path), playback.factory)
    account = add_ready(manager, "alice")
    manager.store.save_cookies(
        account["id"], {"owner": "alice", "SESSION": "expired"}, browser_only=True
    )
    counters = {"reads": 0, "refreshes": 0}
    manager.factory = make_restoring_factory(
        playback, counters, first_refresh_timeout=True
    )
    assert manager.refresh_courses(account["id"])["courses"]
    assert counters["refreshes"] == 2
    assert not manager.password_login.active


def test_expired_sso_still_requires_manual_login_and_keeps_original_data(tmp_path):
    playback = Playback()
    manager = Manager(Store(tmp_path), playback.factory)
    account = add_ready(manager, "alice")
    manager.store.save_cookies(
        account["id"], {"owner": "alice", "SESSION": "expired"}, browser_only=True
    )
    before = manager.store.account(account["id"], private=True)
    counters = {"reads": 0, "refreshes": 0}
    manager.factory = make_restoring_factory(playback, counters, expired_sso=True)
    with pytest.raises(ValueError, match="登录状态已过期"):
        manager.refresh_courses(account["id"])
    assert manager.store.account(account["id"], private=True) == before
    assert counters == {"reads": 2, "refreshes": 1}


def test_discarding_only_rejected_api_session_can_restore_valid_cas_login(tmp_path):
    playback = Playback()
    manager = Manager(Store(tmp_path), playback.factory)
    account = add_ready(manager, "alice")
    manager.store.save_cookies(
        account["id"],
        {"owner": "alice", "SESSION": "expired", "CASTGC": "valid-ticket"},
        browser_only=True,
    )
    counters = {"refreshes": 0}

    def factory():
        adapter = playback.factory()
        original = adapter.get_courses

        def courses():
            if adapter.auth.cookies.get("SESSION") == "expired":
                raise PlatformResponseError("http_status", http_status=401)
            return original()

        def refresh(**kwargs):
            counters["refreshes"] += 1
            raise AssertionError("A valid saved login must not require another sign-in")

        adapter.get_courses = courses
        adapter.auth.refresh_session = refresh
        adapter.session_cookies = lambda: dict(adapter.auth.cookies)
        return adapter

    manager.factory = factory
    result = manager.refresh_courses(account["id"])
    saved = manager.store.account(account["id"], private=True)["credentials"]["cookies"]
    assert result["selected"] == account["selected"]
    assert "SESSION" not in saved and saved["CASTGC"] == "valid-ticket"
    assert counters["refreshes"] == 0
