from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import json
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'backend'), str(Path(__file__).parent)]
import pytest
from test_local_multi import Manager, Playback, create_app
from app.local_multi.store import Store
from fastapi.testclient import TestClient


def fixture_factory(statuses):
    playback = Playback()
    def factory():
        adapter = playback.factory()
        session = Mock()
        session.cookies = {'owner':'alice','CASLOGC':'{"uuid":"user-alice"}'}
        def get(url, **kwargs):
            if url.endswith('/getLoginQrImg'):
                value = {'img':'aGVsbG8=','qrToken':'private-remote-qr-token'}
            elif url.endswith('/getLoginQrInfo'):
                value = next(statuses)
            else:
                value = {}
            result = Mock()
            result.json.return_value = value
            return result
        session.get.side_effect = get
        adapter.auth = SimpleNamespace(cookies={},session=session)
        return adapter
    return factory


def test_qr_approval_stores_cookies_and_loads_courses_without_password(tmp_path):
    manager = Manager(Store(tmp_path), fixture_factory(iter([{'status':-1},{'status':0},{'status':1,'oncePassword':'private-ticket'}])))
    account = manager.store.save('测试')
    initial = manager.qr.start(account['id'])
    assert 'token' not in initial
    assert manager.qr.poll(initial['id'])['status'] == 'waiting'
    assert '手机' not in manager.qr.poll(initial['id'])['message']
    result = manager.qr.poll(initial['id'])
    assert result['status'] == 'success' and result['course_count'] == 2
    saved = manager.store.account(account['id'], private=True)
    assert saved['credentials']['qr_only']
    assert saved['credentials']['cookies']['owner'] == 'alice'
    assert len(saved['courses']) == 2
    public = json.dumps(manager.snapshot())
    assert 'private-ticket' not in public and 'private-remote-qr-token' not in public and 'cookies' not in public


def test_qr_cannot_rebind_or_start_queue_during_active_scan(tmp_path):
    manager = Manager(Store(tmp_path), fixture_factory(iter([])))
    account = manager.store.save('测试')
    initial = manager.qr.start(account['id'])
    with pytest.raises(ValueError, match='扫码'):
        manager.start()
    with pytest.raises(ValueError, match='扫码'):
        manager.qr.start(account['id'])
    manager.qr.cancel(initial['id'])
    assert not manager.qr.active
    assert manager.qr.poll(initial['id'])['status'] == 'cancelled'


@pytest.mark.parametrize('status', [2,3])
def test_expired_or_cancelled_remote_qr_never_saves_credentials(tmp_path,status):
    manager = Manager(Store(tmp_path), fixture_factory(iter([{'status':status}])))
    account = manager.store.save('测试')
    initial = manager.qr.start(account['id'])
    result = manager.qr.poll(initial['id'])
    assert result['status'] in {'expired','cancelled'}
    assert not manager.qr.active
    assert 'cookies' not in manager.store.account(account['id'],private=True)['credentials']


def test_qr_local_timeout_and_stale_ids_are_rejected(tmp_path):
    manager = Manager(Store(tmp_path), fixture_factory(iter([])))
    account = manager.store.save('测试')
    initial = manager.qr.start(account['id'])
    with pytest.raises(ValueError):
        manager.qr.poll('wrong-id')
    manager.qr.job['created'] = time.monotonic()-121
    assert manager.qr.poll(initial['id'])['status'] == 'expired'
    assert not manager.qr.active


def test_qr_endpoints_keep_remote_token_private(tmp_path):
    factory = fixture_factory(iter([{'status':-1}]))
    app=create_app(tmp_path, factory=factory, scheduler=False)
    with TestClient(app,base_url='http://127.0.0.1',headers={'X-Local-Request':'1'}) as client:
        account=client.post('/api/accounts',json={'label':'测试'}).json()
        start=client.post(f"/api/accounts/{account['id']}/qr").json()
        result=client.get(f"/api/qr/{start['id']}")
        assert result.status_code==200 and result.json()['status']=='waiting'
        assert 'private-remote-qr-token' not in result.text
        assert client.delete(f"/api/qr/{start['id']}").status_code==200
