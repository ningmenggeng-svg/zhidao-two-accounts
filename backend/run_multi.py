"""Run the local edition, or package this entry point with PyInstaller."""

import argparse
import json
import logging
import socket
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from app.local_multi.web import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18665)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    directory = args.data_dir or root / "data-multi"
    url = f"http://127.0.0.1:{args.port}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", args.port))
    except OSError:
        sock.close()
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=2) as response:
                same_app = json.load(response).get("application") == "zhidao-multi-local"
        except Exception:
            same_app = False
        if same_app:
            if not args.no_browser:
                webbrowser.open(url)
            return
        raise RuntimeError(f"本机端口 {args.port} 已被其他程序占用，请用 --port 指定其他端口。")
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=directory / "service.log",
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
    try:
        app = create_app(directory)
        config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_config=None, access_log=False)
        server = uvicorn.Server(config)
        app.state.shutdown = lambda: setattr(server, "should_exit", True)

        def open_when_ready():
            for _ in range(100):
                if server.started:
                    webbrowser.open(url)
                    return
                threading.Event().wait(0.1)

        if not args.no_browser:
            threading.Thread(target=open_when_ready, daemon=True).start()
        (directory / "runtime.json").write_text(json.dumps({"url": url}), encoding="utf-8")
        server.run(sockets=[sock])
    finally:
        sock.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # A windowed executable has no console; make startup failures visible.
        if getattr(sys, "frozen", False):
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, str(exc), "知到多账号版 · 启动失败", 0x10)
        else:
            raise
