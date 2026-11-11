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


async def _run(*args, timeout=120, check=True) -> str:
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
    if check and proc.returncode != 0:
        raise UpdateError(f"{' '.join(args[:2])}: {text[-500:]}")
    return text if check else (proc.returncode, text)


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
    """Переключает папку на свежую версию из GitHub и ставит зависимости, если они поменялись.

    Обычный git pull здесь не годится: на Windows git иногда считает файлы «изменёнными» только
    из-за переводов строк, и pull отказывается работать. Поэтому: скачиваем, проверяем, что настоящих
    правок и своих коммитов в папке нет, и переключаемся на версию из GitHub. Папки data и .venv
    git не трогает — они в .gitignore.
    """
    reason = why_not_updatable()
    if reason:
        raise UpdateError(reason)
    await _git("fetch", "--quiet", timeout=60)
    before = await _git("rev-parse", "HEAD")
    if int(await _git("rev-list", "--count", "@{u}..HEAD") or 0):
        raise UpdateError("в папке программы есть свои коммиты — обновите вручную через git pull")
    if not int(await _git("rev-list", "--count", "HEAD..@{u}") or 0):
        return {"updated": False, "changes": []}

    dirty = set((await _git("diff", "--name-only")).splitlines()) | \
        set((await _git("diff", "--cached", "--name-only")).splitlines())
    real = []
    for f in sorted(x for x in dirty if x.strip()):
        code, _ = await _git("diff", "HEAD", "--ignore-cr-at-eol", "--quiet", "--", f, check=False)
        if code != 0:
            real.append(f)
    if real:
        raise UpdateError("изменены файлы программы: " + ", ".join(real[:10])
                          + " — сохраните свои правки отдельно и обновите вручную")

    req_before = _requirements_hash()
    await _git("reset", "--hard", "--quiet", "@{u}")
    after = await _git("rev-parse", "HEAD")
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
