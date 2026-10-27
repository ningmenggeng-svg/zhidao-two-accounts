"""Official password login in an app-owned, persistent Edge profile.

The user enters credentials, submits the login and completes verification.
The application retains only the resulting official session.
"""

import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from app.services.course.zhihuishu.auth import LOGIN_SERVICE

LOGIN_PAGE = "https://login.zhihuishu.com/?" + urlencode(
    {"origin": "zhs", "source": "custom", "service": LOGIN_SERVICE}
)
logger = logging.getLogger(__name__)


def browser_error_code(exc):
    text = str(exc).lower()
    if isinstance(exc, FileNotFoundError) or "executable doesn't exist" in text:
        return "browser_missing"
    if any(word in text for word in ("already in use", "processsingleton", "singletonlock", "profile in use")):
        return "profile_busy"
    if isinstance(exc, TimeoutError) or "timeout" in text:
        return "timeout"
    if any(word in text for word in ("targetclosed", "has been closed", "target closed")):
        return "window_closed"
    if any(word in text for word in ("net::", "connection", "resolve", "ssl")):
        return "network"
    return "connection_failed"


def record_browser_error(phase, exc):
    # Never log exception bodies: automation errors can contain entered values.
    code = browser_error_code(exc)
    logger.warning("Official login diagnostic: phase=%s code=%s type=%s", phase, code, type(exc).__name__)
    return code


def live_page(context, previous=None):
    pages = [page for page in context.pages if not page.is_closed()]
    if previous in pages:
        return previous
    official = [page for page in pages if (urlsplit(page.url).hostname or "").endswith(".zhihuishu.com")]
    return official[0] if official else pages[0] if pages else None


@contextmanager
def edge_profile(directory):
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as runtime:
        context = runtime.chromium.launch_persistent_context(
            user_data_dir=str(directory),
            channel="msedge",
            headless=False,
            no_viewport=True,
            accept_downloads=False,
            locale="zh-CN",
            args=["--no-first-run", "--no-default-browser-check", "--start-maximized"],
        )
        try:
            yield context
        finally:
            context.close()


def show_login_window(context, page):
    # Operate only on this app's page, never on unrelated Edge windows.
    # Restore first: a minimized window cannot be maximized directly.
    session = context.new_cdp_session(page)
    try:
        window = session.send("Browser.getWindowForTarget")
        session.send(
            "Browser.setWindowBounds",
            {"windowId": window["windowId"], "bounds": {"windowState": "normal", "left": 80, "top": 80}},
        )
        session.send(
            "Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "maximized"}}
        )
        page.bring_to_front()
    finally:
        session.detach()


def official_cookies(cookies):
    result = {}
    for cookie in cookies:
        domain = cookie.get("domain", "").lstrip(".")
        if domain != "zhihuishu.com" and not domain.endswith(".zhihuishu.com"):
            continue
        if cookie["name"] == "SESSION" and domain != "onlineservice-api.zhihuishu.com":
            continue
        result[cookie["name"]] = cookie["value"]
    return result


