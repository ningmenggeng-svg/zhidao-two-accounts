from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import time
import threading

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'backend'),str(Path(__file__).parent)]
import pytest
from test_local_multi import Manager, Playback, wait_for
from app.local_multi.password_browser import PasswordLogin, official_cookies, show_login_window
from app.local_multi.store import Store


def make_login(tmp_path, *, signed_in=True):
    manager=Manager(Store(tmp_path),Playback().factory)
    account=manager.store.save('测试')
    page=Mock()
    page.url='https://login.zhihuishu.com/'
    page.is_closed.return_value=False
    page.locator.return_value.first.is_visible.return_value=False
    page.wait_for_timeout.side_effect=lambda ms:time.sleep(.005)
    context=Mock()
    context.new_cdp_session.return_value.send.return_value={'windowId':7}
    context.pages=[page]
    context.cookies.return_value=([
        {'name':'CASLOGC','value':'{"uuid":"alice-uuid"}','domain':'.zhihuishu.com'},
        {'name':'owner','value':'alice','domain':'.zhihuishu.com'},
        {'name':'external','value':'not-for-app','domain':'.other.invalid'},
    ] if signed_in else [])
    profiles=[]
    @contextmanager
    def factory(directory):
        profiles.append(directory)
        yield context
        context.close()
    login=PasswordLogin(manager,manager.password_login.normalize,manager.password_login.error,
                        context_factory=factory,poll_seconds=.005,timeout=.05 if not signed_in else 2)
    manager.password_login=login
    return manager,account,context,page,profiles


def test_approved_password_login_saves_session_and_courses(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path)
    job=manager.password_login.start(account['id'])
    wait_for(lambda:not manager.password_login.active)
    result=manager.password_login.poll(job['id'])
    assert result['status']=='success' and result['course_count']==2
    saved=manager.store.account(account['id'],private=True)
    assert saved['credentials']['browser_only']
    assert 'password' not in saved['credentials']
    assert 'external' not in saved['credentials']['cookies']
    assert len(saved['courses'])==2
    assert profiles[0].is_relative_to(tmp_path/'browser-profiles')
    assert 'alice' not in profiles[0].name
    assert 'local-password' not in str(result)
    page.goto.assert_called_once()
    context.close.assert_called_once()


