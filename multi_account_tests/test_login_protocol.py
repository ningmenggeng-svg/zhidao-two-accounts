from pathlib import Path
import sys
import json
from base64 import b64decode
from urllib.parse import unquote
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.course.zhihuishu.auth import ZhihuishuAuth, encode_login_payload
from app.local_multi.service import friendly_error


def test_password_request_matches_official_secretstr_contract():
    auth = ZhihuishuAuth()
    auth.session = Mock()
    auth.session.cookies = {'CASLOGC': '{"uuid":"test-user"}'}
    auth.session.post.return_value.json.return_value = {'status': 1, 'pwd':'one-time-ticket'}
    auth.password_login('test@example.invalid', '中文 + / ? %')
    body = auth.session.post.call_args.kwargs['data']
    assert set(body) == {'secretStr'}
    decoded = json.loads(unquote(b64decode(body['secretStr']).decode('ascii')))
    assert decoded == {'account':'test@example.invalid', 'password':'中文 + / ? %', 'validate':''}
    assert 'password' not in body


def test_security_and_rate_limit_failures_are_actionable():
    import pytest
    for status, expected in ((-4,'安全验证'),(-9,'安全验证'),(-6,'5 分钟'),(-1,'扫码登录'),(-2,'账号或密码')):
        auth = ZhihuishuAuth()
        auth.session = Mock()
        auth.session.post.return_value.json.return_value = {'status':status, 'msg':'do-not-echo-remote'}
        with pytest.raises(Exception) as error:
            auth.password_login('test-user','test-password')
        assert expected in friendly_error(error.value)
        assert 'do-not-echo-remote' not in str(error.value)


def test_login_encoding_preserves_reserved_characters_and_unicode():
    token = encode_login_payload('账户', "a+b&c=中%文/?#")
    text = b64decode(token).decode('ascii')
    assert '%E8%B4%A6%E6%88%B7' in text
    assert 'a+b&c=' in text
    assert json.loads(unquote(text))['password'] == "a+b&c=中%文/?#"
