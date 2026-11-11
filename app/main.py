"""Точка входа: запускает локальный сервер и открывает интерфейс в браузере."""
import asyncio
import logging
import socket
import sys
import webbrowser

import uvicorn

from . import db
from .engine import engine
from .paths import DATA_DIR

HOST = "127.0.0.1"


def _free_port(start=8765):
    if "--port" in sys.argv:
        return int(sys.argv[sys.argv.index("--port") + 1])
    for port in range(start, start + 50):
        with socket.socket() as s:
            if sys.platform != "win32":   # на Windows SO_REUSEADDR разрешает двойной bind
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError("нет свободного порта")


async def _serve():
    from .web import app
    db.conn()
    db.reset_stale()
    port = _free_port()
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    url = f"http://{HOST}:{port}"

    async def open_browser():
        while not server.started:
            await asyncio.sleep(0.1)
        print(f"\n  CheckChats запущен: {url}\n  Данные: {DATA_DIR}\n  Чтобы выйти — закройте это окно.\n")
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)

    asyncio.create_task(open_browser())
    try:
        await server.serve()
    finally:
        await engine.stop()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(DATA_DIR / "checkchats.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
