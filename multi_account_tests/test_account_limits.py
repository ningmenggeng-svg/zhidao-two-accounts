import threading

import pytest
from fastapi.testclient import TestClient

from test_local_multi import Manager, Playback, Store, create_app

HEADERS = {"X-Local-Request": "1"}


def test_fresh_install_opens_directly_and_replaces_deleted_account(tmp_path):
    app = create_app(tmp_path, factory=Playback().factory, scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1", headers=HEADERS) as client:
        state = client.get('/api/state').json()
        assert state['accounts'] == []
        assert state['limits'] == {'accounts': 2, 'parallel_accounts': 2}
        assert 'license' not in state
        assert 'activation-screen' not in client.get('/').text
        first = client.post('/api/accounts', json={'label': 'one'}).json()
        assert client.post('/api/accounts', json={'label': 'two'}).status_code == 200
        assert client.post('/api/accounts', json={'label': 'three'}).status_code == 400
        assert client.put('/api/accounts/'+first['id'], json={'label': 'edited'}).status_code == 200
        assert client.delete('/api/accounts/'+first['id']).status_code == 200
        assert client.post('/api/accounts', json={'label': 'replacement'}).status_code == 200
        assert len(client.get('/api/state').json()['accounts']) == 2
        assert client.post('/api/invite/activate', json={'code': 'old'}).status_code == 404
        assert client.post('/api/invite/issue', json={'device': 'old'}).status_code == 404
    assert not list(tmp_path.rglob('*activation*'))
    assert not list(tmp_path.rglob('*invite*'))


def test_concurrent_store_instances_cannot_exceed_two(tmp_path):
    stores = [Store(tmp_path) for _ in range(4)]
    barrier = threading.Barrier(4)
    outcomes = []

    def save(store):
        barrier.wait()
        try:
            store.save('account')
            outcomes.append(True)
        except ValueError:
            outcomes.append(False)

    threads = [threading.Thread(target=save, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert outcomes.count(True) == 2
    assert len(stores[0].accounts()) == 2


def test_parallel_settings_and_legacy_values_are_limited_to_two(tmp_path):
    store = Store(tmp_path)
    store.put('settings', {'parallel_accounts': 5})
    assert store.settings()['parallel_accounts'] == 2
    manager = Manager(store, Playback().factory)
    with pytest.raises(ValueError, match='1 到 2'):
        manager.update_settings({'parallel_accounts': 3})
    with pytest.raises(ValueError, match='1 到 2'):
        manager.update_settings({'parallel_accounts': 0})
    app = create_app(tmp_path, factory=Playback().factory, scheduler=False)
    with TestClient(app, base_url='http://127.0.0.1', headers=HEADERS) as client:
        assert client.put('/api/settings', json={'parallel_accounts': 3}).status_code == 422
        assert client.put('/api/settings', json={'parallel_accounts': 2}).status_code == 200
