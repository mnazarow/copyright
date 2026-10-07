# -*- coding: utf-8 -*-
"""Пользователи, роли, вход через LDAP / Active Directory и журнал действий.

Роли:
  admin     — администратор: все, включая настройки, пользователей и журнал;
  corrector — корректор: загрузка, отметки ✓ ✕ ✎, пересборка, массовые действия, белый список и глоссарий;
  author    — автор: загрузка документов, просмотр и скачивание отчетов, удаление своих заданий.

Встроенный администратор «admin» входит паролем из KORREKTOR_ADMIN_PASSWORD (korrektor.env) и существует всегда.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets

from . import core

ROLES = {"admin": "администратор", "corrector": "корректор", "author": "автор"}
RANK = {"author": 1, "corrector": 2, "admin": 3}
BUILTIN = "admin"
LOGIN_RE = re.compile(r"^[\w.@-]{2,64}$")
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']{1,64}@[^@\s,;<>\"']{1,190}\.[^@\s,;<>\"'.]{2,24}$")
TRUSTED = {x.strip() for x in os.environ.get("KORREKTOR_TRUSTED_PROXIES", "127.0.0.1,::1").split(",") if x.strip()}


def session_stamp(u):
    """Отпечаток учетной записи в cookie: меняется при смене пароля и при пересоздании пользователя,
    поэтому старые сеансы перестают действовать."""
    if u.get("source") == "env":
        base = "env|" + core.ADMIN_PASSWORD
        from . import totp
        t = totp.get(BUILTIN)
        sec = t["secret"] if t else ""
    else:
        base = f"{u.get('id')}|{u.get('created')}|{u.get('pw') or ''}"
        sec = u.get("totp_secret") or ""
    if sec:                                            # подключение или сброс второго фактора завершает старые сеансы
        base += "|totp|" + sec
    return hashlib.sha256((core.SECRET + base).encode()).hexdigest()[:12]


# ------------------------------------------------------------------ база
def init():
    with core.db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT UNIQUE COLLATE NOCASE, name TEXT DEFAULT '',
            role TEXT DEFAULT 'author', pw TEXT DEFAULT '', source TEXT DEFAULT 'local', active INTEGER DEFAULT 1,
            email TEXT DEFAULT '', department TEXT DEFAULT '', created TEXT, last_login TEXT)""")
        if "oidc_sub" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
            c.execute("ALTER TABLE users ADD COLUMN oidc_sub TEXT DEFAULT ''")
        c.execute("""CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT, ip TEXT, action TEXT, target TEXT, detail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)")
    from . import totp
    totp.init()


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def hash_pw(pw, salt=None, it=200_000):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), it).hex()
    return f"pbkdf2_sha256${it}${salt}${h}"


def verify_pw(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
    except (ValueError, AttributeError):
        return False
    good = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(), int(it)).hex()
    return hmac.compare_digest(good.encode(), h.encode())


def pw_problem(pw):
    """Требования к паролю: от 8 символов, буквы и цифры."""
    if len(pw or "") < 8:
        return "Пароль — не короче 8 символов"
    if not re.search(r"\d", pw) or not re.search(r"[^\W\d_]", pw):
        return "Пароль должен содержать буквы и цифры"
    return ""


def get(login):
    if not login:
        return None
    if login.lower() == BUILTIN:
        return {"id": 0, "login": BUILTIN, "name": "Администратор", "role": "admin", "source": "env", "active": 1,
                "email": "", "department": ""}
    with core.db() as c:
        r = c.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
    return dict(r) if r else None


def all_users():
    with core.db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM users ORDER BY login")]


def save(login, name="", role="author", password=None, source="local", active=1, email="", department=""):
    if not LOGIN_RE.match(login or "") or login.lower() == BUILTIN:
        raise ValueError("Логин: 2–64 символа (буквы, цифры, точка, @, дефис); «admin» зарезервирован")
    if role not in ROLES:
        raise ValueError("Неизвестная роль")
    email = (email or "").strip()
    if email and not EMAIL_RE.match(email):
        raise ValueError("Почта: один адрес вида имя@домен")
    u = get(login)
    with core.db() as c:
        if u:
            c.execute("UPDATE users SET name=?, role=?, active=?, email=?, department=? WHERE login=?",
                      (name, role, int(bool(active)), email, department, login))
        else:
            c.execute("INSERT INTO users(login,name,role,source,active,email,department,created) VALUES(?,?,?,?,?,?,?,?)",
                      (login, name, role, source, int(bool(active)), email, department, now()))
        if password:
            c.execute("UPDATE users SET pw=? WHERE login=?", (hash_pw(password), login))
    return get(login)


def set_oidc_sub(login, sub):
    with core.db() as c:
        c.execute("UPDATE users SET oidc_sub=? WHERE login=?", (sub, login))


def by_oidc_sub(sub):
    with core.db() as c:
        r = c.execute("SELECT * FROM users WHERE oidc_sub=? AND oidc_sub<>''", (sub,)).fetchone()
    return dict(r) if r else None


def delete(login):
    with core.db() as c:
        c.execute("DELETE FROM users WHERE login=? AND login<>?", (login, BUILTIN))


def touch(login):
    if login.lower() != BUILTIN:
        with core.db() as c:
            c.execute("UPDATE users SET last_login=? WHERE login=?", (now(), login))


# ------------------------------------------------------------------ вход
def authenticate(login, password, st):
    """→ (пользователь, None) или (None, причина)."""
    login = (login or BUILTIN).strip()
    if login.lower() == BUILTIN:
        return (get(BUILTIN), None) if core.check_password(password) else (None, "bad")
    if not LOGIN_RE.match(login):
        return None, "bad"
    u = get(login)
    if u and u["source"] == "local":
        if not (u["pw"] and verify_pw(password, u["pw"])):
            return None, "bad"
        return (u, None) if u["active"] else (None, "disabled")      # «отключен» — только после верного пароля
    if st.get("ldap_enabled"):
        ok, info = ldap_bind(login, password, st)
        if not ok:
            return None, "bad" if info != "unavailable" else "ldap"
        if u and not u["active"]:
            return None, "disabled"
        if not u:
            email = info.get("email", "")
            try:
                u = save(login, info.get("name", ""), st.get("ldap_default_role") or "author", source="ldap",
                         email=email if EMAIL_RE.match(email or "") else "", department=info.get("department", ""))
            except Exception:  # noqa — параллельный первый вход: запись уже создана
                u = get(login)
        return (u, None) if u else (None, "bad")
    return None, "bad"


def ldap_bind(login, password, st):
    """Проверка пароля простым подключением (bind) к LDAP / Active Directory."""
    if not password:
        return False, "empty"
    try:
        import ldap3
    except ImportError:
        core.log.error("LDAP включен, но модуль ldap3 не установлен (pip install ldap3)")
        return False, "unavailable"
    if not LOGIN_RE.match(login or ""):
        return False, "bad"
    safe = login
    dn = (st.get("ldap_bind_template") or "{login}").replace("{login}", safe)
    try:
        import ssl
        url = st.get("ldap_url") or ""
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED if st.get("ldap_tls_verify", True) else ssl.CERT_NONE,
                        ca_certs_file=st.get("ldap_ca_file") or None)
        server = ldap3.Server(url, use_ssl=url.startswith("ldaps://"), tls=tls, get_info=ldap3.NONE, connect_timeout=8)
        conn = ldap3.Connection(server, user=dn, password=password, auto_bind=False, receive_timeout=10)
        if st.get("ldap_url", "").startswith("ldap://") and st.get("ldap_starttls"):
            conn.start_tls()
        if not conn.bind():
            return False, "bad"
        info = {}
        base = st.get("ldap_search_base")
        if base:
            flt = (st.get("ldap_search_filter") or "(sAMAccountName={login})").replace("{login}", ldap3.utils.conv.escape_filter_chars(safe))
            conn.search(base, flt, attributes=["displayName", "mail", "department", "memberOf"])
            if conn.entries:
                e = conn.entries[0]
                info = {"name": str(e.displayName) if "displayName" in e else "",
                        "email": str(e.mail) if "mail" in e else "",
                        "department": str(e.department) if "department" in e else ""}
        conn.unbind()
        return True, info
    except Exception as e:  # noqa
        core.log.warning("LDAP недоступен: %s", e)
        return False, "unavailable"


# ------------------------------------------------------------------ права
def can(user, action):
    """Действия: view, upload, review (✓ ✕ ✎, пересборка), manage (белый список, глоссарий, массовые действия),
    admin (настройки, пользователи, журнал)."""
    need = {"view": "author", "upload": "author", "review": "corrector", "manage": "corrector", "admin": "admin"}[action]
    return bool(user) and RANK.get(user.get("role"), 0) >= RANK[need]


class Scope:
    """Какие задания видит пользователь: свои + (если задан отдел) задания своего отдела."""
    __slots__ = ("login", "dept")

    def __init__(self, login, dept=None):
        self.login, self.dept = login or "", (dept or "").strip().lower() or None

    def ok(self, owner, department=""):
        if owner and owner == self.login:
            return True
        return bool(self.dept) and (department or "").strip().lower() == self.dept

    def __repr__(self):
        return f"Scope({self.login!r}, {self.dept!r})"


def own_only(user, st=None):
    """Ограничение видимости заданий (Scope) или None — пользователь видит все.
    * «Авторы видят только свои документы» — автор видит только загруженное им;
    * «Разделение по отделам» — авторы и корректоры видят свои задания и задания своего отдела
      (без отдела в учетной записи — только свои). Администратор видит все."""
    st = st or core.load_settings()
    if not user or user.get("role") == "admin" or user.get("source") == "token":
        return None                                    # интеграции по токену API видят все задания
    if user.get("role") == "author" and st.get("authors_own_only"):
        return Scope(user.get("login", ""))
    if st.get("department_scope"):
        return Scope(user.get("login", ""), user.get("department") or "")
    return None


JOB_PATH = re.compile(r"^/(?:api/)?jobs?/([^/]+)")
ID_RE = re.compile(r"[0-9]{1,12}")


def job_visible(user, jid, st=None):
    scope = own_only(user, st)
    if scope is None:
        return True
    job = core.get_job(jid)
    return bool(job) and scope.ok(job.get("owner"), job.get("department"))


ADMIN_PATHS = ("/settings", "/admin")
REVIEW_PATHS = (re.compile(r"^/api/jobs/[^/]+/(findings|rebuild)"), re.compile(r"^/api/analytics/(bulk|whitelist|glossary)"))


def path_allowed(user, method, path):
    if path.startswith(ADMIN_PATHS):
        return can(user, "admin")
    if method not in ("GET", "HEAD", "OPTIONS") and any(r.match(path) for r in REVIEW_PATHS):
        return can(user, "review")
    return can(user, "view")


# ------------------------------------------------------------------ журнал
def client_ip(request):
    """Адрес клиента. X-Real-IP учитывается только от локального обратного прокси (nginx на этом же сервере),
    иначе заголовок можно подделать и обойти блокировку перебора паролей."""
    host = request.client.host if request.client else ""
    if host in TRUSTED:                                # доверенный обратный прокси (nginx, Traefik)
        fwd = request.headers.get("x-real-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
        return fwd or host
    return host


def audit(request, action, target="", detail=""):
    try:
        u = getattr(request.state, "user", None) if request is not None else None
        ip = client_ip(request) if request is not None else ""
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)[:2000]
        with core.db() as c:
            c.execute("INSERT INTO audit(ts,user,ip,action,target,detail) VALUES(?,?,?,?,?,?)",
                      (now(), (u or {}).get("login", "") if isinstance(u, dict) else str(u or ""), ip, action,
                       str(target)[:300], str(detail)[:2000]))
    except Exception:  # noqa — журнал не должен ломать основное действие
        core.log.exception("журнал действий")


AUDIT_NAMES = {
    "login": "вход", "login_failed": "неудачный вход", "logout": "выход", "upload": "загрузка документа",
    "job_delete": "удаление задания", "job_restart": "повторная проверка", "finding": "изменение замечания",
    "rebuild": "пересборка отчета", "bulk": "массовая отметка", "whitelist": "белый список",
    "glossary": "глоссарий", "settings": "изменение настроек", "settings_import": "загрузка настроек",
    "settings_reset": "сброс настроек", "prompt": "мастер-промпт", "token": "новый токен API",
    "user_save": "пользователь сохранен", "user_delete": "пользователь удален", "password": "смена пароля",
    "download": "скачивание", "backup": "резервная копия", "restore": "восстановление из копии",
    "backup_upload": "загрузка копии", "remind": "напоминания", "totp": "двухфакторный вход",
    "mgmt_report": "отчет для руководства", "products": "справочник продукции",
    "profile": "профиль изменен",
}


def audit_rows(q="", user="", action="", limit=500, offset=0):
    sql, args = "SELECT * FROM audit WHERE 1=1", []
    if user:
        sql += " AND user=?"
        args.append(user)
    if action:
        sql += " AND action=?"
        args.append(action)
    if q:
        sql += " AND (target LIKE ? OR detail LIKE ? OR ip LIKE ?)"
        args += [f"%{q}%"] * 3
    with core.db() as c:
        total = c.execute(sql.replace("SELECT *", "SELECT COUNT(*)"), args).fetchone()[0]
        rows = [dict(r) for r in c.execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?", args + [limit, offset])]
    return total, rows


def cleanup_audit(days=365):
    lim = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
    with core.db() as c:
        c.execute("DELETE FROM audit WHERE ts < ?", (lim,))
