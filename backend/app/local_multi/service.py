"""One queue, isolated logins, course selection, and a persistent daily schedule."""

from __future__ import annotations

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .password_browser import PasswordLogin
from .qr import QRLogin
from .store import ACCOUNT_LIMIT, PARALLEL_LIMIT, Store

SHANGHAI = timezone(timedelta(hours=8))
TERMINAL = {"completed", "cancelled", "error", "failed"}


def now():
    return datetime.now(SHANGHAI)


def next_daily(clock: str, current: datetime | None = None):
    current = current or now()
    hour, minute = map(int, clock.split(":"))
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.isoformat()


def friendly_error(exc: Exception):
    # Do not expose remote exception bodies, request URLs, passwords or cookies.
    message = str(exc).lower()
    if "login_required" in message:
        return "请先点击「账号密码登录」，在 Edge 官方页面完成登录后再获取课程。"
    if "login_qr_expired" in message:
        return "扫码登录状态已过期，请点击「扫码登录」重新确认。"
    if "login_browser_expired" in message:
        return "官方网页登录状态已过期，请点击「账号密码登录」重新确认。"
    if "login_rate_limited" in message:
        return "知到暂时限制了登录，请等 5 分钟后再试，期间不要反复点击。"
    if "login_security_check" in message:
        return "知到要求安全验证，请先到官方登录页手动完成验证，再重试。"
    if "login_password_update_required" in message:
        return "知到要求更新密码，请在 Edge 官方页面自行处理后重新登录。"
    if "login_parameters_rejected" in message:
        return "知到密码登录接口未接受请求，请点击「扫码登录」用知到 App 确认登录。"
    if any(word in message for word in ("captcha", "验证", "滑块", "人机")):
        return "平台要求安全验证，请先在知到官方网页完成验证，再重试。"
    if "password invalid" in message or "用户名或密码" in message:
        return "账号或密码不正确，请在 Edge 官方页面检查后重试。"
    if any(word in message for word in ("timeout", "connection", "resolve", "ssl", "proxy")):
        return "连接知到失败，请检查网络，稍后重试。"
    return "知到未接受本次请求，请检查登录状态或稍后重试。"


def course_list(raw: list[dict]):
    result, seen = [], set()
    for course in raw:
        course_id = (
            course.get("courseId") or course.get("id") or course.get("recruitAndCourseId") or course.get("secret")
        )
        if course_id is None or str(course_id) in seen:
            continue
        seen.add(str(course_id))
        result.append(
            {
                "id": str(course_id),
                "name": str(course.get("courseName") or course.get("name") or "未命名课程"),
                "school": str(course.get("schoolName") or course.get("school") or ""),
                "term": str(course.get("semesterName") or course.get("termName") or ""),
            }
        )
    return result


def adapter_factory():
    from app.services.course.zhihuishu.adapter import ZhihuishuAdapter  # noqa: PLC0415

    class JoinedAdapter(ZhihuishuAdapter):
        """Cancellation is acknowledged only after the playback thread exits."""

        def __init__(self):
            super().__init__()
            self.done = threading.Event()
            self.done.set()

        def start_course(self, *args, **kwargs):
            self.done.clear()
            try:
                result = super().start_course(*args, **kwargs)
                if result.get("status") in TERMINAL:
                    self.done.set()
                return result
            except BaseException:
                self.done.set()
                raise

        def _run_task_loop_guarded(self, task_id):
            try:
                super()._run_task_loop_guarded(task_id)
            finally:
                self.done.set()

    return JoinedAdapter()


