import json
import hashlib
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.local_multi.service import Manager, SHANGHAI, next_daily
from app.local_multi.store import Store
from app.local_multi.web import create_app


class Playback:
    """Models a slow cancellation acknowledgement, without contacting any platform."""

    def __init__(self, duration=0.03):
        self.duration = duration
        self.calls = []
        self.events = []
        self.live = 0
        self.peak = 0
        self.fail_course = None

    def factory(self):
        recorder = self

        class Adapter:
            def __init__(self):
                self.done = threading.Event()
                self.done.set()
                self.auth = SimpleNamespace(cookies={})
                self.owner = ""
                self.cancelled = threading.Event()
                self.paused = False
                self.task = {}

            def _init_services(self, cookies):
                self.owner = cookies["owner"]

            def login_with_password(self, username, password):
                raise AssertionError("The local app must never send a stored password")

            def get_courses(self):
                if self.auth.cookies.get("expired"):
                    raise ValueError("Expired session: SECRET-REMOTE-BODY")
                return [
                    {"courseId": self.owner + "-1", "courseName": "课程一"},
                    {"courseId": self.owner + "-2", "courseName": "课程二"},
                ]

            def start_course(self, course_id, **kwargs):
                assert course_id.startswith(self.owner)
                recorder.calls.append((self.owner, course_id))
                self.done.clear()
                self.cancelled.clear()
                self.task = {
                    "task_id": course_id,
                    "status": "running",
                    "percentage": 25,
                    "current_video": "第一节",
                }

                def play():
                    recorder.live += 1
                    recorder.peak = max(recorder.peak, recorder.live)
                    recorder.events.append(("start", course_id))
                    end = time.monotonic() + recorder.duration
                    while time.monotonic() < end or self.paused:
                        if self.cancelled.wait(0.005):
                            # A request is still in flight after cancel_task returned.
                            time.sleep(0.12)
                            self.task.update(status="cancelled")
                            break
                    else:
                        failed = recorder.fail_course == course_id
                        self.task.update(
                            status="error" if failed else "completed",
                            percentage=100,
                            failed=int(failed),
                        )
                    recorder.events.append(("end", course_id))
                    recorder.live -= 1
                    self.done.set()

                threading.Thread(target=play, daemon=True).start()
                return {"task_id": course_id, "status": "running"}

            def get_task(self, task_id):
                return self.task.copy()

            def cancel_task(self):
                self.paused = False
                self.cancelled.set()

            def pause_task(self):
                self.paused = True

            def resume_task(self):
                self.paused = False

        return Adapter()


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "Timed out waiting for worker"


def add_ready(manager, username, selected=None):
    account = manager.store.save(username)
    manager.store.save_cookies(account["id"], {"owner": username, "token": "secret-cookie"}, browser_only=True)
    refreshed = manager.refresh_courses(account["id"])
    manager.store.select(
        account["id"], selected or [c["id"] for c in refreshed["courses"]], True
    )
    return manager.store.account(account["id"])


def test_sessions_survive_restart_without_saving_passwords(tmp_path):
    recorder = Playback()
    manager = Manager(Store(tmp_path), recorder.factory)
    account = add_ready(manager, "alice")
    manager.store.put("test", {"hello": "世界"})
    raw = (tmp_path / "accounts.sqlite3").read_bytes()
    assert b"secret-password" not in raw and b"secret-cookie" not in raw
    restored = Store(tmp_path)
    private = restored.account(account["id"], private=True)
    assert "password" not in private["credentials"]
    assert private["credentials"]["cookies"]["token"] == "secret-cookie"
    public = json.dumps(manager.snapshot())
    assert (
        "credentials" not in public
        and "secret-password" not in public
        and "secret-cookie" not in public
    )
    assert restored.account(account["id"])["selected"] == ["alice-1", "alice-2"]
    restarted=Manager(restored,recorder.factory,poll_seconds=.005)
    restarted.start()
    wait_for(lambda:not restarted.active)
    assert restarted.state['status']=='completed' and not restarted.password_login.active


