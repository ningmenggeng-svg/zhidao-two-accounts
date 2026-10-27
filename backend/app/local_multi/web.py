"""Loopback-only HTTP interface. No passwords are returned to the browser."""

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .autostart import Autostart
from .service import Manager
from .store import Store

STATIC = Path(__file__).parent / "static"


class AccountInput(BaseModel):
    model_config = {"extra": "forbid"}
    label: str = Field(default="", max_length=100)


class AutostartInput(BaseModel):
    enabled: bool


class StartInput(BaseModel):
    account_ids: list[str] = Field(min_length=1, max_length=200)


class SelectionInput(BaseModel):
    selected: list[str] = Field(max_length=1000)
    included: bool = True


class OrderInput(BaseModel):
    ids: list[str] = Field(max_length=200)


class SettingsInput(BaseModel):
    daily_enabled: bool = False
    daily_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    auto_answer: bool = True
    continue_on_error: bool = True
    course_minutes: int = Field(default=0, ge=0, le=240)
    execution_mode: Literal["sequential", "parallel"] = "sequential"
    parallel_accounts: int = Field(default=2, ge=1, le=2)


def create_app(directory: Path, *, factory=None, scheduler=True, autostart=None):
    store = Store(directory)
    manager = Manager(store, **({"factory": factory} if factory else {}))
    startup = autostart if autostart is not None else Autostart()

    @asynccontextmanager
    async def lifespan(app):
        if scheduler:
            manager.start_scheduler()
        yield
        manager.close()

    app = FastAPI(title="知到多账号本地版", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = manager
    app.state.shutdown = None

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"detail": "仅允许本机访问。"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            if request.headers.get("x-local-request") != "1":
                return JSONResponse({"detail": "请从本地应用界面操作。"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != f"http://{request.headers.get('host')}":
                return JSONResponse({"detail": "不允许跨站访问。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Pydantic errors otherwise echo the invalid input, including passwords.
        return JSONResponse({"detail": "填写内容不符合要求，请检查账号、时间和运行上限。"}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse({"detail": "本地操作失败，请重新打开程序，或查看 data/service.log。"}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"application": "zhidao-multi-local", "version": "1.5.0"}

    @app.get("/api/state")
    def state():
        return {**manager.snapshot(), "autostart": startup.status()}

    @app.put("/api/autostart")
    def update_autostart(body: AutostartInput):
        return startup.set_enabled(body.enabled)

    @app.post("/api/accounts")
    def add_account(body: AccountInput):
        with manager.lock:
            manager.assert_idle()
            return store.save(body.label)

    @app.put("/api/accounts/order")
    def reorder(body: OrderInput):
        with manager.lock:
            manager.assert_idle()
            store.reorder(body.ids)
        return {"ok": True}

    @app.put("/api/accounts/{account_id}")
    def edit_account(account_id: str, body: AccountInput):
        with manager.lock:
            manager.assert_idle()
            return store.save(body.label, account_id)

    @app.delete("/api/accounts/{account_id}")
    def delete_account(account_id: str):
        with manager.lock:
            manager.assert_idle()
            store.delete(account_id)
        return {"ok": True}

    @app.post("/api/accounts/{account_id}/courses")
    def refresh_courses(account_id: str):
        return manager.refresh_courses(account_id)

    @app.post("/api/accounts/{account_id}/qr")
    def start_qr(account_id: str):
        return manager.qr.start(account_id)

    @app.post("/api/accounts/{account_id}/password-login")
    def start_password_login(account_id: str):
        return manager.password_login.start(account_id)

    @app.get("/api/password-login/{job_id}")
    def poll_password_login(job_id: str):
        return manager.password_login.poll(job_id)

    @app.delete("/api/password-login/{job_id}")
    def cancel_password_login(job_id: str):
        return manager.password_login.cancel(job_id)

    @app.post("/api/password-login/{job_id}/check")
    def check_password_login(job_id: str):
        return manager.password_login.check(job_id)

    @app.post("/api/password-login/{job_id}/show")
    def show_password_login(job_id: str):
        return manager.password_login.show(job_id)

    @app.get("/api/qr/{job_id}")
    def poll_qr(job_id: str):
        return manager.qr.poll(job_id)

    @app.delete("/api/qr/{job_id}")
    def cancel_qr(job_id: str):
        return manager.qr.cancel(job_id)

    @app.put("/api/accounts/{account_id}/selection")
    def select_courses(account_id: str, body: SelectionInput):
        with manager.lock:
            manager.assert_idle()
            store.select(account_id, body.selected, body.included)
        return {"ok": True}

    @app.put("/api/settings")
    def settings(body: SettingsInput):
        return manager.update_settings(body.model_dump())

    @app.post("/api/queue/start")
    def start(body: StartInput | None = None):
        return manager.start(account_ids=body.account_ids if body else None)

    @app.post("/api/queue/{action}")
    def control(action: str):
        manager.control(action)
        return {"ok": True}

    @app.post("/api/shutdown")
    def shutdown():
        if app.state.shutdown is None:
            raise HTTPException(400, "当前运行方式不支持从网页退出。")
        # Let this HTTP response finish before stopping the server.
        threading.Timer(0.4, app.state.shutdown).start()
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    def script():
        return FileResponse(STATIC / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    def stylesheet():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    return app
