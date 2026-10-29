"""Classify wrapped request failures without exposing remote exception text."""

from dataclasses import dataclass

import requests

from app.services.course.zhihuishu.errors import PlatformResponseError


@dataclass(frozen=True)
class Failure:
    kind: str
    retryable: bool = False
    exception_type: str = "Exception"
    http_status: int | None = None
    platform_code: int | None = None


MESSAGES = {
    "network_timeout": "连接知到超时，请检查网络后重试。",
    "network_connection": "连接知到失败，请检查网络后重试。",
    "network_proxy": "代理连接失败，请检查代理或网络后重试。",
    "network_tls": "安全连接校验失败，请检查网络和系统时间后重试。",
    "platform_unavailable": "知到服务暂时不可用，请稍后重试。",
    "rate_limited": "知到暂时限制了请求频率，请稍后再试，避免反复点击。",
    "access_denied": "知到拒绝访问或需要安全验证，请到官方页面确认后重试。",
    "invalid_response": "知到返回的数据异常，请稍后重试。",
    "platform_rejected": "知到未接受课程请求，请稍后重试。",
    "unknown": "读取课程失败，暂不能确认原因，请稍后重试或查看本机日志。",
}


class CourseReadError(ValueError):
    def __init__(self, failure, attempts, *, qr_only=False):
        self.failure, self.attempts, self.qr_only = failure, attempts, qr_only
        super().__init__(failure.kind)

    def user_message(self):
        if self.failure.kind == "session_invalid":
            return (
                "扫码登录状态已过期，请点击「扫码登录」重新确认。"
                if self.qr_only
                else "官方网页登录状态已过期，请点击「账号密码登录」重新确认。"
            )
        message = MESSAGES[self.failure.kind]
        return f"获取课程已自动重试 {self.attempts - 1} 次。{message}" if self.attempts > 1 else message


def classify_failure(exc):
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    for error in chain:
        name = type(error).__name__
        if isinstance(error, PlatformResponseError):
            status, code = error.http_status, error.code
            if status == 401 or code == 401:
                kind, retryable = "session_invalid", False
            elif status == 429 or code == 429:
                kind, retryable = "rate_limited", False
            elif status == 403 or code == 403:
                kind, retryable = "access_denied", False
            elif status in {408, 500, 502, 503, 504} or code in {408, 500, 502, 503, 504}:
                kind, retryable = "platform_unavailable", True
            elif error.reason in {"invalid_json", "invalid_response", "missing_data", "missing_list"}:
                kind, retryable = "invalid_response", True
            else:
                kind, retryable = "platform_rejected", False
            return Failure(kind, retryable, name, status, code)
        if isinstance(error, requests.exceptions.SSLError):
            return Failure("network_tls", False, name)
        if isinstance(error, requests.exceptions.ProxyError):
            return Failure("network_proxy", True, name)
        if isinstance(error, requests.exceptions.Timeout):
            return Failure("network_timeout", True, name)
        if isinstance(error, requests.exceptions.ConnectionError):
            return Failure("network_connection", True, name)
        if isinstance(error, requests.exceptions.JSONDecodeError):
            return Failure("invalid_response", True, name)
        if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
            return classify_failure(PlatformResponseError("http_status", http_status=error.response.status_code))
    # These are explicit local authentication failures, not generic upstream errors.
    if any(
        str(error)
        .lower()
        .startswith(
            ("cookies invalid", "not logged in", "expired session", "login_browser_expired", "login_qr_expired")
        )
        for error in chain
    ):
        return Failure("session_invalid", exception_type=type(chain[-1]).__name__)
    return Failure("unknown", exception_type=type(chain[-1]).__name__)