def test_edit_remark_retains_session_profile_and_course_selection(tmp_path):
    manager = Manager(Store(tmp_path), Playback().factory)
    a = add_ready(manager, "alice")
    previous=manager.store.account(a["id"],private=True)
    changed=manager.store.save("新备注",a["id"])
    current=manager.store.account(a["id"],private=True)
    assert changed["label"]=="新备注" and changed["selected"]==a["selected"]
    assert current["credentials"]==previous["credentials"]
    assert current["profile"]==previous["profile"]


def test_accounts_with_same_remark_use_isolated_profiles_and_validate_selection(tmp_path):
    manager = Manager(Store(tmp_path), Playback().factory)
    a = add_ready(manager, "alice")
    b=manager.store.save("alice")
    assert b["id"]!=a["id"]
    assert manager.store.account(b["id"],private=True)["credentials"]=={}
    with pytest.raises(ValueError):
        manager.store.select(a["id"], ["bob-1"], True)
    assert manager.store.account(a["id"])["selected"] == ["alice-1", "alice-2"]


def test_missing_key_never_silently_replaces_vault(tmp_path):
    Store(tmp_path)
    next(tmp_path.glob("vault.*")).unlink()
    with pytest.raises(RuntimeError, match="密钥文件丢失"):
        Store(tmp_path)


def test_selected_courses_follow_reordered_accounts_and_never_overlap(tmp_path):
    playback = Playback()
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    a = add_ready(manager, "alice", selected=["alice-2"])
    b = add_ready(manager, "bob", selected=["bob-1"])
    manager.store.reorder([b["id"], a["id"]])
    manager.start()
    wait_for(lambda: not manager.active)
    assert playback.calls == [("bob", "bob-1"), ("alice", "alice-2")]
    assert playback.peak == 1
    assert playback.events == [
        ("start", "bob-1"),
        ("end", "bob-1"),
        ("start", "alice-2"),
        ("end", "alice-2"),
    ]
    assert manager.state["status"] == "completed"


def test_invalid_reorder_is_atomic(tmp_path):
    store = Store(tmp_path)
    a = store.save("a")
    b = store.save("b")
    with pytest.raises(ValueError):
        store.reorder([b["id"], b["id"]])
    assert [a["id"] for a in store.accounts()] == [a["id"], b["id"]]


def test_unchecked_accounts_are_not_logged_in_or_executed(tmp_path):
    playback = Playback()
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    a = add_ready(manager, "alice")
    add_ready(manager, "bob", selected=["bob-2"])
    manager.store.select(a["id"], a["selected"], False)
    manager.start()
    wait_for(lambda: not manager.active)
    assert playback.calls == [("bob", "bob-2")]


def test_stop_waits_for_current_thread_and_does_not_start_next(tmp_path):
    playback = Playback(duration=2)
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    add_ready(manager, "alice")
    add_ready(manager, "bob")
    manager.start()
    wait_for(lambda: playback.live == 1)
    manager.control("stop")
    assert manager.active
    assert manager.state["status"] == "stopping"
    with pytest.raises(ValueError):
        manager.start()
    wait_for(lambda: not manager.active)
    assert playback.live == 0 and playback.calls == [("alice", "alice-1")]
    assert manager.state["status"] == "cancelled"
    assert [job["status"] for job in manager.state["jobs"]] == [
        "cancelled",
        "skipped",
        "skipped",
        "skipped",
    ]


def test_pause_resume_and_duplicate_start_guard(tmp_path):
    playback = Playback(duration=0.2)
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    add_ready(manager, "alice")
    manager.start()
    wait_for(lambda: playback.live == 1)
    manager.control("pause")
    time.sleep(0.25)
    assert len(playback.calls) == 1 and manager.state["status"] == "paused"
    with pytest.raises(ValueError):
        manager.start()
    manager.control("resume")
    wait_for(lambda: not manager.active)
    assert manager.state["status"] == "completed"


@pytest.mark.parametrize("continue_on_error,expected", [(True, 3), (False, 1)])
def test_failed_course_continues_only_when_configured(
    tmp_path, continue_on_error, expected
):
    playback = Playback()
    playback.fail_course = "alice-1"
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    add_ready(manager, "alice")
    add_ready(manager, "bob", selected=["bob-1"])
    manager.update_settings(
        {**manager.store.settings(), "continue_on_error": continue_on_error}
    )
    manager.start()
    wait_for(lambda: not manager.active)
    assert len(playback.calls) == expected
    assert manager.state["jobs"][0]["status"] == "failed"


