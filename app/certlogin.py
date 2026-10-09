# -*- coding: utf-8 -*-
"""Вход по сертификату (смарт-карта, токен Рутокен/JaCarta, сертификат в браузере).

Сертификат проверяет nginx (ssl_verify_client) и передает Корректору результат проверки и имя владельца (Subject DN)
в заголовках. Корректор доверяет этим заголовкам, только если в запросе есть секрет прокси (заголовок
X-Korrektor-Proxy-Secret = настройка «Секрет прокси для входа по сертификату», не короче 16 знаков): его знает только
nginx, поэтому подставить заголовки сертификата из браузера нельзя. Адрес отправителя не проверяется: служба
запускается с --proxy-headers, и uvicorn подменяет его адресом клиента из X-Forwarded-For.
Пользователь находится по почте из сертификата (emailAddress), по CN или по UID — как задано в настройках.
"""
import hmac
import re

from . import users


def parse_dn(dn):
    """Subject DN в формате RFC 2253 (как его отдает nginx: $ssl_client_s_dn) → {атрибут: значение}.
    Поддерживаются экранирования «\\,» и «\\XX» (байты UTF-8 русских имен)."""
    out, cur, key, i, buf = {}, "", None, 0, bytearray()

    def flush_buf():
        nonlocal cur, buf
        if buf:
            cur += buf.decode("utf-8", "replace")
            buf = bytearray()
    s = dn or ""
    if s.startswith("/"):                           # nginx < 1.11.6: «/C=RU/O=Компания/CN=Иванов»
        for part in s.strip("/").split("/"):
            if "=" in part:
                k, v = part.split("=", 1)
                out.setdefault(k.strip().upper(), v.strip())
        if "E" in out and "EMAILADDRESS" not in out:
            out["EMAILADDRESS"] = out["E"]
        return out
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1:i + 3]
            if re.fullmatch(r"[0-9A-Fa-f]{2}", nxt):
                buf.append(int(nxt, 16))
                i += 3
                continue
            flush_buf()
            cur += s[i + 1]
            i += 2
            continue
        flush_buf()
        if ch == "=" and key is None:
            key, cur = cur.strip(), ""
        elif ch in ",+" and key is not None:
            out.setdefault(key.upper(), cur.strip())
            key, cur = None, ""
        else:
            cur += ch
        i += 1
    flush_buf()
    if key is not None:
        out.setdefault(key.upper(), cur.strip())
    if "E" in out and "EMAILADDRESS" not in out:
        out["EMAILADDRESS"] = out["E"]
    return out


def identify(request, st):
    """→ (пользователь, None) или (None, причина)."""
    secret = st.get("cert_proxy_secret") or ""
    if len(secret) < 16:
        return None, "не задан секрет прокси для входа по сертификату (не короче 16 знаков)"
    if not hmac.compare_digest(request.headers.get("x-korrektor-proxy-secret", "").encode(), secret.encode()):
        return None, "nginx не передал секрет прокси — проверьте настройку proxy_set_header X-Korrektor-Proxy-Secret"
    verify = request.headers.get(st.get("cert_header_verify") or "X-SSL-Client-Verify", "")
    if verify != "SUCCESS":
        return None, f"сертификат не предъявлен или не прошел проверку ({verify or 'нет сертификата'})"
    dn = parse_dn(request.headers.get(st.get("cert_header_dn") or "X-SSL-Client-S-DN", ""))
    how = st.get("cert_map") or "email"
    if how == "email":
        email = (dn.get("EMAILADDRESS") or "").strip().lower()
        if not email:
            return None, "в сертификате нет адреса почты (emailAddress)"
        found = [u for u in users.all_users() if (u.get("email") or "").strip().lower() == email]
        if len(found) > 1:
            return None, f"почта {email} указана у нескольких учетных записей"
        u = found[0] if found else None
        login = email.split("@")[0]
    else:
        login = (dn.get("UID") if how == "uid" else dn.get("CN")) or ""
        u = users.get(login) if users.LOGIN_RE.match(login or "") else None
    if not u and st.get("cert_autocreate"):
        if not users.LOGIN_RE.match(login or "") or users.get(login):
            return None, f"не удалось создать учетную запись: логин «{login}» не подходит или занят"
        u = users.save(login, dn.get("CN", "")[:120], st.get("cert_default_role") or "author", None, source="cert",
                       email=dn.get("EMAILADDRESS", "") if users.EMAIL_RE.match(dn.get("EMAILADDRESS", "")) else "",
                       department=dn.get("OU", "")[:120])
    if not u:
        return None, "учетная запись для этого сертификата не найдена — обратитесь к администратору"
    if not u.get("active"):
        return None, "учетная запись отключена"
    return u, None
