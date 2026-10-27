"""User-confirmed QR login through the current official Zhidao endpoints."""

import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.services.course.zhihuishu.auth import LOGIN_SERVICE, LOGIN_URL

QR_BASE = "https://user.zhihuishu.com/zhsuser/qrCodeLogin"


class QRLogin:
    def __init__(self, manager, normalize_courses, friendly_error):
        self.manager = manager
        self.normalize = normalize_courses
        self.error = friendly_error
        self.active = False
        self.job = None
        self.request_lock = threading.Lock()

    def start(self, account_id):
        with self.manager.lock:
            self.manager.assert_idle()
            account = self.manager.store.account(account_id)
            job = {
                "id": uuid4().hex,
                "account_id": account_id,
                "label": account["label"],
                "created": time.monotonic(),
                "status": "waiting",
                "message": "请使用知到 App 扫码并确认登录。",
                "adapter": self.manager.factory(),
            }
            self.job, self.active = job, True
        try:
            auth = job["adapter"].auth
            payload = auth.session.get(f"{QR_BASE}/getLoginQrImg", timeout=10).json()
            if not isinstance(payload.get("img"), str) or not payload.get("qrToken"):
                raise ValueError("qr_response_invalid")
            with self.manager.lock:
                if self.job is not job or not self.active:
                    raise ValueError("扫码已取消。")
                job["token"] = payload["qrToken"]
            return {
                "id": job["id"],
                "image": "data:image/png;base64," + payload["img"],
                "label": account["label"],
                "status": "waiting",
            }
        except Exception:
            with self.manager.lock:
                if self.job is job:
                    self.active = False
                    job.update(status="failed", message="二维码获取失败，请检查网络后重试。")
            self.manager._close_adapter(job["adapter"])
            raise ValueError("二维码获取失败，请检查网络后重试。") from None

    @staticmethod
    def public(job):
        return {key: job[key] for key in ("id", "status", "message", "course_count") if key in job}

    def poll(self, job_id):
        with self.request_lock:
            with self.manager.lock:
                job = self.job
                if not job or job["id"] != job_id:
                    raise ValueError("二维码已更新，请使用当前窗口中的二维码。")
                if not self.active:
                    return self.public(job)
                if time.monotonic() - job["created"] > 120:
                    self.active = False
                    job.update(status="expired", message="二维码已过期，请重新获取。")
                    self.manager._close_adapter(job["adapter"])
                    return self.public(job)
            adapter = job["adapter"]
            auth = adapter.auth
            try:
                payload = auth.session.get(
                    f"{QR_BASE}/getLoginQrInfo", params={"qrToken": job["token"]}, timeout=10
                ).json()
                status = payload.get("status")
                if status in (2, 3):
                    with self.manager.lock:
                        if self.job is job and self.active:
                            self.active = False
                            job.update(
                                status="expired" if status == 2 else "cancelled", message="二维码已失效，请重新获取。"
                            )
                    self.manager._close_adapter(adapter)
                elif status == 1:
                    with self.manager.lock:
                        if self.job is not job or not self.active:
                            return self.public(job)
                        job["message"] = "已确认登录，正在获取课程。"
                    ticket = payload.get("oncePassword")
                    if not ticket:
                        raise ValueError("qr_ticket_missing")
                    auth.session.get(LOGIN_URL, params={"pwd": ticket, "service": LOGIN_SERVICE}, timeout=15)
                    auth.cookies = auth.session.cookies
                    adapter._init_services(auth.cookies)
                    courses = self.normalize(adapter.get_courses())
                    with self.manager.lock:
                        if self.job is job and self.active:
                            self.manager.store.save_cookies(job["account_id"], auth.cookies, qr_only=True)
                            updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
                            self.manager.store.save_courses(job["account_id"], courses, updated)
                            self.active = False
                            job.update(
                                status="success",
                                course_count=len(courses),
                                message=f"登录成功，已获取 {len(courses)} 门课程。",
                            )
                    self.manager._close_adapter(adapter)
                elif status == 0:
                    with self.manager.lock:
                        job["message"] = "已扫描，请在知到 App 上确认登录。"
                elif status != -1:
                    raise ValueError("qr_status_unknown")
            except Exception as exc:
                with self.manager.lock:
                    if self.job is job and self.active:
                        self.active = False
                        job.update(status="failed", message=self.error(exc))
                self.manager._close_adapter(adapter)
            with self.manager.lock:
                if job["status"] in {"success", "failed", "expired", "cancelled"}:
                    self.manager._close_adapter(adapter)
                return self.public(job)

    def cancel(self, job_id):
        with self.manager.lock:
            job = self.job
            if job and job["id"] == job_id and self.active:
                self.active = False
                job.update(status="cancelled", message="扫码已取消。")
                if self.request_lock.acquire(blocking=False):
                    try:
                        self.manager._close_adapter(job["adapter"])
                    finally:
                        self.request_lock.release()
            return {"ok": True}