def test_failed_login_skips_that_account_and_continues(tmp_path):
    playback = Playback()
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    a = add_ready(manager, "alice")
    manager.store.save_cookies(a["id"],{"owner":"alice","expired":True},browser_only=True)
    add_ready(manager, "bob", selected=["bob-1"])
    manager.start()
    wait_for(lambda: not manager.active)
    assert playback.calls == [("bob", "bob-1")]
    assert all(j["status"] == "failed" for j in manager.state["jobs"][:2])
    assert "SECRET-REMOTE-BODY" not in json.dumps(manager.snapshot())


def test_time_limit_waits_for_cancel_before_next_account(tmp_path):
    playback = Playback(duration=5)
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    add_ready(manager, "alice", selected=["alice-1"])
    add_ready(manager, "bob", selected=["bob-1"])
    # Small duration for a fast timer test; the API only accepts integer minutes.
    manager.store.put("settings", {"course_minutes": 0.0002})
    manager.start()
    wait_for(lambda: not manager.active)
    assert playback.peak == 1
    assert all(job["status"] == "limited" for job in manager.state["jobs"])


def test_schedule_uses_beijing_time_and_advances_once(tmp_path):
    playback = Playback()
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    add_ready(manager, "alice", selected=["alice-1"])
    before = datetime(2026, 10, 25, 8, 59, tzinfo=SHANGHAI)
    assert next_daily("09:00", before) == "2026-10-25T09:00:00+08:00"
    manager.store.put(
        "settings",
        {
            "daily_enabled": True,
            "daily_time": "09:00",
            "next_run": next_daily("09:00", before),
        },
    )
    at = before.replace(hour=9, minute=0)
    manager.tick(at)
    manager.tick(at)
    wait_for(lambda: not manager.active)
    manager.tick(at)
    assert len(playback.calls) == 1
    assert manager.state["source"] == "daily"
    assert manager.store.settings()["next_run"] == "2026-10-26T09:00:00+08:00"


def test_busy_queue_is_not_duplicated_by_scheduler(tmp_path):
    playback = Playback(duration=0.2)
    manager = Manager(Store(tmp_path), playback.factory, poll_seconds=0.005)
    add_ready(manager, "alice", selected=["alice-1"])
    manager.start()
    at = datetime(2026, 10, 25, 9, tzinfo=SHANGHAI)
    manager.store.put(
        "settings",
        {"daily_enabled": True, "daily_time": "09:00", "next_run": at.isoformat()},
    )
    manager.tick(at)
    wait_for(lambda: not manager.active)
    assert len(playback.calls) == 1
    assert any("不重复启动" in log["message"] for log in manager.state["logs"])


def test_interrupted_queue_is_not_silently_restarted(tmp_path):
    store = Store(tmp_path)
    store.put(
        "queue", {"status": "running", "jobs": [{"status": "running"}], "logs": []}
    )
    manager = Manager(store, Playback().factory)
    assert manager.state["status"] == "interrupted"
    assert not manager.active
    assert manager.state["jobs"][0]["status"] == "interrupted"


def test_api_lifecycle_selection_and_no_secret_response(tmp_path):
    app = create_app(tmp_path, factory=Playback().factory, scheduler=False)
    with TestClient(
        app, base_url="http://127.0.0.1", headers={"X-Local-Request": "1"}
    ) as client:
        a = client.post(
            "/api/accounts", json={"label": "alice"}
        ).json()
        assert client.post(f"/api/accounts/{a['id']}/courses").status_code == 400
        app.state.manager.store.save_cookies(a["id"],{"owner":"alice","token":"secret-cookie"},browser_only=True)
        assert client.post(f"/api/accounts/{a['id']}/courses").status_code == 200
        assert (
            client.put(
                f"/api/accounts/{a['id']}/selection", json={"selected": ["alice-2"]}
            ).status_code
            == 200
        )
        public = client.get("/api/state").text
        assert (
            "private-password" not in public
            and "secret-cookie" not in public
            and "credentials" not in public
        )
        assert client.post("/api/queue/start").status_code == 200
        wait_for(lambda: not app.state.manager.active)
        assert client.get("/api/state").json()["queue"]["status"] == "completed"
        assert client.get("/").status_code == 200
        assert client.get("/app.js").status_code == 200
        assert client.delete(f"/api/accounts/{a['id']}").status_code == 200
        assert client.get("/api/state").json()["accounts"] == []


