# -*- coding: utf-8 -*-
"""Двухфакторный вход: одноразовые коды TOTP (RFC 6238) — Яндекс Ключ, Google Authenticator, Microsoft Authenticator,
FreeOTP, Bitwarden, KeePassXC и т. п. Плюс 8 резервных кодов на случай потери телефона.

Хранение: у пользователей — в таблице users (totp_secret, totp_last, totp_recovery); у встроенного администратора —
в файле .admin_totp.json в папке данных (права 600).
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import sys
import time
from urllib.parse import quote

from . import core

STEP, DIGITS = 30, 6
ADMIN_FILE = os.path.join(core.DATA, ".admin_totp.json")


def init():
    with core.db() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)")}
        for col, ddl in (("totp_secret", "TEXT DEFAULT ''"), ("totp_last", "INTEGER DEFAULT 0"),
                         ("totp_recovery", "TEXT DEFAULT ''")):
            if col not in cols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 15
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS).zfill(DIGITS)


def check_code(secret, code, last=0, window=1, now=None):
    """→ номер шага времени, если код верен и не использовался (защита от повтора), иначе None."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    t = int((now or time.time()) // STEP)
    for step in range(t - window, t + window + 1):
        if step > int(last or 0) and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def uri(secret, login):
    issuer = "Корректор"
    return (f"otpauth://totp/{quote(issuer)}:{quote(login)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def _hash(code):
    return hashlib.sha256(("korrektor-recovery|" + code.replace("-", "").lower()).encode()).hexdigest()


def new_recovery():
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(8)]
    return codes, [_hash(c) for c in codes]


# ------------------------------------------------------------------ хранение
def _builtin(login):
    from .users import BUILTIN
    return (login or "").lower() == BUILTIN


def get(login):
    """→ {secret, last, recovery: [хэши]} или None, если двухфакторный вход не включен."""
    if _builtin(login):
        try:
            d = json.load(open(ADMIN_FILE, encoding="utf-8"))
            return d if d.get("secret") else None
        except (OSError, ValueError):
            return None
    with core.db() as c:
        r = c.execute("SELECT totp_secret, totp_last, totp_recovery FROM users WHERE login=?", (login,)).fetchone()
    if not r or not r[0]:
        return None
    return {"secret": r[0], "last": r[1] or 0, "recovery": json.loads(r[2] or "[]")}


def enabled(login):
    return get(login) is not None


def _save(login, secret, last, recovery):
    if _builtin(login):
        if not secret:
            if os.path.exists(ADMIN_FILE):
                os.remove(ADMIN_FILE)
            return
        old = os.umask(0o077)
        try:
            core.write_json_atomic(ADMIN_FILE, {"secret": secret, "last": last, "recovery": recovery})
        finally:
            os.umask(old)
        return
    with core.db() as c:
        c.execute("UPDATE users SET totp_secret=?, totp_last=?, totp_recovery=? WHERE login=?",
                  (secret or "", int(last or 0), json.dumps(recovery or []), login))


def enable(login, secret, step):
    codes, hashes = new_recovery()
    _save(login, secret, step, hashes)
    return codes


def disable(login):
    _save(login, "", 0, [])


_lock = __import__("threading").Lock()


def verify(login, code):
    """Проверка кода при входе: TOTP или резервный код (резервный код одноразовый). → 'totp' | 'recovery' | None."""
    with _lock:
        d = get(login)
        if not d:
            return None
        step = check_code(d["secret"], code, d.get("last", 0))
        if step:
            _save(login, d["secret"], step, d.get("recovery", []))
            return "totp"
        h = _hash(str(code or "").strip())
        if h in (d.get("recovery") or []):
            rest = [x for x in d["recovery"] if x != h]
            _save(login, d["secret"], d.get("last", 0), rest)
            return "recovery"
        return None


def required(user, st):
    """Обязателен ли второй фактор для пользователя по настройке «Двухфакторный вход обязателен для»."""
    if not user or user.get("source") in ("oidc", "token"):
        return False                                   # при едином входе второй фактор — на сервере учетных записей
    need = st.get("totp_required") or "none"
    role = user.get("role")
    return (need == "all" or (need == "correctors" and role in ("corrector", "admin"))
            or (need == "admins" and role == "admin"))


def _cli():
    """python -m app.totp off ЛОГИН — отключить двухфакторный вход (потерян телефон и резервные коды)."""
    if len(sys.argv) != 3 or sys.argv[1] != "off":
        print("использование: python -m app.totp off ЛОГИН")
        sys.exit(2)
    disable(sys.argv[2])
    print(f"Двухфакторный вход для «{sys.argv[2]}» отключен")


if __name__ == "__main__":
    _cli()