class PasswordLogin:
    def __init__(
        self, manager, normalize_courses, friendly_error, *, context_factory=edge_profile, poll_seconds=1, timeout=None
    ):
        self.manager, self.normalize, self.error = manager, normalize_courses, friendly_error
        self.context_factory, self.poll_seconds, self.timeout = context_factory, poll_seconds, timeout
        self.active = False
        self.job = None
        self.thread = None

    @staticmethod
    def public(job):
        keys = (
            "id", "account_id", "status", "message", "course_count", "window_opened", "phase", "error_code",
            "window_show_result",
        )
        return {key: job[key] for key in keys if key in job}

    def start(self, account_id):
        with self.manager.lock:
            self.manager.assert_idle()
            account = self.manager.store.account(account_id, private=True)
            job = {
                "id": uuid4().hex,
                "account_id": account_id,
                "status": "opening",
                "message": "正在打开知到官方账号密码登录窗口。",
                "stop": threading.Event(),
                "check": threading.Event(),
                "show": threading.Event(),
                "window_opened": False,
                "phase": "launching",
            }
            self.job, self.active = job, True
            self.thread = threading.Thread(target=self._run, args=(job, account), daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.active = False
                raise ValueError("无法打开登录窗口，请重试。") from None
            return self.public(job)

    def _update(self, job, **values):
        with self.manager.lock:
            if self.job is job and not job["stop"].is_set():
                job.update(values)

    def _run(self, job, account):
        stage = "opening"
        try:
            profile = self.manager.store.browser_directory(account["profile"])
            with self.context_factory(profile) as context:
                page = live_page(context) or context.new_page()
                self._show_window(job, context, page)
                stage = "loading"
                self._update(
                    job,
                    status="loading",
                    phase="loading",
                    window_opened=True,
                    message="已连接官方登录窗口，正在加载网页。找不到窗口时可点击「显示登录窗口」。",
                )
                try:
                    page.goto(LOGIN_PAGE, wait_until="domcontentloaded", timeout=30000)
                    navigation_message = "请在官方窗口选择「密码登录」，手动输入账号密码并登录；验证码由你完成。"
                except Exception as exc:
                    code = record_browser_error(stage, exc)
                    page = live_page(context, page)
                    if page is None:
                        raise
                    self._update(job, error_code=code)
                    navigation_message = (
                        "窗口已连接，但网页尚未加载完成。找不到窗口时点击「显示登录窗口」，程序会继续检查。"
                    )
                stage = "waiting"
                self._update(
                    job,
                    status="waiting",
                    phase="waiting",
                    message=navigation_message,
                )
                deadline = time.monotonic() + self.timeout if self.timeout is not None else None
                last_check, last_token = 0, None
                while not job["stop"].is_set() and (deadline is None or time.monotonic() < deadline):
                    page = live_page(context, page)
                    if page is None:
                        self._update(job, status="cancelled", message="登录窗口已关闭，可以重新点击账号密码登录。")
                        return
                    if job["show"].is_set():
                        job["show"].clear()
                        self._show_window(job, context, page, requested=True)
                    cookies = official_cookies(context.cookies())
                    identity = cookies.get("CASLOGC")
                    signature = hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).hexdigest()
                    force_check = job["check"].is_set()
                    job["check"].clear()
                    if identity and (force_check or signature != last_token or time.monotonic() - last_check > 10):
                        last_token, last_check = signature, time.monotonic()
                        adapter = self.manager.factory()
                        try:
                            adapter.auth.cookies = cookies
                            adapter._init_services(adapter.auth.cookies)
                            self._update(job, phase="courses", message="已读取到登录状态，正在获取课程。")
                            courses = self.normalize(adapter.get_courses())
                            with self.manager.lock:
                                if self.job is job and not job["stop"].is_set():
                                    self.manager.store.save_cookies(
                                        account["id"], adapter.auth.cookies, browser_only=True
                                    )
                                    updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
                                    self.manager.store.save_courses(account["id"], courses, updated)
                                    job.update(
                                        status="success",
                                        course_count=len(courses),
                                        message=f"账号密码登录成功，已获取 {len(courses)} 门课程。",
                                    )
                            return
                        except Exception as exc:
                            record_browser_error("courses", exc)
                            self._update(
                                job,
                                phase="courses",
                                error_code="course_fetch_failed",
                                message="窗口已连接，但课程读取尚未成功。请确认官网已完成登录，再点「检查登录状态」。",
                            )
                        finally:
                            self.manager._close_adapter(adapter)
                    # Pump browser events while keeping all browser access on this worker.
                    try:
                        page.wait_for_timeout(int(self.poll_seconds * 1000))
                    except Exception:
                        if live_page(context, page) is None:
                            self._update(job, status="cancelled", message="登录窗口已关闭，可以重新点击账号密码登录。")
                            return
                self._update(job, status="expired", message="登录等待超时，请重新打开账号密码登录。")
        except Exception as exc:
            if job.get("status") != "success":
                code = record_browser_error(stage, exc)
                if code == "browser_missing":
                    message = "未找到 Microsoft Edge，请确认电脑已安装 Edge。"
                elif code == "profile_busy":
                    message = "该账号的旧登录窗口仍在使用中，请先关闭旧登录窗口，再重新点击账号密码登录。"
                elif job.get("window_opened"):
                    message = "登录窗口已打开过，但程序与它的连接中断。请关闭该登录窗口后重新登录。"
                else:
                    message = "程序未能连接到专用登录窗口。如果已有登录窗口，请先关闭它，再重试。"
                self._update(job, status="failed", phase=stage, error_code=code, message=message)
        finally:
            with self.manager.lock:
                if self.job is job:
                    self.active = False

    def poll(self, job_id):
        with self.manager.lock:
            if not self.job or self.job["id"] != job_id:
                raise ValueError("登录窗口已更新，请使用当前窗口。")
            return self.public(self.job)

    def _show_window(self, job, context, page, *, requested=False):
        try:
            show_login_window(context, page)
        except Exception as exc:
            record_browser_error("show_window", exc)
            values = {"window_show_result": "failed"}
            if requested:
                values["message"] = "显示窗口的请求未成功。请关闭本次登录对话框后重新打开账号密码登录。"
        else:
            values = {"window_show_result": "sent"}
            if requested:
                values["message"] = "已向官方窗口发送显示请求。请在 Edge 窗口选择「密码登录」并完成登录。"
        self._update(job, **values)

    def show(self, job_id):
        with self.manager.lock:
            if not self.job or self.job["id"] != job_id or not self.active:
                raise ValueError("这次登录已结束，请重新点击账号密码登录。")
            self.job["show"].set()
            self.job["window_show_result"] = "requested"
            return {"ok": True}

    def cancel(self, job_id):
        with self.manager.lock:
            if self.job and self.job["id"] == job_id:
                self.job["stop"].set()
                self.job.update(status="cancelled", message="账号密码登录已取消。")
            return {"ok": True}

    def check(self, job_id):
        with self.manager.lock:
            if not self.job or self.job["id"] != job_id or not self.active:
                raise ValueError("这次登录已结束，请重新点击账号密码登录。")
            self.job["check"].set()
            return {"ok": True}