def test_legacy_upgrade_removes_password_and_preserves_session_and_profile(tmp_path):
    manager=Manager(Store(tmp_path),Playback().factory)
    account=add_ready(manager,"alice")
    store=manager.store
    profile=account["id"]+"-"+hashlib.sha256(b"alice").hexdigest()[:12]
    profile_directory=tmp_path/'browser-profiles'/profile
    profile_directory.mkdir(parents=True)
    (profile_directory/'marker').write_text('test profile')
    legacy={"password":"legacy-private-password","cookies":{"owner":"alice","token":"secret-cookie"},"browser_only":True}
    with store.connection() as db:
        db.execute('ALTER TABLE accounts RENAME COLUMN profile TO username')
        db.execute('UPDATE accounts SET username=?,secret=? WHERE id=?',('alice',store.cipher.encrypt(json.dumps(legacy).encode()).decode(),account['id']))
    upgraded=Store(tmp_path)
    private=upgraded.account(account['id'],private=True)
    assert 'password' not in private['credentials']
    assert private['credentials']['cookies']==legacy['cookies']
    assert private['profile']==profile and upgraded.browser_directory(private['profile'])==profile_directory
    assert private['selected']==account['selected'] and private['courses']==account['courses']
    assert 'username' not in upgraded.account(account['id']) and 'profile' not in upgraded.account(account['id'])
    assert Store(tmp_path).account(account['id'],private=True)['profile']==profile


def test_delete_account_removes_only_its_owned_browser_profile(tmp_path):
    manager=Manager(Store(tmp_path),Playback().factory)
    a,b=add_ready(manager,'alice'),add_ready(manager,'bob')
    paths=[]
    for account in (a,b):
        path=manager.store.browser_directory(manager.store.account(account['id'],private=True)['profile'])
        path.mkdir(parents=True)
        (path/'Cookies').write_text('mock session')
        paths.append(path)
    manager.store.delete(a['id'])
    assert not paths[0].exists() and (paths[1]/'Cookies').exists()
    assert manager.store.account(b['id'])['selected']==b['selected']
    assert len(manager.store.accounts())==1


def test_delete_rejects_browser_path_outside_account_directory(tmp_path):
    store=Store(tmp_path/'vault')
    account=store.save('test')
    outside=tmp_path/'outside'
    outside.mkdir()
    marker=outside/'keep.txt'
    marker.write_text('keep')
    with store.connection() as db:
        db.execute('UPDATE accounts SET profile=? WHERE id=?',('../../outside',account['id']))
    with pytest.raises(ValueError,match='路径无效'):
        store.delete(account['id'])
    assert marker.read_text()=='keep' and len(store.accounts())==1


def test_parallel_accounts_overlap_but_each_account_keeps_its_course_order(tmp_path):
    playback=Playback(duration=.12)
    manager=Manager(Store(tmp_path),playback.factory,poll_seconds=.005)
    for name in ('alice','bob'):
        add_ready(manager,name)
    manager.update_settings({**manager.store.settings(),'execution_mode':'parallel','parallel_accounts':2})
    manager.start()
    wait_for(lambda:not manager.active)
    assert playback.peak==2 and manager.state['status']=='completed'
    for name in ('alice','bob'):
        own=[event for event in playback.events if event[1].startswith(name+'-')]
        assert own==[('start',name+'-1'),('end',name+'-1'),('start',name+'-2'),('end',name+'-2')]


def test_parallel_pause_and_stop_wait_for_all_workers_and_skip_remaining_courses(tmp_path):
    playback=Playback(duration=3)
    manager=Manager(Store(tmp_path),playback.factory,poll_seconds=.005)
    for name in ('alice','bob'):
        add_ready(manager,name)
    manager.update_settings({**manager.store.settings(),'execution_mode':'parallel','parallel_accounts':2})
    manager.start()
    wait_for(lambda:playback.live==2)
    manager.control('pause')
    time.sleep(.05)
    assert len(playback.calls)==2 and manager.state['status']=='paused'
    manager.control('resume')
    manager.control('stop')
    assert manager.active
    wait_for(lambda:not manager.active)
    assert playback.live==0 and len(playback.calls)==2
    assert manager.current_adapters=={}
    assert all(job['status'] in {'cancelled','skipped'} for job in manager.state['jobs'])


