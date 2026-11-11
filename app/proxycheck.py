"""Проверка прокси: доходит ли через него соединение до серверов Telegram.

Если в строке не указан тип (host:port:user:pass), пробуем socks5, затем http,
и возвращаем строку с найденным типом.
"""
import asyncio
import time

from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError, ProxyType
from python_socks.async_.asyncio import Proxy

from .accounts import parse_proxy

TG_DC = ("149.154.167.51", 443)   # DC2 Telegram
TYPES = {"socks5": ProxyType.SOCKS5, "socks4": ProxyType.SOCKS4, "http": ProxyType.HTTP}


def net_error_text(e: BaseException) -> str:
    """Понятный текст сетевой ошибки (в том числе виндовых WinError)."""
    win = getattr(e, "winerror", None)
    if win == 121:
        return "таймаут сети: прокси или Telegram не отвечает (WinError 121)"
    if win == 10060 or isinstance(e, (asyncio.TimeoutError, TimeoutError, ProxyTimeoutError)):
        return "таймаут: сервер не ответил вовремя"
    if win == 10061 or isinstance(e, ConnectionRefusedError):
        return "соединение отклонено: порт закрыт или неверный адрес"
    if win in (10051, 10065):
        return "сеть недоступна"
    if isinstance(e, ProxyConnectionError):
        return "не удалось подключиться к прокси: сервер не отвечает или порт закрыт"
    if isinstance(e, ProxyError):
        msg = str(e)
        if "auth" in msg.lower() or "407" in msg:
            return "прокси отклонил логин/пароль"
        return f"прокси отказал: {msg}"
    return str(e) or type(e).__name__


def _explicit_type(raw) -> bool:
    if not isinstance(raw, str):
        return True
    s = raw.strip()
    return "://" in s or s[:1] in "[{"


async def _try(ptype: str, p: dict, timeout: float) -> int:
    proxy = Proxy.create(proxy_type=TYPES[ptype], host=p["addr"], port=p["port"],
                         username=p.get("username"), password=p.get("password"), rdns=True)
    t = time.monotonic()
    sock = await proxy.connect(*TG_DC, timeout=timeout)
    ms = round((time.monotonic() - t) * 1000)
    sock.close()
    return ms


async def check(raw, timeout: float = 12) -> dict:
    """{ok, type, ms, error, fixed} — fixed: строка прокси с явным типом."""
    try:
        p = parse_proxy(raw)
    except ValueError as e:
        return {"ok": False, "type": None, "ms": None, "error": str(e), "fixed": raw}
    if not p:
        return {"ok": False, "type": None, "ms": None, "error": "пустой прокси", "fixed": raw}

    explicit = _explicit_type(raw)
    types = [p["proxy_type"]] if explicit else ["socks5", "http"]
    # Типы пробуем одновременно: прокси «не того» типа часто молчит до таймаута.
    # Первый успешный ответ — результат, остальные попытки отменяем.
    tasks = {asyncio.ensure_future(_try(t, p, timeout)): t for t in types}
    results_by_type = {}
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                t = tasks[task]
                if task.exception() is None:
                    fixed = raw if explicit else f"{t}://{raw.strip()}"
                    return {"ok": True, "type": t, "ms": task.result(), "error": None, "fixed": fixed}
                results_by_type[t] = task.exception()
    finally:
        for task in pending:
            task.cancel()
    results = [results_by_type[t] for t in types]
    # ProxyTimeoutError — подкласс OSError, поэтому проверяем его раньше
    unreachable = [r for r in results if isinstance(r, (ProxyConnectionError, OSError))
                   and not isinstance(r, ProxyTimeoutError)]
    if unreachable:
        # До самого прокси не достучались — тип тут ни при чём
        return {"ok": False, "type": None, "ms": None, "error": net_error_text(unreachable[0]), "fixed": raw}
    errors = [f"{t}: {net_error_text(r)}" if len(types) > 1 else net_error_text(r) for t, r in zip(types, results)]
    return {"ok": False, "type": None, "ms": None, "error": "; ".join(errors), "fixed": raw}


async def check_direct(timeout: float = 8) -> dict:
    """Доступен ли Telegram с этого компьютера без прокси."""
    t = time.monotonic()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(*TG_DC), timeout)
        w.close()
        return {"ok": True, "ms": round((time.monotonic() - t) * 1000), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": None, "error": net_error_text(e)}


async def check_many(lines: list, concurrency: int = 30) -> list:
    sem = asyncio.Semaphore(concurrency)

    async def one(ln):
        async with sem:
            r = await check(ln)
            r["line"] = ln
            return r

    return await asyncio.gather(*(one(ln) for ln in lines))
