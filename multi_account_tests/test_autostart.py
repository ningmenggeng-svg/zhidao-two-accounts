from contextlib import nullcontext
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.local_multi.autostart import Autostart, RUN_KEY, VALUE_NAME
from test_local_multi import create_app


class Registry:
    HKEY_CURRENT_USER='current-user'
    KEY_READ=1
    KEY_SET_VALUE=2
    REG_SZ=1

    def __init__(self):
        self.values={}
        self.locations=[]

    def OpenKey(self,root,path,*args):
        self.locations.append((root,path))
        return nullcontext(self.values)

    CreateKeyEx=OpenKey

    def QueryValueEx(self,key,name):
        if name not in key:raise FileNotFoundError
        return key[name]

    def SetValueEx(self,key,name,reserved,kind,value):
        key[name]=(value,kind)

    def DeleteValue(self,key,name):
        if name not in key:raise FileNotFoundError
        del key[name]


def make_startup(tmp_path):
    executable=tmp_path/'folder with spaces'/'ZhidaoMulti.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'test executable')
    registry=Registry()
    return Autostart(executable=executable,registry=registry,supported=True),registry


def test_autostart_quotes_executable_and_starts_without_browser_for_current_user(tmp_path):
    startup,registry=make_startup(tmp_path)
    assert not startup.status()['enabled']
    assert startup.set_enabled(True)['enabled']
    command,kind=registry.values[VALUE_NAME]
    assert command==f'"{startup.executable}" --no-browser'
    assert all(location==(registry.HKEY_CURRENT_USER,RUN_KEY) for location in registry.locations)
    assert startup.set_enabled(False)['enabled'] is False
    assert VALUE_NAME not in registry.values


def test_unsupported_autostart_never_writes_registry(tmp_path):
    registry=Registry()
    startup=Autostart(registry=registry,supported=False)
    assert startup.status()=={'enabled':False,'supported':False}
    with pytest.raises(ValueError,match='Windows'):
        startup.set_enabled(True)
    assert registry.values=={}


def test_autostart_missing_program_does_not_save_broken_startup_entry(tmp_path):
    registry=Registry()
    startup=Autostart(executable=tmp_path/'missing.exe',registry=registry,supported=True)
    with pytest.raises(ValueError,match='不存在'):
        startup.set_enabled(True)
    assert registry.values=={}


def test_autostart_endpoint_persists_choice_and_state_reports_it(tmp_path):
    startup,registry=make_startup(tmp_path)
    app=create_app(tmp_path/'data',scheduler=False,autostart=startup)
    with TestClient(app,base_url='http://127.0.0.1',headers={'X-Local-Request':'1'}) as client:
        assert not client.get('/api/state').json()['autostart']['enabled']
        assert client.put('/api/autostart',json={'enabled':True}).json()['enabled']
        assert client.get('/api/state').json()['autostart']['enabled']
        assert not client.put('/api/autostart',json={'enabled':False}).json()['enabled']