def test_explicit_account_choice_runs_only_that_account_even_if_unchecked(tmp_path):
    playback=Playback()
    manager=Manager(Store(tmp_path),playback.factory,poll_seconds=.005)
    add_ready(manager,'alice')
    b=add_ready(manager,'bob',selected=['bob-2'])
    manager.store.select(b['id'],b['selected'],False)
    with pytest.raises(ValueError):manager.start(account_ids=['missing'])
    manager.start(account_ids=[b['id']])
    wait_for(lambda:not manager.active)
    assert playback.calls==[('bob','bob-2')]
    assert manager.state['jobs'][0]['account_id']==b['id']


def test_legacy_upgrade_failure_keeps_original_schema_and_data(tmp_path):
    import sqlite3
    from cryptography.fernet import InvalidToken
    store=Store(tmp_path)
    a,b=store.save('a'),store.save('b')
    token=store.cipher.encrypt(b'{"password":"old-test-password"}').decode()
    with store.connection() as db:
        db.execute('ALTER TABLE accounts RENAME COLUMN profile TO username')
        db.execute('UPDATE accounts SET username=?,secret=? WHERE id=?',('a',token,a['id']))
        db.execute('UPDATE accounts SET username=?,secret=? WHERE id=?',('b','corrupted',b['id']))
    with pytest.raises(InvalidToken):Store(tmp_path)
    with sqlite3.connect(store.path) as db:
        assert 'username' in {row[1] for row in db.execute('PRAGMA table_info(accounts)')}
        assert db.execute('SELECT secret FROM accounts WHERE id=?',(a['id'],)).fetchone()[0]==token


def test_api_rejects_cross_site_and_invalid_values_without_echoing_secrets(tmp_path):
    app = create_app(tmp_path, scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/state").status_code == 403
        assert (
            client.get(
                "/api/state",
                headers={"X-Local-Request": "1", "Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )
        assert client.get("/", headers={"Host": "evil.invalid"}).status_code == 403
        invalid = client.post(
            "/api/accounts",
            headers={"X-Local-Request": "1"},
            json={"username": "", "password": "NEVER-ECHO"},
        )
        assert invalid.status_code == 422 and "NEVER-ECHO" not in invalid.text
        assert (
            client.put(
                "/api/settings",
                headers={"X-Local-Request": "1"},
                json={"daily_time": "25:70"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/queue/start", headers={"X-Local-Request": "1"}
            ).status_code
            == 400
        )


def test_production_adapter_initializes_without_remote_requests():
    from app.local_multi.service import adapter_factory

    adapter = adapter_factory()
    assert adapter.done.is_set()
    assert adapter.learning is None


def test_original_playback_thread_acknowledges_cancellation_only_after_cleanup(monkeypatch):
    from app.local_multi.service import adapter_factory

    entered, cleanup = threading.Event(), threading.Event()

    class Learning:
        def watch_video(self, video, *, is_cancelled, **kwargs):
            entered.set()
            wait_for(is_cancelled)
            cleanup.wait(3)
            return False

    adapter = adapter_factory()
    adapter.learning = Learning()
    monkeypatch.setattr(adapter, "get_videos", lambda course_id: [{"id": "v1", "title": "第一节"}])
    result = adapter.start_course("course-1")
    assert entered.wait(2)
    adapter.cancel_task()
    assert not adapter.done.is_set()
    cleanup.set()
    assert adapter.done.wait(2)
    assert adapter.get_task(result["task_id"])["status"] == "cancelled"


def test_original_adapter_empty_course_does_not_leave_queue_waiting(monkeypatch):
    from app.local_multi.service import adapter_factory

    adapter = adapter_factory()
    adapter.learning = object()
    monkeypatch.setattr(adapter, "get_videos", lambda course_id: [])
    result = adapter.start_course("course-1")
    assert result["status"] == "completed"
    assert adapter.done.is_set()
