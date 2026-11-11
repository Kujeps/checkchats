"""Аккаунты: импорт пар .session + .json, прокси, создание клиентов Telethon."""
import io
import json
import shutil
import zipfile
from pathlib import Path

from telethon import TelegramClient

from . import db
from .paths import ACCOUNTS_DIR

# Ключи JSON → параметры клиента. Значения по умолчанию — как у Telegram Desktop.
DEFAULT_PARAMS = {
    "app_id": 2040,
    "app_hash": "b18441a1ff607e10a989891a5462e627",
    "device": "Desktop",
    "sdk": "Windows 10",
    "app_version": "5.6.3 x64",
    "lang_pack": "en",
    "system_lang_pack": "en-US",
}
PARAM_KEYS = list(DEFAULT_PARAMS) + ["twoFA", "first_name", "phone", "proxy"]


# ---------- прокси ----------

def parse_proxy(raw):
    """Понимает:
        socks5://user:pass@host:port, http://host:port,
        host:port:user:pass, user:pass@host:port, host:port,
        список из JSON аккаунта [type, host, port, rdns, user, pass] или словарь.
    Возвращает словарь для Telethon или None.
    """
    if not raw:
        return None
    if isinstance(raw, str) and raw.strip()[:1] in "[{":
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError(f"не понимаю формат прокси: {raw}")
    if isinstance(raw, dict):
        if "addr" in raw or "host" in raw:
            return {
                "proxy_type": str(raw.get("proxy_type") or raw.get("type") or "socks5").lower(),
                "addr": raw.get("addr") or raw.get("host"),
                "port": int(raw["port"]),
                "username": raw.get("username") or raw.get("user") or None,
                "password": raw.get("password") or raw.get("pass") or None,
                "rdns": True,
            }
        return None
    if isinstance(raw, (list, tuple)):
        if len(raw) < 3:
            return None
        t = raw[0]
        t = {1: "socks4", 2: "socks5", 3: "http"}.get(t, t) if isinstance(t, int) else str(t).lower()
        return {"proxy_type": t, "addr": raw[1], "port": int(raw[2]), "rdns": True,
                "username": raw[4] if len(raw) > 4 else None, "password": raw[5] if len(raw) > 5 else None}

    s = str(raw).strip()
    ptype = "socks5"
    if "://" in s:
        scheme, s = s.split("://", 1)
        ptype = {"https": "http", "socks5h": "socks5"}.get(scheme.lower(), scheme.lower())
    user = pwd = None
    if "@" in s:
        cred, s = s.rsplit("@", 1)
        user, _, pwd = cred.partition(":")
    parts = s.split(":")
    if len(parts) == 4 and not user:
        host, port, user, pwd = parts
    elif len(parts) == 2:
        host, port = parts
    else:
        raise ValueError(f"не понимаю формат прокси: {raw}")
    if ptype not in ("socks5", "socks4", "http"):
        raise ValueError(f"неизвестный тип прокси: {ptype}")
    return {"proxy_type": ptype, "addr": host, "port": int(port),
            "username": user or None, "password": pwd or None, "rdns": True}


def proxy_label(raw) -> str:
    try:
        p = parse_proxy(raw)
    except ValueError:
        return "ошибка формата"
    return f"{p['proxy_type']}://{p['addr']}:{p['port']}" if p else ""


# ---------- импорт ----------

def _params_from_json(data: dict) -> dict:
    params = {k: data[k] for k in PARAM_KEYS if data.get(k) is not None}
    # встречаются варианты названий ключей
    for alt, key in (("api_id", "app_id"), ("api_hash", "app_hash"), ("device_model", "device"),
                     ("system_version", "sdk"), ("lang_code", "lang_pack"),
                     ("system_lang_code", "system_lang_pack"), ("twofa", "twoFA"), ("password", "twoFA")):
        if data.get(alt) is not None and key not in params:
            params[key] = data[alt]
    return params


