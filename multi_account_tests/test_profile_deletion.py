"""Regression checks for real Edge long paths and failed profile cleanups."""

import ctypes
import os
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'backend')]

import pytest
from app.local_multi import store as store_module
from app.local_multi.store import Store, _profile_filesystem_path


@pytest.mark.skipif(os.name != 'nt', reason='Windows extended file paths')
def test_real_edge_model_path_over_260_characters_is_removed_only_for_its_account(tmp_path):
    store = Store(tmp_path / 'app' / 'data-multi')
    first, second = store.save('first'), store.save('second')
    first_private = store.account(first['id'], private=True)
    second_private = store.account(second['id'], private=True)
    root = store.browser_directory(first_private['profile'])
    other = store.browser_directory(second_private['profile'])
    other.mkdir(parents=True)
    (other / 'Cookies').write_text('keep-other-session')
    nested = root / 'EdgeOptimizationGuideModelsManifest' / '2026.9.18.5'
    while len(str(nested)) < 235:
        nested /= 'extra-component-dir'
    marker = nested / 'variants' / 'webgpu-fp16-prerelease' / 'configs' / 'writing_assistance_api.binarypb'
    assert len(str(marker)) > 260
    assert root.parent == (store.directory / 'browser-profiles').resolve()
    assert marker.is_relative_to(root)
    extended_marker = Path(_profile_filesystem_path(marker))
    extended_marker.parent.mkdir(parents=True)
    extended_marker.write_bytes(b'edge-model-test-only')
    assert extended_marker.exists()
    store.delete(first['id'])
    assert not os.path.exists(_profile_filesystem_path(root))
    assert (other / 'Cookies').read_text() == 'keep-other-session'
    assert [a['id'] for a in store.accounts()] == [second['id']]


@pytest.mark.skipif(os.name != 'nt', reason='Windows real file sharing locks')
def test_real_locked_edge_file_retains_account_and_can_be_retried(tmp_path):
    store = Store(tmp_path)
    account = store.save('test')
    root = store.browser_directory(store.account(account['id'], private=True)['profile'])
    root.mkdir(parents=True)
    marker = root / 'Cookies'
    marker.write_bytes(b'test-cookie-database')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    from ctypes import wintypes
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(marker), 0x80000000, 0, None, 3, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        with pytest.raises(ValueError, match='被占用'):
            store.delete(account['id'])
        assert len(store.accounts()) == 1
    finally:
        kernel.CloseHandle(handle)
    store.delete(account['id'])
    assert not root.exists() and store.accounts() == []


@pytest.mark.parametrize(('error', 'message'), [
    (PermissionError('private-path'), '写入权限'),
    (FileNotFoundError('private-path'), '账号已保留'),
    (OSError('private-path'), '账号已保留'),
])
def test_non_lock_cleanup_error_does_not_claim_edge_is_open(tmp_path, monkeypatch, caplog, error, message):
    store = Store(tmp_path)
    account = store.save('test')
    root = store.browser_directory(store.account(account['id'], private=True)['profile'])
    root.mkdir(parents=True)
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(store_module.shutil, 'rmtree', fail)
    with pytest.raises(ValueError, match=message):
        store.delete(account['id'])
    assert len(store.accounts()) == 1
    assert 'private-path' not in caplog.text
