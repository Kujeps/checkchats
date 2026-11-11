"""Обновление программы из GitHub кнопкой в интерфейсе: git pull, зависимости, перезапуск."""
import asyncio
import hashlib
import os
import sys

from .paths import BASE_DIR, DATA_DIR

RESTART_FLAG = DATA_DIR / ".restarting"     # после перезапуска не открывать браузер второй раз
RESTART_EXIT_CODE = 3                       # run.bat по этому коду запускает программу заново


class UpdateError(Exception):
    pass


def why_not_updatable() -> str | None:
    if getattr(sys, "frozen", False):
        return "это собранная версия (.exe) — обновите исходники через git pull и пересоберите build.bat"
    if not (BASE_DIR / ".git").exists():
        return "папка программы не скачана через git clone — обновление кнопкой недоступно"
    return None


async def _run(*args, timeout=120) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")   # не ждать ввода пароля в консоли
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=BASE_DIR, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        raise UpdateError(f"не найдена программа {args[0]} — установите Git (winget install --id Git.Git -e)")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise UpdateError(f"{' '.join(args[:2])}: не ответил за {timeout} с")
    text = out.decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise UpdateError(f"{' '.join(args[:2])}: {text[-500:]}")
    return text


def _git(*args, **kw):
    return _run("git", "-c", "core.quotepath=off", *args, **kw)


def _requirements_hash() -> str:
    p = BASE_DIR / "requirements.txt"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


async def version() -> dict:
    reason = why_not_updatable()
    if reason:
        return {"updatable": False, "reason": reason}
    try:
        line = await _git("log", "-1", "--format=%h|%cI|%s")
    except UpdateError as e:
        return {"updatable": False, "reason": str(e)}
    commit, date, subject = (line.split("|", 2) + ["", ""])[:3]
    return {"updatable": True, "commit": commit, "date": date, "subject": subject}


async def check() -> dict:
    """Скачивает сведения о новых версиях (без изменения файлов) и перечисляет, что изменится."""
    reason = why_not_updatable()
    if reason:
        raise UpdateError(reason)
    await _git("fetch", "--quiet", timeout=60)
    log = await _git("log", "--format=%h %s", "HEAD..@{u}")
    changes = [ln for ln in log.splitlines() if ln.strip()]
    return {"behind": len(changes), "changes": changes}


async def pull() -> dict:
    """git pull и, если поменялись зависимости, их установка. Возвращает список изменений."""
    reason = why_not_updatable()
    if reason:
        raise UpdateError(reason)
    before = await _git("rev-parse", "HEAD")
    req_before = _requirements_hash()
    await _git("pull", "--ff-only", timeout=120)
    after = await _git("rev-parse", "HEAD")
    if after == before:
        return {"updated": False, "changes": []}
    changes = [ln for ln in (await _git("log", "--format=%h %s", f"{before}..{after}")).splitlines() if ln.strip()]
    deps = _requirements_hash() != req_before
    if deps:
        await _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", timeout=600)
    return {"updated": True, "changes": changes, "deps": deps}


def restart(port: int):
    """Вызывается после остановки сервера: перезапуск программы с теми же аргументами."""
    RESTART_FLAG.write_text(str(port))   # новая копия займёт тот же порт — открытая страница его найдёт
    if os.name == "nt":
        # На Windows программу заново запустит run.bat, увидев этот код выхода
        sys.exit(RESTART_EXIT_CODE)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def consume_restart_flag() -> int | None:
    """Порт прошлой копии, если это перезапуск после обновления."""
    if not RESTART_FLAG.exists():
        return None
    try:
        port = int(RESTART_FLAG.read_text().strip() or 0) or None
    except ValueError:
        port = None
    RESTART_FLAG.unlink(missing_ok=True)
    return port
