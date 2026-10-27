"""Encrypted local credentials and transactional configuration storage."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet


def _dpapi(value: bytes, *, decrypt: bool = False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    api.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    api.restype = wintypes.BOOL
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("无法解锁本机账号数据，请使用原 Windows 用户运行。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


ACCOUNT_LIMIT = 2
PARALLEL_LIMIT = 2


DEFAULT_SETTINGS = {
    "daily_enabled": False,
    "daily_time": "09:00",
    "next_run": None,
    "speed": 1.0,
    "auto_answer": True,
    "continue_on_error": True,
    "course_minutes": 0,
    "execution_mode": "sequential",
    "parallel_accounts": 1,
}


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        key_file = directory / ("vault.dpapi" if os.name == "nt" else "vault.key")
        if key_file.exists():
            key = key_file.read_bytes()
            if os.name == "nt":
                key = _dpapi(key, decrypt=True)
        else:
            if (directory / "accounts.sqlite3").exists():
                raise RuntimeError("加密密钥文件丢失，请恢复原 data-multi 文件夹后再启动。")
            key = Fernet.generate_key()
            # Exclusive create: never silently replace the key for an existing vault.
            with key_file.open("xb") as stream:
                stream.write(_dpapi(key) if os.name == "nt" else key)
            if os.name != "nt":
                key_file.chmod(0o600)
        self.cipher = Fernet(key)
        self.path = directory / "accounts.sqlite3"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, position INTEGER NOT NULL, label TEXT NOT NULL,
                    profile TEXT NOT NULL UNIQUE, secret TEXT NOT NULL,
                    included INTEGER NOT NULL DEFAULT 1, courses TEXT NOT NULL DEFAULT '[]',
                    selected TEXT NOT NULL DEFAULT '[]', updated TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            self._upgrade_manual_login(db)

    def _upgrade_manual_login(self, db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
        if "username" not in columns:
            return
        # Prepare every conversion before changing the legacy schema. Keep the
        # existing browser directory so saved official sessions remain usable.
        converted = []
        for row in db.execute("SELECT id,username,secret FROM accounts"):
            credentials = json.loads(self.cipher.decrypt(row["secret"].encode()).decode())
            credentials.pop("password", None)
            profile = row["id"] + "-" + hashlib.sha256(row["username"].encode()).hexdigest()[:12]
            secret = self.cipher.encrypt(json.dumps(credentials).encode()).decode()
            converted.append((profile, secret, row["id"]))
        db.execute("BEGIN IMMEDIATE")
        db.execute("ALTER TABLE accounts RENAME COLUMN username TO profile")
        db.executemany("UPDATE accounts SET profile=?,secret=? WHERE id=?", converted)

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA secure_delete=ON")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _decode(self, row, private=False):
        result = dict(row)
        result["included"] = bool(result["included"])
        for key in ("courses", "selected"):
            result[key] = json.loads(result[key])
        secret = result.pop("secret")
        profile = result.pop("profile")
        if private:
            result["profile"] = profile
            result["credentials"] = json.loads(self.cipher.decrypt(secret.encode()).decode())
        return result

    def accounts(self, *, private=False):
        with self.connection() as db:
            return [self._decode(row, private) for row in db.execute("SELECT * FROM accounts ORDER BY position, id")]

    def account(self, account_id: str, *, private=False):
        with self.connection() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise ValueError("这个账号已不存在，请刷新列表。")
            return self._decode(row, private)

    def save(self, label: str, account_id: str | None = None):
        label = label.strip()
        if len(label) > 100:
            raise ValueError("备注最多 100 字。")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone() if account_id else None
            if account_id and not existing:
                raise ValueError("这个账号已不存在。")
            account_id = account_id or uuid4().hex
            if existing:
                db.execute("UPDATE accounts SET label=? WHERE id=?", (label or existing["label"], account_id))
            else:
                if db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] >= ACCOUNT_LIMIT:
                    raise ValueError("最多保存 2 个账号，请先删除不再使用的账号。")
                position = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM accounts").fetchone()[0]
                secret = self.cipher.encrypt(b"{}").decode()
                db.execute(
                    "INSERT INTO accounts(id,position,label,profile,secret) VALUES(?,?,?,?,?)",
                    (account_id, position, label or f"账号 {position + 1}", account_id, secret),
                )
        return self.account(account_id)

    def save_cookies(self, account_id: str, cookies: dict, *, qr_only=False, browser_only=False):
        with self.connection() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise ValueError("这个账号已不存在。")
            credentials = self._decode(row, True)["credentials"]
            credentials.pop("password", None)
            credentials["cookies"] = cookies
            if qr_only:
                credentials["qr_only"] = True
                credentials.pop("browser_only", None)
            if browser_only:
                credentials["browser_only"] = True
                credentials.pop("qr_only", None)
            token = self.cipher.encrypt(json.dumps(credentials).encode()).decode()
            db.execute("UPDATE accounts SET secret=? WHERE id=?", (token, account_id))

    def save_courses(self, account_id: str, courses: list[dict], updated: str):
        with self.connection() as db:
            row = db.execute("SELECT selected FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise ValueError("这个账号已不存在。")
            valid = {c["id"] for c in courses}
            selected = [value for value in json.loads(row[0]) if value in valid]
            db.execute(
                "UPDATE accounts SET courses=?,selected=?,updated=? WHERE id=?",
                (json.dumps(courses, ensure_ascii=False), json.dumps(selected), updated, account_id),
            )

    def select(self, account_id: str, selected: list[str], included: bool):
        with self.connection() as db:
            row = db.execute("SELECT courses FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise ValueError("这个账号已不存在。")
            valid = {c["id"] for c in json.loads(row[0])}
            if any(value not in valid for value in selected):
                raise ValueError("所选课程已变化，请重新获取课程。")
            selected = list(dict.fromkeys(selected))
            db.execute(
                "UPDATE accounts SET selected=?,included=? WHERE id=?",
                (json.dumps(selected), int(included), account_id),
            )

    def reorder(self, ids: list[str]):
        with self.connection() as db:
            actual = {row[0] for row in db.execute("SELECT id FROM accounts")}
            if len(ids) != len(set(ids)) or set(ids) != actual:
                raise ValueError("账号列表有变化，请刷新后再排序。")
            for position, account_id in enumerate(ids):
                db.execute("UPDATE accounts SET position=? WHERE id=?", (position, account_id))

    def delete(self, account_id: str):
        with self.connection() as db:
            row = db.execute("SELECT profile FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                return
            profile = self.browser_directory(row["profile"])
            if profile.exists():
                try:
                    shutil.rmtree(profile)
                except OSError:
                    raise ValueError("浏览器资料仍被占用，请关闭该账号的 Edge 登录窗口后重试。") from None
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def browser_directory(self, profile: str):
        base = (self.directory / "browser-profiles").resolve()
        target = (base / profile).resolve()
        if not profile or Path(profile).name != profile or target.parent != base or target.name != profile:
            raise ValueError("账号的浏览器资料路径无效。")
        return target

    def get(self, key: str, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key: str, value):
        with self.connection() as db:
            db.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def settings(self):
        settings = {**DEFAULT_SETTINGS, **self.get("settings", {})}
        settings["parallel_accounts"] = max(1, min(PARALLEL_LIMIT, int(settings["parallel_accounts"])))
        return settings