def import_files(files: dict) -> dict:
    """files: {имя_файла: bytes}. Принимает .session, .json и .zip с ними."""
    flat = {}
    for name, content in files.items():
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    base = Path(info.filename).name
                    if base.lower().endswith((".session", ".json")) and not base.startswith("._"):
                        flat[base] = z.read(info)
        else:
            flat[Path(name).name] = content

    sessions = {Path(n).stem: c for n, c in flat.items() if n.lower().endswith(".session")}
    jsons = {Path(n).stem: c for n, c in flat.items() if n.lower().endswith(".json")}
    added, updated, errors = [], [], []
    for stem, content in sessions.items():
        if not content.startswith(b"SQLite format 3"):
            errors.append(f"{stem}: не похоже на сессию Telethon")
            continue
        params = {}
        if stem in jsons:
            try:
                params = _params_from_json(json.loads(jsons[stem].decode("utf-8", "ignore")))
            except ValueError as e:
                errors.append(f"{stem}.json: {e}")
        else:
            errors.append(f"{stem}: нет .json, будут параметры Telegram Desktop")
        folder = ACCOUNTS_DIR / stem
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stem}.session"
        path.write_bytes(content)
        if stem in jsons:
            (folder / f"{stem}.json").write_bytes(jsons[stem])
        proxy = params.pop("proxy", None)
        proxy_str = None
        if proxy:
            try:
                if parse_proxy(proxy):
                    proxy_str = proxy if isinstance(proxy, str) else json.dumps(proxy)
            except ValueError:
                pass
        existing = db.one("SELECT id FROM accounts WHERE name=?", (stem,))
        if existing:
            db.q("UPDATE accounts SET session_path=?, params=?, status='new', last_error=NULL WHERE id=?",
                 (str(path), json.dumps(params, ensure_ascii=False), existing["id"]))
            if proxy_str:
                db.q("UPDATE accounts SET proxy=? WHERE id=? AND (proxy IS NULL OR proxy='')",
                     (proxy_str, existing["id"]))
            updated.append(stem)
        else:
            db.q("INSERT INTO accounts(name, session_path, params, proxy, status, added_at) "
                 "VALUES(?,?,?,?, 'new', ?)",
                 (stem, str(path), json.dumps(params, ensure_ascii=False), proxy_str, db.now()))
            added.append(stem)
    for stem in jsons:
        if stem not in sessions:
            errors.append(f"{stem}.json: нет парного .session")
    return {"added": added, "updated": updated, "errors": errors}


def assign_proxies(lines: list, overwrite=False) -> dict:
    """Раздаёт прокси аккаунтам 1:1. Без overwrite — только тем, у кого прокси нет."""
    proxies, bad = [], []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        try:
            parse_proxy(ln)
            proxies.append(ln)
        except ValueError as e:
            bad.append(str(e))
    used = {r["proxy"] for r in db.all_("SELECT proxy FROM accounts WHERE proxy IS NOT NULL")}
    if overwrite:
        targets = db.all_("SELECT id FROM accounts ORDER BY id")
        free = proxies
    else:
        targets = db.all_("SELECT id FROM accounts WHERE proxy IS NULL OR proxy='' ORDER BY id")
        free = [p for p in proxies if p not in used]
    n = 0
    for acc, p in zip(targets, free):
        db.q("UPDATE accounts SET proxy=? WHERE id=?", (p, acc["id"]))
        n += 1
    return {"assigned": n, "valid": len(proxies), "bad": bad,
            "without_proxy": max(0, len(targets) - len(free))}


def scan_folder() -> dict:
    """Подхватывает .session (+ .json рядом), положенные в data/accounts вручную.

    Каждый новый аккаунт переносится в свою подпапку data/accounts/<имя>/, как при загрузке.
    """
    known = {r["name"] for r in db.all_("SELECT name FROM accounts")}
    found = {}
    for path in ACCOUNTS_DIR.rglob("*.session"):
        stem = path.stem
        if stem in known or stem in found or path.name.startswith("._"):
            continue
        found[stem] = path
    if not found:
        return {"added": [], "updated": [], "errors": []}

    files = {}
    for stem, path in found.items():
        files[path.name] = path.read_bytes()
        js = path.with_suffix(".json")
        if js.exists():
            files[js.name] = js.read_bytes()
    res = import_files(files)

    # Убираем исходники, если они лежали не там, куда их положил импорт
    for stem in res["added"]:
        src = found[stem]
        dest = ACCOUNTS_DIR / stem / f"{stem}.session"
        if src.resolve() != dest.resolve():
            for p in (src, src.with_suffix(".json"), Path(str(src) + "-journal")):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
    return res


def delete_account(acc_id: int):
    acc = db.one("SELECT * FROM accounts WHERE id=?", (acc_id,))
    if not acc:
        return
    db.q("DELETE FROM accounts WHERE id=?", (acc_id,))
    folder = Path(acc["session_path"]).parent
    if folder.parent == ACCOUNTS_DIR:
        shutil.rmtree(folder, ignore_errors=True)


def make_client(acc: dict) -> TelegramClient:
    p = dict(DEFAULT_PARAMS)
    p.update(json.loads(acc["params"] or "{}"))
    session = acc["session_path"][:-len(".session")]
    return TelegramClient(
        session,
        api_id=int(p["app_id"]),
        api_hash=p["app_hash"],
        device_model=str(p["device"]),
        system_version=str(p["sdk"]),
        app_version=str(p["app_version"]).strip(),
        lang_code=p["lang_pack"],
        system_lang_code=p["system_lang_pack"],
        proxy=parse_proxy(acc["proxy"]) if acc.get("proxy") else None,
        connection_retries=2,
        retry_delay=2,
        timeout=20,
        request_retries=2,
        flood_sleep_threshold=0,   # FloodWait обрабатываем сами
        receive_updates=False,     # обновления не нужны, только запросы
    )