def test_no_cookie_never_counts_as_login_success(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    job=manager.password_login.start(account['id'])
    wait_for(lambda:not manager.password_login.active)
    assert manager.password_login.poll(job['id'])['status']=='expired'
    assert 'cookies' not in manager.store.account(account['id'],private=True)['credentials']


def test_default_login_survives_five_minutes_until_user_cancels(tmp_path, monkeypatch):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    clock=[0.0]
    monkeypatch.setattr('app.local_multi.password_browser.time',SimpleNamespace(monotonic=lambda:clock[0]))
    previous=manager.password_login
    login=PasswordLogin(manager,previous.normalize,previous.error,
                        context_factory=previous.context_factory,poll_seconds=.005)
    manager.password_login=login
    def advance_clock(ms):
        clock[0]+=301
        time.sleep(.005)
    page.wait_for_timeout.side_effect=advance_clock
    job=login.start(account['id'])
    try:
        wait_for(lambda:page.wait_for_timeout.call_count>=2 or not login.active)
        assert clock[0]>300
        assert login.active
        assert login.poll(job['id'])['status']=='waiting'
        context.close.assert_not_called()
        assert 'cookies' not in manager.store.account(account['id'],private=True)['credentials']
    finally:
        login.cancel(job['id'])
        wait_for(lambda:not login.active)
    assert login.poll(job['id'])['status']=='cancelled'
    context.close.assert_called_once()


def test_cancelled_password_login_does_not_capture_later_session(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    manager.password_login.timeout=2
    job=manager.password_login.start(account['id'])
    manager.password_login.cancel(job['id'])
    wait_for(lambda:not manager.password_login.active)
    assert manager.password_login.poll(job['id'])['status']=='cancelled'
    assert 'cookies' not in manager.store.account(account['id'],private=True)['credentials']


def test_active_password_login_blocks_queue_or_other_login(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    manager.password_login.timeout=2
    job=manager.password_login.start(account['id'])
    try:
        with pytest.raises(ValueError):manager.start()
        with pytest.raises(ValueError):manager.qr.start(account['id'])
        with pytest.raises(ValueError):manager.password_login.start(account['id'])
    finally:
        manager.password_login.cancel(job['id'])
        wait_for(lambda:not manager.password_login.active)


def test_manual_login_never_reads_fills_or_submits_credential_fields(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    login=manager.password_login
    login.timeout=2
    job=login.start(account['id'])
    try:
        wait_for(lambda:login.job['status']=='waiting')
        page.locator.assert_not_called()
        page.get_by_role.assert_not_called()
        page.fill.assert_not_called()
        page.click.assert_not_called()
        assert manager.store.account(account['id'],private=True)['credentials']=={}
    finally:
        login.cancel(job['id'])
        wait_for(lambda:not login.active)


def test_remark_edit_keeps_same_browser_profile_for_next_login(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path)
    manager.password_login.start(account['id'])
    wait_for(lambda:not manager.password_login.active)
    manager.store.save('新备注',account['id'])
    manager.password_login.start(account['id'])
    wait_for(lambda:not manager.password_login.active)
    assert len(profiles)==2 and profiles[0]==profiles[1]
    assert manager.store.account(account['id'])['label']=='新备注'


def test_cookie_scope_does_not_mix_service_sessions():
    cookies=official_cookies([
        {'name':'SESSION','value':'course','domain':'studyservice-api.zhihuishu.com'},
        {'name':'SESSION','value':'online','domain':'onlineservice-api.zhihuishu.com'},
        {'name':'auth','value':'foreign','domain':'evilzhihuishu.com'},
    ])
    assert cookies=={'SESSION':'online'}


def test_navigation_timeout_is_not_reported_as_missing_browser(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path)
    page.goto.side_effect=TimeoutError('navigation timeout')
    job=manager.password_login.start(account['id'])
    wait_for(lambda:not manager.password_login.active)
    result=manager.password_login.poll(job['id'])
    assert result['status']=='success'
    assert result['window_opened'] is True
    assert result['error_code']=='timeout'


def test_loading_error_keeps_open_window_waiting_for_user(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    manager.password_login.timeout=2
    page.goto.side_effect=RuntimeError('net::ERR_CONNECTION_RESET')
    job=manager.password_login.start(account['id'])
    try:
        wait_for(lambda:manager.password_login.job['status']=='waiting')
        result=manager.password_login.poll(job['id'])
        assert result['window_opened']
        assert '窗口已连接' in result['message']
        assert manager.password_login.active
        context.close.assert_not_called()
        manager.password_login.check(job['id'])
        assert manager.password_login.job['check'].is_set()
    finally:
        manager.password_login.cancel(job['id'])
        wait_for(lambda:not manager.password_login.active)


def test_profile_in_use_is_not_reported_as_missing_edge(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path)
    @contextmanager
    def busy(directory):
        raise RuntimeError('user data directory is already in use')
        yield
    manager.password_login.context_factory=busy
    job=manager.password_login.start(account['id'])
    wait_for(lambda:not manager.password_login.active)
    result=manager.password_login.poll(job['id'])
    assert result['error_code']=='profile_busy'
    assert '旧登录窗口' in result['message']
    assert '未安装' not in result['message']


def test_context_close_failure_cannot_overwrite_success(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path)
    @contextmanager
    def closing(directory):
        yield context
        raise RuntimeError('Target page, context or browser has been closed')
    manager.password_login.context_factory=closing
    job=manager.password_login.start(account['id'])
    wait_for(lambda:not manager.password_login.active)
    assert manager.password_login.poll(job['id'])['status']=='success'


def test_live_page_follows_new_official_tab():
    from app.local_multi.password_browser import live_page
    previous=Mock(); previous.is_closed.return_value=True
    current=Mock(); current.is_closed.return_value=False; current.url='https://onlineweb.zhihuishu.com/'
    assert live_page(SimpleNamespace(pages=[previous,current]),previous) is current


def test_show_window_restores_and_targets_only_the_owned_page():
    context,page=Mock(),Mock()
    session=context.new_cdp_session.return_value
    session.send.return_value={'windowId':17}
    show_login_window(context,page)
    context.new_cdp_session.assert_called_once_with(page)
    assert session.send.call_args_list[1].args==(
        'Browser.setWindowBounds',{'windowId':17,'bounds':{'windowState':'normal','left':80,'top':80}})
    assert session.send.call_args_list[2].args==(
        'Browser.setWindowBounds',{'windowId':17,'bounds':{'windowState':'maximized'}})
    page.bring_to_front.assert_called_once()
    session.detach.assert_called_once()
    page.goto.assert_not_called()
    page.locator.assert_not_called()


def test_show_request_runs_on_login_worker_without_counting_as_success(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    login=manager.password_login
    login.timeout=2
    thread_ids=[]
    page.bring_to_front.side_effect=lambda:thread_ids.append(threading.get_ident())
    job=login.start(account['id'])
    try:
        wait_for(lambda:login.job['status']=='waiting')
        with pytest.raises(ValueError):login.show('previous-login')
        login.show(job['id'])
        wait_for(lambda:len(thread_ids)==2)
        result=login.poll(job['id'])
        assert result['status']=='waiting' and result['window_show_result']=='sent'
        assert set(thread_ids)=={login.thread.ident}
        assert login.thread.ident!=threading.get_ident()
        assert 'cookies' not in manager.store.account(account['id'],private=True)['credentials']
    finally:
        login.cancel(job['id'])
        wait_for(lambda:not login.active)
    with pytest.raises(ValueError):login.show(job['id'])


def test_show_failure_keeps_login_open_and_reports_failure(tmp_path):
    manager,account,context,page,profiles=make_login(tmp_path,signed_in=False)
    login=manager.password_login
    login.timeout=2
    context.new_cdp_session.side_effect=RuntimeError('window unavailable')
    job=login.start(account['id'])
    try:
        wait_for(lambda:login.job['status']=='waiting')
        login.show(job['id'])
        wait_for(lambda:login.job.get('window_show_result')=='failed')
        result=login.poll(job['id'])
        assert result['status']=='waiting' and login.active
        assert '未成功' in result['message']
        context.close.assert_not_called()
    finally:
        login.cancel(job['id'])
        wait_for(lambda:not login.active)
