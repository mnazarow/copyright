# -*- coding: utf-8 -*-
"""Вход через корпоративный сервер учетных записей по OpenID Connect: Keycloak, ADFS, Microsoft Entra ID, Яндекс 360,
Authentik, Blitz Identity и др. Поток «код авторизации» с PKCE.

Подпись id_token не проверяется криптографически: токен получен сервером напрямую от сервера учетных записей
по HTTPS (обмен кода на токен с секретом клиента), что допускается спецификацией OpenID Connect Core, п. 3.1.3.7.
Проверяются издатель (iss), получатель (aud), срок (exp) и одноразовое значение (nonce).
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

from . import core, users

_discovery = {}


def enabled(st):
    return bool(st.get("oidc_enabled") and st.get("oidc_issuer") and st.get("oidc_client_id"))


def _secure(url):
    """Без проверки подписи id_token доверие держится на HTTPS: обычный http — только для локальной проверки."""
    u = urlparse(url or "")
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"))


def discover(st):
    import httpx
    iss = st["oidc_issuer"].rstrip("/")
    if not _secure(iss):
        raise ValueError("адрес издателя OpenID Connect должен начинаться с https://")
    cached = _discovery.get(iss)
    if cached and cached[0] > time.time():
        return cached[1]
    r = httpx.get(iss + "/.well-known/openid-configuration", timeout=10)
    r.raise_for_status()
    conf = r.json()
    for k in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if conf.get(k) and not _secure(conf[k]):
            raise ValueError(f"{k} сервера учетных записей должен быть по https://")
    _discovery[iss] = (time.time() + 3600, conf)
    return conf


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _sign(payload):
    raw = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(core.SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + sig


def _unsign(value):
    try:
        raw, sig = value.rsplit(".", 1)
    except (ValueError, AttributeError):
        return None
    good = hmac.new(core.SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig.encode(), good.encode()):
        return None
    data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    return data if data.get("exp", 0) > time.time() else None


def redirect_uri(st, request):
    base = (st.get("service_url") or str(request.base_url)).rstrip("/")
    return base + "/login/oidc/callback"


def start(st, request, nxt):
    """→ (адрес сервера учетных записей, значение cookie с состоянием входа)."""
    conf = discover(st)
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    params = {"response_type": "code", "client_id": st["oidc_client_id"], "redirect_uri": redirect_uri(st, request),
              "scope": st.get("oidc_scopes") or "openid profile email", "state": state, "nonce": nonce,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    url = conf["authorization_endpoint"] + ("&" if "?" in conf["authorization_endpoint"] else "?") + urlencode(params)
    cookie = _sign({"state": state, "nonce": nonce, "verifier": verifier, "next": nxt, "exp": time.time() + 600})
    return url, cookie


def _claims_from_jwt(token):
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise ValueError("сервер учетных записей вернул неверный id_token")


def finish(st, request, code, state, cookie):
    """Обмен кода на токен, проверка, создание или обновление пользователя. → (пользователь, адрес возврата)."""
    import httpx
    saved = _unsign(cookie or "")
    if not saved or not hmac.compare_digest(saved["state"].encode(), (state or "").encode()):
        raise ValueError("сеанс входа устарел или подменен — начните вход заново")
    conf = discover(st)
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(st, request),
            "client_id": st["oidc_client_id"], "code_verifier": saved["verifier"]}
    if st.get("oidc_client_secret"):
        data["client_secret"] = st["oidc_client_secret"]
    r = httpx.post(conf["token_endpoint"], data=data, timeout=15, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise ValueError(f"сервер учетных записей отклонил код ({r.status_code}): {r.text[:200]}")
    tok = r.json()
    claims = _claims_from_jwt(tok.get("id_token") or "")
    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud]
    if claims.get("iss", "").rstrip("/") != (conf.get("issuer") or st["oidc_issuer"]).rstrip("/"):
        raise ValueError("id_token выдан другим сервером (iss)")
    if st["oidc_client_id"] not in aud:
        raise ValueError("id_token выдан другому приложению (aud)")
    if len(aud) > 1 and claims.get("azp") != st["oidc_client_id"]:
        raise ValueError("id_token выдан другому приложению (azp)")
    if not claims.get("sub"):
        raise ValueError("в id_token нет идентификатора пользователя (sub)")
    if float(claims.get("exp", 0)) < time.time() - 60:
        raise ValueError("id_token просрочен")
    if claims.get("nonce") != saved["nonce"]:
        raise ValueError("неверный nonce — возможна попытка повторного входа")
    if tok.get("access_token") and conf.get("userinfo_endpoint"):
        try:
            ui = httpx.get(conf["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=10)
            if ui.status_code == 200:
                claims = {**ui.json(), **claims}
        except Exception:  # noqa — сведения профиля необязательны
            pass
    claims["_iss_sub"] = f"{claims.get('iss', '').rstrip('/')}|{claims['sub']}"
    return provision(st, claims), saved.get("next") or "/"


def _values(v):
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else []


def role_from_claims(st, claims):
    claim = st.get("oidc_role_claim") or "groups"
    have = set()
    for part in claim.split(","):
        node = claims
        for key in part.strip().split("."):                 # вложенные: realm_access.roles
            node = node.get(key, {}) if isinstance(node, dict) else {}
        have |= {x.strip("/").lower() for x in _values(node)}
    admins = {x.strip().strip("/").lower() for x in (st.get("oidc_admin_values") or "").split(",") if x.strip()}
    correctors = {x.strip().strip("/").lower() for x in (st.get("oidc_corrector_values") or "").split(",") if x.strip()}
    managers = {x.strip().strip("/").lower() for x in (st.get("oidc_manager_values") or "").split(",") if x.strip()}
    if admins & have:
        return "admin"
    if managers & have:
        return "manager"
    if correctors & have:
        return "corrector"
    return None


def provision(st, claims):
    """Учетная запись по устойчивому идентификатору (iss|sub). Логин из токена используется только при первом входе
    и не может «подхватить» чужую учетную запись: локальные и LDAP-записи не присоединяются, логин не переименовывается."""
    key = claims.get("_iss_sub") or ""
    raw = str(claims.get(st.get("oidc_login_claim") or "preferred_username") or claims.get("email") or claims.get("sub") or "")
    mapped = role_from_claims(st, claims)
    mapping_on = bool((st.get("oidc_admin_values") or "").strip() or (st.get("oidc_corrector_values") or "").strip()
                      or (st.get("oidc_manager_values") or "").strip())
    u = users.by_oidc_sub(key) if key else None
    if not u:
        login = raw[:64]
        if not users.LOGIN_RE.match(login) or login.lower() == users.BUILTIN:
            raise ValueError(f"логин «{raw[:40]}» из сервера учетных записей не подходит (буквы, цифры, точка, @, дефис)")
        other = users.get(login)
        if other:
            raise ValueError(f"логин «{login}» уже занят учетной записью другого типа ({other['source']}); "
                             "администратор должен удалить ее или переименовать")
    else:
        login = u["login"]
        if not u["active"]:
            raise ValueError("учетная запись отключена администратором")
    email = claims.get("email") or ""
    email = email if users.EMAIL_RE.match(email) else (u or {}).get("email", "")
    if mapping_on:
        role = mapped or (st.get("oidc_default_role") or "author")       # группы на сервере — источник истины
    else:
        role = u["role"] if u else (st.get("oidc_default_role") or "author")
    name = claims.get("name") or " ".join(x for x in (claims.get("family_name"), claims.get("given_name")) if x) or (u or {}).get("name", "")
    dept = claims.get("department") or (u or {}).get("department", "")
    try:
        saved = users.save(login, str(name)[:120], role, None, source="oidc", active=1, email=email,
                           department=str(dept)[:120])
    except ValueError:
        raise
    except Exception:  # noqa — параллельный первый вход
        saved = users.get(login)
    if key and saved and not u:
        users.set_oidc_sub(login, key)
    return saved