class Manager:
    def __init__(self, store: Store, factory=adapter_factory, *, poll_seconds=0.5):
        self.store, self.factory, self.poll_seconds = store, factory, poll_seconds
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.closing = threading.Event()
        self.worker = None
        self.scheduler = None
        self.paused = False
        self.active = False
        self.current_adapters = {}
        self.qr = QRLogin(self, course_list, friendly_error)
        self.password_login = PasswordLogin(self, course_list, friendly_error)
        self.state = store.get("queue", {"status": "idle", "jobs": [], "logs": [], "source": "manual"})
        if self.state.get("status") in {"running", "paused", "stopping"}:
            self.state["status"] = "interrupted"
            for job in self.state.get("jobs", []):
                if job["status"] in {"pending", "running", "logging_in"}:
                    job["status"] = "interrupted"
                    job["message"] = "上次程序已退出，请重新开始。"
            store.put("queue", self.state)

    def assert_idle(self):
        if self.active:
            raise ValueError("队列正在运行，请先停止队列，再修改账号或课程。")
        if self.qr.active:
            raise ValueError("正在扫码登录，请先完成扫码或关闭扫码窗口。")
        if self.password_login.active:
            raise ValueError("官方账号密码登录正在进行，请完成登录或关闭登录窗口。")

    def _persist(self):
        self.store.put("queue", self.state)

    def log(self, message):
        with self.lock:
            self.state.setdefault("logs", []).append({"time": now().strftime("%H:%M:%S"), "message": message})
            self.state["logs"] = self.state["logs"][-200:]
            self._persist()

    def snapshot(self):
        with self.lock:
            return {
                "accounts": self.store.accounts(),
                "settings": self.store.settings(),
                "queue": copy.deepcopy(self.state),
                "active": self.active,
                "qr_active": self.qr.active,
                "password_login_active": self.password_login.active,
                "password_login": self.password_login.public(self.password_login.job)
                if self.password_login.job
                else None,
                "limits": {"accounts": ACCOUNT_LIMIT, "parallel_accounts": PARALLEL_LIMIT},
            }

    def _login(self, account):
        adapter = self.factory()
        credentials = account["credentials"]
        cookies = credentials.get("cookies")
        if cookies:
            try:
                adapter.auth.cookies = cookies
                adapter._init_services(adapter.auth.cookies)
                courses = adapter.get_courses()
                return adapter, course_list(courses)
            except Exception:
                self._close_adapter(adapter)
                raise ValueError("login_qr_expired" if credentials.get("qr_only") else "login_browser_expired") from None
        self._close_adapter(adapter)
        raise ValueError("login_required")

    @staticmethod
    def _close_adapter(adapter):
        for component in (
            getattr(adapter, "auth", None),
            getattr(adapter, "learning", None),
            getattr(adapter, "answer", None),
        ):
            session = getattr(component, "session", None)
            if session is not None:
                session.close()

    def refresh_courses(self, account_id):
        # Serializes refresh against edits and queue admission; adapters never share identities.
        with self.lock:
            self.assert_idle()
            account = self.store.account(account_id, private=True)
            try:
                adapter, courses = self._login(account)
            except Exception as exc:
                raise ValueError(friendly_error(exc)) from None
            try:
                self.store.save_courses(account_id, courses, now().strftime("%Y-%m-%d %H:%M"))
                return self.store.account(account_id)
            finally:
                self._close_adapter(adapter)

    def start(self, source="manual", *, account_ids=None):
        with self.lock:
            self.assert_idle()
            available = self.store.accounts(private=True)
            if account_ids is not None:
                valid = {a["id"] for a in available}
                if not account_ids or len(set(account_ids)) != len(account_ids) or not set(account_ids) <= valid:
                    raise ValueError("所选账号已变化，请刷新后重新选择。")
                accounts = [a for a in available if a["id"] in account_ids and a["selected"]]
            else:
                accounts = [a for a in available if a["included"] and a["selected"]]
            if not accounts:
                raise ValueError("请至少为一个执行账号勾选课程。")
            if len(accounts) > ACCOUNT_LIMIT:
                raise ValueError("最多选择 2 个账号执行。")
            jobs = []
            for account in accounts:
                names = {course["id"]: course["name"] for course in account["courses"]}
                for course_id in account["selected"]:
                    if course_id not in names:
                        raise ValueError("课程列表已变化，请重新获取并选择课程。")
                    jobs.append(
                        {
                            "id": uuid4().hex,
                            "account_id": account["id"],
                            "account": account["label"],
                            "course_id": course_id,
                            "course": names[course_id],
                            "status": "pending",
                            "percentage": 0,
                            "message": "等待执行",
                            "current_video": "",
                        }
                    )
            settings = self.store.settings()
            self.state = {
                "status": "running", "source": source, "started": now().isoformat(), "jobs": jobs, "logs": [],
                "execution_mode": settings["execution_mode"],
            }
            self.active, self.paused = True, False
            self.stop_event.clear()
            self.log(f"{'定时' if source == 'daily' else '手动'}开始：{len(accounts)} 个账号，{len(jobs)} 门课程。")
            self.worker = threading.Thread(target=self._run, args=(accounts, settings), daemon=True)
            try:
                self.worker.start()
            except BaseException:
                self.active = False
                self.state["status"] = "failed"
                self._persist()
                raise
            return copy.deepcopy(self.state)

    def _job(self, job_id, **values):
        with self.lock:
            for job in self.state["jobs"]:
                if job["id"] == job_id:
                    job.update(values)
                    break
            # Live progress is held in memory; durable writes happen at transitions.
            if values.get("status") in TERMINAL | {"limited", "skipped"}:
                self._persist()

    def _wait_unpaused(self):
        while self.paused and not self.stop_event.wait(self.poll_seconds):
            pass
        return not self.stop_event.is_set()

    def _play(self, adapter, job, settings):
        self._job(job["id"], status="running", message="正在学习")
        self.log(f"{job['account']} · {job['course']}：开始。")
        result = adapter.start_course(job["course_id"], speed=settings["speed"], auto_answer=settings["auto_answer"])
        if self.paused:
            adapter.pause_task()
        spent, previous, limited = 0.0, time.monotonic(), False
        while not adapter.done.wait(self.poll_seconds):
            current = time.monotonic()
            if not self.paused:
                spent += current - previous
            previous = current
            if self.stop_event.is_set():
                adapter.cancel_task()
            minutes = settings["course_minutes"]
            if minutes and spent >= minutes * 60 and not limited:
                limited = True
                adapter.cancel_task()
            progress = adapter.get_task(result["task_id"]) or {}
            self._job(
                job["id"],
                percentage=progress.get("percentage", 0),
                current_video=str(progress.get("current_video") or ""),
                message="等待当前请求结束"
                if self.stop_event.is_set() or limited
                else "已暂停"
                if self.paused
                else "正在学习",
            )
        progress = adapter.get_task(result["task_id"]) or {}
        status = progress.get("status", "failed")
        if self.stop_event.is_set():
            status, message = "cancelled", "已停止"
        elif limited:
            status, message = "limited", "已达到本门课程运行上限"
        elif status == "completed" and not progress.get("failed"):
            message = "本次执行完成"
        else:
            status, message = "failed", "部分内容未完成，请检查平台状态后重试。"
        self._job(job["id"], status=status, percentage=progress.get("percentage", 0), message=message)
        self.log(f"{job['account']} · {job['course']}：{message}")
        return status not in {"failed", "error"}

    def _cancel_running(self):
        with self.lock:
            for adapter in list(self.current_adapters.values()):
                adapter.cancel_task()

    def _run_account(self, account, settings):
        if not self._wait_unpaused():
            return
        jobs = [job for job in self.state["jobs"] if job["account_id"] == account["id"]]
        self.log(f"正在登录：{account['label']}。")
        try:
            adapter, available = self._login(account)
        except Exception as exc:
            message = friendly_error(exc)
            for job in jobs:
                self._job(job["id"], status="failed", message=message)
            self.log(f"{account['label']}：{message}")
            if not settings["continue_on_error"]:
                self.stop_event.set()
                self._cancel_running()
            return
        with self.lock:
            self.current_adapters[account["id"]] = adapter
        try:
            valid = {course["id"] for course in available}
            for job in jobs:
                if not self._wait_unpaused():
                    break
                try:
                    if job["course_id"] not in valid:
                        self._job(job["id"], status="failed", message="这门课程已不在当前账号中，请重新获取课程。")
                        success = False
                    else:
                        success = self._play(adapter, job, settings)
                except Exception as exc:
                    # Each account waits for its playback worker before its next course.
                    adapter.cancel_task()
                    adapter.done.wait()
                    message = friendly_error(exc)
                    self._job(job["id"], status="failed", message=message)
                    self.log(f"{account['label']} · {job['course']}：{message}")
                    success = False
                if not success and not settings["continue_on_error"]:
                    self.stop_event.set()
                    self._cancel_running()
                    break
        finally:
            with self.lock:
                self.current_adapters.pop(account["id"], None)
            self._close_adapter(adapter)

    def _run(self, accounts, settings):
        try:
            if settings["execution_mode"] == "parallel":
                workers = max(1, min(PARALLEL_LIMIT, int(settings["parallel_accounts"]), len(accounts)))
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zhidao-account") as pool:
                    try:
                        futures = [pool.submit(self._run_account, account, settings) for account in accounts]
                        for future in as_completed(futures):
                            future.result()
                    except Exception:
                        self.stop_event.set()
                        self._cancel_running()
                        raise
            else:
                for account in accounts:
                    if not self._wait_unpaused():
                        break
                    self._run_account(account, settings)
        except Exception:
            self.log("队列遇到本地错误，请停止后重试。")
            self.stop_event.set()
        finally:
            with self.lock:
                for job in self.state["jobs"]:
                    if job["status"] in {"pending", "running", "logging_in"}:
                        job.update(status="skipped", message="本次未执行")
                stopped = self.stop_event.is_set()
                failed = any(job["status"] in {"failed", "error"} for job in self.state["jobs"])
                self.state["status"] = "cancelled" if stopped else "partial" if failed else "completed"
                self.state["finished"] = now().isoformat()
                self.active, self.paused = False, False
                self.log("队列已停止。" if stopped else "本轮执行结束。")

    def control(self, action):
        with self.lock:
            if not self.active:
                raise ValueError("当前没有正在运行的队列。")
            if self.stop_event.is_set():
                raise ValueError("正在停止，请等待当前请求结束。")
            if action == "stop":
                self.stop_event.set()
                self.paused = False
                self.state["status"] = "stopping"
                self._cancel_running()
                self.log("收到停止请求，等待当前请求结束。")
            elif action in {"pause", "resume"}:
                self.paused = action == "pause"
                self.state["status"] = "paused" if self.paused else "running"
                for adapter in list(self.current_adapters.values()):
                    (adapter.pause_task if self.paused else adapter.resume_task)()
                self.log("队列已暂停。" if self.paused else "队列继续执行。")
            else:
                raise ValueError("不支持的操作。")
            self._persist()

    def update_settings(self, values):
        with self.lock:
            self.assert_idle()
            settings = self.store.settings()
            if not 1 <= values.get("parallel_accounts", settings["parallel_accounts"]) <= PARALLEL_LIMIT:
                raise ValueError("同时运行账号数应为 1 到 2。")
            changed = (
                values["daily_time"] != settings["daily_time"] or values["daily_enabled"] != settings["daily_enabled"]
            )
            settings.update(values)
            if settings["daily_enabled"]:
                if changed or not settings["next_run"]:
                    settings["next_run"] = next_daily(settings["daily_time"])
            else:
                settings["next_run"] = None
            self.store.put("settings", settings)
            return settings

    def tick(self, current=None):
        with self.lock:
            current = current or now()
            settings = self.store.settings()
            due = settings.get("next_run")
            if not settings["daily_enabled"] or not due or current < datetime.fromisoformat(due):
                return
            # Persist consumption before admission: no duplicate/catch-up backlog.
            settings["next_run"] = next_daily(settings["daily_time"], current)
            self.store.put("settings", settings)
            if self.active:
                self.log("今日定时时间已到，现有队列仍在执行，本次不重复启动。")
                return
            try:
                self.start("daily")
            except ValueError:
                self.log("今日定时未启动：还没有勾选账号和课程。")

    def start_scheduler(self):
        def loop():
            while not self.closing.wait(3):
                try:
                    self.tick()
                except Exception:
                    self.log("定时检查遇到本地错误，请检查设置。")

        self.scheduler = threading.Thread(target=loop, daemon=True)
        self.scheduler.start()

    def close(self):
        self.closing.set()
        with self.lock:
            if self.qr.active:
                self.qr.cancel(self.qr.job["id"])
            if self.password_login.active:
                self.password_login.cancel(self.password_login.job["id"])
            if self.active and not self.stop_event.is_set():
                self.control("stop")
        if self.worker:
            self.worker.join(timeout=15)
        if self.password_login.thread:
            self.password_login.thread.join(timeout=5)
