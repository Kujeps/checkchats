"""Точка входа: запускает локальный сервер и открывает интерфейс в браузере."""
import asyncio
import logging
import socket
import sys
import webbrowser

import uvicorn

from . import db, geo, updater
from .engine import engine
from .paths import DATA_DIR

HOST = "127.0.0.1"
RESTART_PORT = None   # перезапуск после обновления: порт прошлой копии


def _free_port(start=8765):
    if "--port" in sys.argv:
        return int(sys.argv[sys.argv.index("--port") + 1])
    start = RESTART_PORT or start
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
    global RESTART_PORT
    RESTART_PORT = updater.consume_restart_flag()
    db.conn()
    db.reset_stale()
    geo.backfill(db)
    port = _free_port()
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    app.state.server = server
    app.state.port = port
    app.state.restart = False
    url = f"http://{HOST}:{port}"

    async def open_browser():
        while not server.started:
            await asyncio.sleep(0.1)
        print(f"\n  CheckChats запущен: {url}\n  Данные: {DATA_DIR}\n  Чтобы выйти — закройте это окно.\n")
        # после обновления страница уже открыта и сама переподключится
        if "--no-browser" not in sys.argv and not RESTART_PORT:
            webbrowser.open(url)

    asyncio.create_task(open_browser())
    try:
        await server.serve()
    finally:
        await engine.stop()
    return app.state.restart, port


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.FileHandler(DATA_DIR / "checkchats.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        restart, port = asyncio.run(_serve())
    except KeyboardInterrupt:
        return
    if restart:
        print("\n  Программа обновлена — перезапуск…\n")
        updater.restart(port)


if __name__ == "__main__":
    main()
