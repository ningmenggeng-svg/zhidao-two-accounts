"""智慧树二维码登录模块"""

import json
import threading
from base64 import b64decode, b64encode
from collections.abc import Callable
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter, Retry

from .errors import PlatformResponseError

LOGIN_URL = "https://passport.zhihuishu.com/login"
LOGIN_SERVICE = (
    "https://onlineservice-api.zhihuishu.com/gateway/f/v1/login/gologin"
    "?fromurl=https%3A%2F%2Fonlineweb.zhihuishu.com%2F"
)
# The login callback above leaves a host-only ``SESSION`` cookie for
# onlineservice-api. Login cookies are kept as a flat dict, so without a domain
# that cookie would also go to studyservice-api, which then keeps the foreign
# session instead of issuing its own on gologin and answers every video request
# with 401. Scope such cookies back to the host that set them.
HOST_ONLY_COOKIES = {"SESSION": "onlineservice-api.zhihuishu.com"}


def encode_login_payload(username: str, password: str) -> str:
    """Match the official client's btoa(encodeURI(JSON.stringify(payload)))."""
    payload = json.dumps(
        {"account": username, "password": password, "validate": ""},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    encoded = quote(payload, safe=";/?:@&=+$,#-_.!~*'()")
    return b64encode(encoded.encode("ascii")).decode("ascii")


def build_cookie_jar(cookies: dict | None) -> requests.cookies.RequestsCookieJar:
    """Cookie jar for API sessions with host-only cookies scoped to their host."""
    jar = requests.cookies.RequestsCookieJar()
    for name, value in (cookies or {}).items():
        jar.set(name, value, domain=HOST_ONLY_COOKIES.get(name, ""))
    return jar


def official_cookie_snapshot(base: dict, cookie_jar) -> dict:
    """Keep updated official cookies, without mixing the two service SESSIONs."""
    result = dict(base)
    for cookie in cookie_jar:
        domain = cookie.domain.lstrip(".")
        if domain != "zhihuishu.com" and not domain.endswith(".zhihuishu.com"):
            continue
        if cookie.name == "SESSION" and domain != HOST_ONLY_COOKIES["SESSION"]:
            continue
        result[cookie.name] = cookie.value
    return result


class ZhihuishuAuth:
    """智慧树认证服务"""

    def __init__(self, proxies: dict | None = None):
        self.proxies = proxies or {}
        self.session = requests.Session()
        retry = Retry(
            total=5, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504]
        )
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.uuid = None
        self._cookies = None

    @property
    def cookies(self):
        return self._cookies

    @cookies.setter
    def cookies(self, cookies):
        normalized = self._normalize_cookies(cookies)
        uuid = None
        if normalized:
            try:
                uuid = json.loads(unquote(normalized.get("CASLOGC", "{}")))["uuid"]
                if not isinstance(uuid, str) or not uuid.strip():
                    raise ValueError("uuid missing")
                normalized[f"exitRecod_{uuid}"] = "2"
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError("Cookies invalid") from exc
        # Commit identity and cookies together, only after validation succeeds.
        self._cookies = normalized
        self.uuid = uuid

    @staticmethod
    def _normalize_cookies(cookies) -> dict:
        """Normalize cookies to a plain dict and avoid conflict-prone get behavior."""
        if not cookies:
            return {}

        normalized = {}
        if isinstance(cookies, dict):
            for name, value in cookies.items():
                if value is None:
                    continue
                normalized[str(name)] = str(value)
            return normalized

        for cookie in cookies:
            normalized[cookie.name] = cookie.value
        return normalized

    def capture_session_cookies(self, cookie_jar):
        updated = official_cookie_snapshot(self.cookies or {}, cookie_jar)
        try:
            identity = json.loads(unquote(updated.get("CASLOGC", "{}"))).get("uuid")
        except (ValueError, TypeError):
            identity = None
        if not identity or identity != self.uuid:
            raise ValueError("login_identity_changed")
        self.cookies = updated
        return dict(self.cookies)

    def refresh_session(self, *, is_cancelled=None):
        """Use an existing CAS ticket via normal official redirects; never submit credentials."""
        original = dict(self.cookies or {})
        if not original.get("CASTGC") or not self.uuid:
            raise ValueError("login_sso_expired")
        with requests.Session() as session:
            for name, value in original.items():
                if name != "SESSION":
                    session.cookies.set(
                        name, value, domain=".zhihuishu.com", secure=True
                    )
            url = LOGIN_URL + "?" + urlencode({"service": LOGIN_SERVICE})
            for _ in range(8):
                if callable(is_cancelled) and is_cancelled():
                    raise InterruptedError("session_refresh_cancelled")
                parsed = urlsplit(url)
                host = parsed.hostname or ""
                if parsed.scheme != "https" or (
                    host != "zhihuishu.com" and not host.endswith(".zhihuishu.com")
                ):
                    raise ValueError("login_redirect_rejected")
                if parsed.username or parsed.password or parsed.port not in (None, 443):
                    raise ValueError("login_redirect_rejected")
                response = session.get(
                    url, proxies=self.proxies, timeout=10, allow_redirects=False
                )
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("login_sso_expired")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise PlatformResponseError(
                        "http_status", http_status=response.status_code
                    )
                break
            else:
                raise ValueError("login_sso_expired")
            fresh = official_cookie_snapshot(
                {key: value for key, value in original.items() if key != "SESSION"},
                session.cookies,
            )
            if not fresh.get("SESSION"):
                raise ValueError("login_sso_expired")
            # Check the account before committing any refreshed authentication data.
            identity = json.loads(unquote(fresh.get("CASLOGC", "{}"))).get("uuid")
            if identity != self.uuid:
                raise ValueError("login_identity_changed")
            self.cookies = fresh
            return dict(self.cookies)

    def qr_login(
        self,
        qr_callback: Callable[[bytes], None],
        _retries: int = 3,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        """Log in with a refreshable QR code and cooperative cancellation."""
        cancel_event = cancel_event or threading.Event()
        login_page = LOGIN_URL
        qr_page = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrImg"
        query_page = "https://passport.zhihuishu.com/qrCodeLogin/getLoginQrInfo"

        try:
            for attempt in range(_retries):
                if cancel_event.is_set():
                    raise InterruptedError("QR login cancelled")

                r = self.session.get(qr_page, proxies=self.proxies, timeout=10).json()
                qr_token = r["qrToken"]
                qr_callback(b64decode(r["img"]))

                while not cancel_event.wait(0.5):
                    msg = self.session.get(
                        query_page,
                        params={"qrToken": qr_token},
                        proxies=self.proxies,
                        timeout=10,
                    ).json()
                    status = msg.get("status")
                    if status in {-1, 0}:
                        continue
                    if status == 1:
                        if cancel_event.is_set():
                            raise InterruptedError("QR login cancelled")
                        self.session.get(
                            login_page,
                            params={
                                "pwd": msg["oncePassword"],
                                "service": LOGIN_SERVICE,
                            },
                            proxies=self.proxies,
                            timeout=10,
                        )
                        self.cookies = self.session.cookies
                        if not self.cookies:
                            raise Exception("No cookies found")
                        return self.cookies
                    if status == 2:
                        break
                    if status == 3:
                        raise Exception("Login canceled")
                    raise Exception(f"Unknown status: {status}")
                else:
                    raise InterruptedError("QR login cancelled")

                if attempt == _retries - 1:
                    raise TimeoutError("QR login retries exhausted")
        except InterruptedError:
            raise
        except Exception as e:
            raise Exception(f"QR login failed: {e}") from e

    def password_login(self, username: str, password: str) -> dict:
        """
        账号密码登录

        Args:
            username: 用户名
            password: 密码

        Returns:
            登录后的 cookies 字典
        """
        login_page = LOGIN_URL
        valid_url = "https://passport.zhihuishu.com/user/validateAccountAndPassword"

        try:
            self.session.get(
                login_page,
                params={"service": LOGIN_SERVICE},
                proxies=self.proxies,
                timeout=10,
            )
            form = {"secretStr": encode_login_payload(username, password)}
            user_info = self.session.post(
                valid_url, data=form, proxies=self.proxies, timeout=10
            ).json()

            if user_info.get("status") == 1:
                params = {
                    "account": username,
                    "pwd": user_info["pwd"],
                    "validate": 0,
                    "service": LOGIN_SERVICE,
                }
                self.session.get(
                    login_page, params=params, proxies=self.proxies, timeout=10
                )
                self.cookies = self.session.cookies
                if not self.cookies:
                    raise ValueError("No cookies found after login")
                return self.cookies
            if user_info.get("status") == -2:
                raise ValueError("Username or password invalid")
            if user_info.get("status") in {-6, -11}:
                raise ValueError("login_rate_limited")
            if user_info.get("status") in {-4, -9, -10, -12, -13}:
                raise ValueError("login_security_check")
            if user_info.get("status") == -5:
                raise ValueError("login_password_update_required")
            if user_info.get("status") == -1:
                raise ValueError("login_parameters_rejected")
            raise ValueError("login_rejected")

        except Exception as e:
            raise Exception(f"Password login failed: {e}") from e
