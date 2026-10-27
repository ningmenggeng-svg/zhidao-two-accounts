"""Opt-in, current-user Windows startup for the packaged desktop app."""

import os
import subprocess
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ZhidaoMultiLocal"


class Autostart:
    def __init__(self, *, executable=None, registry=None, supported=None):
        self.executable = Path(executable or sys.executable).resolve()
        self.supported = supported if supported is not None else os.name == "nt" and bool(getattr(sys, "frozen", False))
        self.registry = registry
        if self.supported and registry is None:
            import winreg  # noqa: PLC0415

            self.registry = winreg
        self.command = subprocess.list2cmdline([str(self.executable), "--no-browser"])

    def status(self):
        enabled = False
        error = None
        if self.supported:
            registry = self.registry
            try:
                with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ) as key:
                    value, kind = registry.QueryValueEx(key, VALUE_NAME)
                    enabled = kind == registry.REG_SZ and value == self.command
            except FileNotFoundError:
                pass
            except OSError:
                error = "无法读取开机自启动设置。"
        result = {"enabled": enabled, "supported": self.supported}
        if error:
            result["error"] = error
        return result

    def set_enabled(self, enabled: bool):
        if not self.supported:
            raise ValueError("开机自启动仅支持 Windows 成品版。")
        registry = self.registry
        try:
            if enabled:
                if not self.executable.is_file():
                    raise ValueError("程序文件不存在，无法设置开机自启动。")
                with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE) as key:
                    registry.SetValueEx(key, VALUE_NAME, 0, registry.REG_SZ, self.command)
            else:
                try:
                    with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE) as key:
                        registry.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        except OSError:
            raise ValueError("无法修改开机自启动设置，请检查当前 Windows 用户权限。") from None
        status = self.status()
        if status.get("error") or status["enabled"] != enabled:
            raise ValueError("开机自启动设置未生效，请重试。")
        return status
