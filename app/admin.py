# -*- coding: utf-8 -*-
"""Страницы администратора: пользователи и роли, журнал действий; личный кабинет (смена пароля)."""
import csv
import io

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from . import core, notify, users
from .pipeline.exports import safe

router = APIRouter()
_tpl = None
_ctx = None


def setup(app, tpl, ctx):
    global _tpl, _ctx
    _tpl, _ctx = tpl, ctx
    app.include_router(router)


def me(request):
    return getattr(request.state, "user", None) or {}


# ------------------------------------------------------------------ пользователи
@router.get("/admin/users", response_class=HTMLResponse)
def users_page(request: Request, saved: str = "", error: str = ""):
    from . import totp
    secret_cols = ("pw", "totp_secret", "totp_recovery", "totp_last", "oidc_sub")
    rows = [dict({k: v for k, v in u.items() if k not in secret_cols}, totp=bool(u.get("totp_secret")))
            for u in users.all_users()]                                 # хэши паролей и ключи 2FA не попадают в страницу
    builtin_totp = totp.enabled(users.BUILTIN)
    with core.db() as c:
        counts = {r[0]: r[1] for r in c.execute("SELECT owner, COUNT(*) FROM jobs GROUP BY owner")}
    return _tpl.TemplateResponse(request, "admin_users.html", _ctx(request, "admin", rows=rows, counts=counts,
                                                                   saved=saved, error=error, builtin_totp=builtin_totp))


@router.post("/admin/users/reset-2fa")
def users_reset_2fa(request: Request, login: str = Form(...)):
    """Сотрудник потерял телефон и резервные коды: администратор отключает его второй фактор."""
    from . import totp
    if not users.get(login):
        raise HTTPException(404)
    totp.disable(login)
    users.audit(request, "totp", login, "отключен администратором")
    return RedirectResponse("/admin/users?saved=" + login, status_code=303)


@router.post("/admin/users/save")
def users_save(request: Request, login: str = Form(...), name: str = Form(""), role: str = Form("author"),
               password: str = Form(""), email: str = Form(""), department: str = Form(""), active: str = Form("")):
    login = login.strip()
    if password:
        prob = users.pw_problem(password)
        if prob:
            return RedirectResponse("/admin/users?error=" + prob, status_code=303)
    try:
        existing = users.get(login)
        if existing and existing["source"] != "local" and password:
            raise ValueError("Пароль пользователя LDAP хранится в домене, его меняют там")
        if not existing and not password:
            raise ValueError("Для нового пользователя задайте пароль")
        if existing and existing["login"].lower() == me(request).get("login", "").lower() and (role != "admin" or not active):
            raise ValueError("Нельзя снять с себя роль администратора или отключить свою учетную запись")
        users.save(login, name.strip()[:120], role, password or None, active=1 if active else 0,
                   email=email.strip()[:200], department=department.strip()[:120],
                   source=existing["source"] if existing else "local")
    except ValueError as e:
        return RedirectResponse("/admin/users?error=" + str(e), status_code=303)
    users.audit(request, "user_save", login, {"role": role, "active": bool(active), "password": bool(password)})
    return RedirectResponse("/admin/users?saved=" + login, status_code=303)


@router.post("/admin/users/delete")
def users_delete(request: Request, login: str = Form(...)):
    if login.lower() == me(request).get("login", "").lower():
        return RedirectResponse("/admin/users?error=Нельзя удалить самого себя", status_code=303)
    users.delete(login)
    users.audit(request, "user_delete", login)
    return RedirectResponse("/admin/users?saved=удален", status_code=303)


# ------------------------------------------------------------------ журнал
@router.get("/admin/audit", response_class=HTMLResponse)
def audit_page(request: Request, q: str = "", user: str = "", action: str = "", page: int = 1):
    page = max(1, page)
    total, rows = users.audit_rows(q, user, action, 100, (page - 1) * 100)
    with core.db() as c:
        who = [r[0] for r in c.execute("SELECT DISTINCT user FROM audit WHERE user<>'' ORDER BY user")]
    return _tpl.TemplateResponse(request, "admin_audit.html", _ctx(
        request, "admin", rows=rows, total=total, q=q, fuser=user, faction=action, pageno=page,
        pages=max(1, (total + 99) // 100), who=who, names=users.AUDIT_NAMES))


@router.get("/admin/audit/export")
def audit_export(q: str = "", user: str = "", action: str = ""):
    _, rows = users.audit_rows(q, user, action, 100000, 0)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Время", "Пользователь", "Адрес", "Действие", "Объект", "Подробности"])
    for r in rows:
        w.writerow([safe(x) for x in (r["ts"], r["user"], r["ip"], users.AUDIT_NAMES.get(r["action"], r["action"]),
                                      r["target"], r["detail"])])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=korrektor_audit.csv"})


# ------------------------------------------------------------------ личный кабинет
@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, saved: int = 0, error: str = ""):
    u = me(request)
    with core.db() as c:
        mine = c.execute("SELECT COUNT(*) FROM jobs WHERE owner=?", (u.get("login", ""),)).fetchone()[0]
    from . import totp
    t = totp.get(u.get("login", "")) if u.get("source") in ("local", "ldap", "env") else None
    return _tpl.TemplateResponse(request, "account.html", _ctx(request, "account", saved=saved, error=error, mine=mine,
                                                               totp_on=bool(t), totp_left=len((t or {}).get("recovery") or []),
                                                               totp_req=totp.required(u, core.load_settings()),
                                                               totp_ok=u.get("source") in ("local", "ldap", "env")))


@router.get("/account/2fa", response_class=HTMLResponse)
def account_2fa_page(request: Request, error: str = ""):
    import time
    from . import oidc, totp
    u = me(request)
    if u.get("source") not in ("local", "ldap", "env") or totp.enabled(u["login"]):
        return RedirectResponse("/account", status_code=303)
    new = oidc._unsign(request.cookies.get("korrektor_2fa_new", "")) or {}
    secret = new.get("s") if new.get("u") == u["login"] and new.get("t") == "totp" else None
    fresh = not secret
    secret = secret or totp.new_secret()
    r = _tpl.TemplateResponse(request, "account_2fa.html", _ctx(request, "account", secret=secret, error=error,
                                                                uri=totp.uri(secret, u["login"]), action="/account/2fa",
                                                                next="/account"))
    if fresh:
        r.set_cookie("korrektor_2fa_new", oidc._sign({"t": "totp", "s": secret, "u": u["login"], "exp": time.time() + 600}),
                     httponly=True, samesite="lax", max_age=600,
                     secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@router.post("/account/2fa", response_class=HTMLResponse)
def account_2fa_enable(request: Request, code: str = Form("")):
    from . import oidc, totp
    u = me(request)
    if u.get("source") not in ("local", "ldap", "env") or totp.enabled(u["login"]):
        return RedirectResponse("/account", status_code=303)
    new = oidc._unsign(request.cookies.get("korrektor_2fa_new", "")) or {}
    if new.get("u") != u["login"] or not new.get("s") or new.get("t") != "totp":
        return RedirectResponse("/account/2fa", status_code=303)
    step = totp.check_code(new["s"], code)
    if not step:
        return RedirectResponse("/account/2fa?error=1", status_code=303)
    codes = totp.enable(u["login"], new["s"], step)
    users.audit(request, "totp", u["login"], "подключен в профиле")
    r = _tpl.TemplateResponse(request, "account_2fa.html", _ctx(request, "account", codes=codes))
    r.delete_cookie("korrektor_2fa_new")
    _reissue(request, r, u["login"])                   # прочие сеансы завершены, текущий продолжается
    return r


@router.post("/account/2fa/disable")
def account_2fa_disable(request: Request, code: str = Form("")):
    from . import totp
    u = me(request)
    if totp.required(u, core.load_settings()):
        return RedirectResponse("/account?error=Двухфакторный вход для вашей роли обязателен — отключить его нельзя",
                                status_code=303)
    if not totp.verify(u.get("login", ""), code):
        return RedirectResponse("/account?error=Неверный код — второй фактор не отключен", status_code=303)
    totp.disable(u["login"])
    users.audit(request, "totp", u["login"], "отключен в профиле")
    r = RedirectResponse("/account?saved=1", status_code=303)
    _reissue(request, r, u["login"])
    return r


def _reissue(request, resp, login):
    hours = int(core.load_settings().get("session_hours") or 336)
    resp.set_cookie("korrektor", core.make_token(login, hours * 3600, users.session_stamp(users.get(login))),
                    httponly=True, samesite="lax", max_age=hours * 3600,
                    secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")


@router.post("/account/password")
def account_password(request: Request, old: str = Form(""), new: str = Form(""), new2: str = Form("")):
    u = me(request)
    if u.get("source") != "local":
        return RedirectResponse("/account?error=Пароль этой учетной записи меняется не здесь "
                                "(администратор — командой sudo korrektor passwd, LDAP — в домене)", status_code=303)
    full = users.get(u["login"])
    if not users.verify_pw(old, full.get("pw", "")):
        return RedirectResponse("/account?error=Текущий пароль указан неверно", status_code=303)
    if new != new2:
        return RedirectResponse("/account?error=Новые пароли не совпадают", status_code=303)
    prob = users.pw_problem(new)
    if prob:
        return RedirectResponse("/account?error=" + prob, status_code=303)
    users.save(u["login"], full["name"], full["role"], new, active=full["active"], email=full["email"],
               department=full["department"])
    users.audit(request, "password", u["login"])
    # пароль сменился — прежние сеансы (в том числе чужие) больше не действуют; текущему выдаем новый
    hours = int(core.load_settings().get("session_hours") or 336)
    r = RedirectResponse("/account?saved=1", status_code=303)
    r.set_cookie("korrektor", core.make_token(u["login"], hours * 3600, users.session_stamp(users.get(u["login"]))),
                 httponly=True, samesite="lax", max_age=hours * 3600,
                 secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@router.post("/account/profile")
def account_profile(request: Request, name: str = Form(""), email: str = Form(""), department: str = Form("")):
    u = me(request)
    if u.get("source") not in ("local", "ldap", "oidc"):
        raise HTTPException(400, "Профиль встроенного администратора не редактируется")
    full = users.get(u["login"])
    if core.load_settings().get("department_scope"):
        department = full.get("department") or ""      # при разделении по отделам отдел назначает администратор
    elif (department or "").strip() != (full.get("department") or ""):
        users.audit(request, "profile", u["login"], {"department": [full.get("department") or "", department.strip()]})
    try:
        users.save(u["login"], name.strip()[:120], full["role"], None, active=full["active"], email=email.strip()[:200],
                   department=department.strip()[:120])
    except ValueError as e:
        return RedirectResponse("/account?error=" + str(e), status_code=303)
    return RedirectResponse("/account?saved=1", status_code=303)


# ------------------------------------------------------------------ проверки связи
@router.post("/settings/test-ldap")
async def test_ldap(request: Request):
    form = await request.form()
    st = core.load_settings()
    ok, info = await run_in_threadpool(users.ldap_bind, form.get("login", ""), form.get("password", ""), st)
    if ok:
        return {"ok": True, "answer": "вход выполнен" + (f": {info.get('name')}, {info.get('email')}" if info else "")}
    return {"ok": False, "error": {"bad": "неверный логин или пароль", "unavailable": "сервер LDAP недоступен или не "
                                   "установлен модуль ldap3 (см. журнал службы)", "empty": "введите логин и пароль"}.get(info, str(info))}


@router.post("/settings/test-notify")
async def test_notify(request: Request):
    st = core.load_settings()
    res = await run_in_threadpool(notify.send_test, st)
    return JSONResponse({"ok": all(v == "ok" for v in res.values()) and bool(res),
                         "answer": "; ".join(f"{k}: {v}" for k, v in res.items()) or "ни один канал не включен",
                         "error": "; ".join(f"{k}: {v}" for k, v in res.items()) or "ни один канал не включен"})


@router.post("/settings/remind-now")
async def remind_now(request: Request):
    st = core.load_settings()
    r = await run_in_threadpool(notify.send_reminders, st, True)
    if not r["docs"]:
        return {"ok": True, "answer": "неразобранных замечаний старше срока нет — напоминать не о чем"}
    ch = "; ".join(f"{k}: {v}" for k, v in r["result"].items()) or "ни один канал не включен"
    users.audit(request, "remind", f"{r['docs']} документов", r)
    return {"ok": all(v == "ok" for v in r["result"].values()) and bool(r["result"]),
            "answer": f"документов: {r['docs']}, писем: {r['letters']}; {ch}", "error": f"документов: {r['docs']}; {ch}"}


@router.post("/settings/mgmt-report-now")
async def mgmt_report_now(request: Request):
    from . import report_pdf
    try:
        r = await run_in_threadpool(report_pdf.send, core.load_settings(), True)
    except Exception as e:  # noqa
        return JSONResponse({"ok": False, "error": str(e)[:300]})
    users.audit(request, "mgmt_report", "отправлен вручную", r)
    return {"ok": r == "ok", "answer": "отчет отправлен", "error": r}


# ------------------------------------------------------------------ справочник продукции
async def _products_from_request(request):
    from .pipeline import products
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        if not f or not getattr(f, "filename", ""):
            raise ValueError("выберите файл CSV или XLSX")
        data, name = await f.read(), f.filename
    else:                                              # REST API: тело запроса — CSV, имя — в ?name=
        if int(request.headers.get("content-length") or 0) > 50 * 1024 * 1024:
            raise ValueError("файл больше 50 МБ")
        data, name = await request.body(), request.query_params.get("name") or "products.csv"
    if len(data) > 50 * 1024 * 1024:
        raise ValueError("файл больше 50 МБ")
    return await run_in_threadpool(products.save, data, name, products.PATH())


@router.post("/settings/products")
async def products_upload(request: Request):
    try:
        ref = await _products_from_request(request)
    except Exception as e:  # noqa
        return RedirectResponse("/settings?perror=" + str(e)[:300] + "#standard", status_code=303)
    users.audit(request, "products", ref["file"], {"items": len(ref["items"]), "units": ref["units"]})
    return RedirectResponse(f"/settings?psaved={len(ref['items'])}#standard", status_code=303)


@router.post("/settings/products/delete")
def products_delete(request: Request):
    import os
    from .pipeline import products
    if os.path.exists(products.PATH()):
        os.remove(products.PATH())
    users.audit(request, "products", "удален")
    return RedirectResponse("/settings#standard", status_code=303)


@router.get("/settings/products.csv")
def products_csv(request: Request):
    from .pipeline import products
    ref = products.load(products.PATH())
    if not ref:
        raise HTTPException(404)
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    names = list(ref["units"])
    w.writerow(["Артикул", "Наименование"] + [f"{n}, {ref['units'][n]}" for n in names])
    for it in ref["items"].values():
        w.writerow([safe(it["article"]), safe(it.get("name", ""))] + [it["params"].get(n, {}).get("raw", "") for n in names])
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=products.csv"})


@router.post("/api/products")
async def api_products(request: Request):
    """Обновление справочника из 1С / ERP: POST тело — CSV (или multipart с полем file). Токен API или администратор."""
    u = me(request)
    if not (u.get("source") == "token" or users.can(u, "admin")):
        raise HTTPException(403, "Справочник обновляет администратор или внешняя система по токену API")
    try:
        ref = await _products_from_request(request)
    except Exception as e:  # noqa
        return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=400)
    users.audit(request, "products", ref["file"], {"items": len(ref["items"])})
    return {"ok": True, "items": len(ref["items"]), "units": ref["units"]}


# ------------------------------------------------------------------ сравнение версий
@router.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request, a: int = 0, b: int = 0, job: int = 0):
    from . import compare
    own = users.own_only(me(request))
    if job and not (a and b):
        a, b = compare.default_pair(job, own)
        a, b = a or 0, b or 0
    res = compare.compare(a, b) if a and b and a != b else None
    pivot = b or a or job
    same, other = compare.candidates(pivot, own) if pivot and core.get_job(pivot) else ([], [])
    return _tpl.TemplateResponse(request, "compare.html", _ctx(request, "jobs", res=res, a=a, b=b, same=same,
                                                               other=other, pivot=core.get_job(pivot) if pivot else None))


@router.get("/api/compare")
def compare_api(a: int, b: int):
    from . import compare
    res = compare.compare(a, b)
    if not res:
        raise HTTPException(404, "Оба задания должны быть готовы")
    return {k: (len(v) if isinstance(v, list) and k in ("fixed", "remain", "hidden", "new") else v) for k, v in res.items()
            if k not in ("a", "b")} | {"a": a, "b": b, "details": compare.rows_csv(res)}


@router.get("/compare/export")
def compare_export(a: int, b: int):
    from . import compare
    res = compare.compare(a, b)
    if not res:
        raise HTTPException(404)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    from .pipeline.exports import safe
    for r in compare.rows_csv(res):
        w.writerow([safe(x) for x in r])
    name = f"сравнение_{a}_{b}.csv"
    from urllib.parse import quote
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.post("/settings/apply")
async def settings_apply(request: Request):
    """Изменить одну логическую настройку (кнопка в рекомендациях аналитики)."""
    from .settings_schema import SCHEMA
    body = await request.json()
    key = body.get("key") if isinstance(body, dict) else None
    if key not in SCHEMA or SCHEMA[key]["type"] != "bool":
        raise HTTPException(400, "Так можно менять только логические настройки (включено/выключено)")
    st = core.update_settings(lambda s: s.__setitem__(key, bool(body.get("value"))))
    users.audit(request, "settings", key, {key: st[key]})
    return {"ok": True, "key": key, "value": st[key], "title": SCHEMA[key]["title"]}


# ------------------------------------------------------------------ метрики Prometheus
@router.get("/metrics")
def metrics(request: Request):
    """Метрики в формате Prometheus. Доступ: с самого сервера (127.0.0.1) или с токеном REST API."""
    import hmac as _h
    import os
    import time as _t
    st = core.load_settings()
    host = request.client.host if request.client else ""
    tok = st.get("api_token")
    auth = request.headers.get("authorization", "")
    proxied = any(h in request.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded"))
    if (proxied or host not in ("127.0.0.1", "::1", "localhost")) and not (tok and _h.compare_digest(auth.encode(), f"Bearer {tok}".encode())):
        return Response("доступ: с сервера или с заголовком Authorization: Bearer <токен API>\n", status_code=401,
                        media_type="text/plain; charset=utf-8")
    week = (__import__("datetime").datetime.now() - __import__("datetime").timedelta(days=7)).isoformat()
    with core.db() as c:
        by_status = dict(c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        done = c.execute("SELECT COUNT(*), COALESCE(SUM(pages),0), COALESCE(SUM(findings),0), COALESCE(SUM(verify_ok),0) "
                         "FROM jobs WHERE status='done'").fetchone()
        wk = c.execute("SELECT COUNT(*), COALESCE(SUM(pages),0), COALESCE(SUM(findings),0), COALESCE(SUM(verify_ok),0) "
                       "FROM jobs WHERE status='done' AND created>=?", (week,)).fetchone()
        dur = c.execute("SELECT started, finished FROM jobs WHERE status='done' AND started IS NOT NULL "
                        "ORDER BY id DESC LIMIT 200").fetchall()
        nusers = c.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    import datetime as _dt
    secs = []
    for a, b in dur:
        try:
            secs.append((_dt.datetime.fromisoformat(b) - _dt.datetime.fromisoformat(a)).total_seconds())
        except (TypeError, ValueError):
            pass
    size = 0
    for root, _, files in os.walk(core.DATA):
        for fn in files:
            try:
                size += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    lines = [
        "# HELP korrektor_info Версия сервиса", "# TYPE korrektor_info gauge", f'korrektor_info{{version="{core.VERSION}"}} 1',
        "# HELP korrektor_jobs Задания по состоянию", "# TYPE korrektor_jobs gauge"]
    for s in ("queued", "running", "done", "failed", "uploading"):
        lines.append(f'korrektor_jobs{{status="{s}"}} {by_status.get(s, 0)}')
    lines += [
        "# HELP korrektor_pages_total Проверено страниц", "# TYPE korrektor_pages_total counter", f"korrektor_pages_total {done[1]}",
        "# HELP korrektor_findings_total Замечаний в отчетах", "# TYPE korrektor_findings_total counter", f"korrektor_findings_total {done[2]}",
        "# HELP korrektor_reports_verified_total Отчетов, прошедших 25 проверок", "# TYPE korrektor_reports_verified_total counter",
        f"korrektor_reports_verified_total {done[3]}",
        "# HELP korrektor_week_documents Документов за 7 дней", "# TYPE korrektor_week_documents gauge", f"korrektor_week_documents {wk[0]}",
        "# HELP korrektor_week_density Замечаний на 10 страниц за 7 дней", "# TYPE korrektor_week_density gauge",
        f"korrektor_week_density {round(10.0 * wk[2] / wk[1], 3) if wk[1] else 0}",
        "# HELP korrektor_week_verify_ratio Доля отчетов 25/25 за 7 дней", "# TYPE korrektor_week_verify_ratio gauge",
        f"korrektor_week_verify_ratio {round(wk[3] / wk[0], 4) if wk[0] else 1}",
        "# HELP korrektor_processing_seconds_avg Среднее время обработки (последние 200 заданий)",
        "# TYPE korrektor_processing_seconds_avg gauge", f"korrektor_processing_seconds_avg {round(sum(secs) / len(secs), 2) if secs else 0}",
        "# HELP korrektor_data_bytes Размер папки данных", "# TYPE korrektor_data_bytes gauge", f"korrektor_data_bytes {size}",
        "# HELP korrektor_users_active Активных пользователей", "# TYPE korrektor_users_active gauge", f"korrektor_users_active {nusers}",
        "# HELP korrektor_scrape_timestamp Время сбора", "# TYPE korrektor_scrape_timestamp gauge", f"korrektor_scrape_timestamp {int(_t.time())}",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/compare/page.png")
def compare_page_png(a: int, b: int, p: int, side: str = "b"):
    from . import compare
    res = compare.page_diff_png(a, b, p, "a" if side == "a" else "b")
    if not res:
        raise HTTPException(404)
    return Response(res[0], media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ резервные копии
@router.post("/settings/backup-now")
async def backup_now(request: Request):
    from . import backup
    try:
        path = await run_in_threadpool(backup.run, core.load_settings(), "вручную")
    except Exception as e:  # noqa
        return JSONResponse({"ok": False, "error": str(e)[:300]})
    users.audit(request, "backup", path)
    import os
    return {"ok": True, "answer": f"создана копия {os.path.basename(path)} ({os.path.getsize(path) // 1024} КБ)"}


@router.get("/settings/backups/{name}")
def backup_download(request: Request, name: str):
    from fastapi.responses import FileResponse
    from . import backup
    path = backup.path_of(name)
    if not path:
        raise HTTPException(404)
    users.audit(request, "download", name, "резервная копия")
    return FileResponse(path, filename=name, media_type="application/gzip")


@router.post("/settings/backups/{name}/verify")
async def backup_verify(request: Request, name: str):
    from . import backup
    path = backup.path_of(name)
    if not path:
        raise HTTPException(404)
    r = await run_in_threadpool(backup.verify, path)
    if r["ok"]:
        txt = (f"копия исправна: база — ok, заданий {r.get('db_jobs', 0)}, пользователей {r.get('db_users', 0)}"
               + ("" if r.get("pages_included") else "; изображения страниц не входят") + (f"; контрольная сумма {r['sha256']}" if r.get("sha256") else ""))
    else:
        txt = "копия повреждена: " + "; ".join(r["errors"])
    return {"ok": r["ok"], "answer": txt}


@router.post("/settings/backups/{name}/restore")
async def backup_restore(request: Request, name: str, confirm: str = Form("")):
    from . import backup
    path = backup.path_of(name)
    if not path:
        raise HTTPException(404)
    if confirm != "ВОССТАНОВИТЬ":
        return JSONResponse({"ok": False, "error": "для подтверждения введите слово ВОССТАНОВИТЬ"})
    actor = dict(me(request))
    try:
        r = await run_in_threadpool(backup.restore, path)
    except Exception as e:  # noqa
        core.log.exception("восстановление")
        return JSONResponse({"ok": False, "error": str(e)[:300]})
    request.state.user = actor                     # запись в журнал уже восстановленной базы — от того, кто восстанавливал
    users.audit(request, "restore", name, r)
    return {"ok": True, "answer": f"восстановлено: заданий {r['jobs']}, пользователей {r['users']}. "
            f"Текущие данные перед восстановлением сохранены в {r['safety']}. Обновите страницу."}


@router.post("/settings/backups-upload")
async def backup_upload(request: Request):
    """Загрузка архива копии с компьютера (перенос на новый сервер). После загрузки — проверка целостности."""
    import os
    from . import backup
    form = await request.form()
    f = form.get("file")
    if not f or not getattr(f, "filename", ""):
        return RedirectResponse("/settings?berror=Выберите файл#backup", status_code=303)
    head = await f.read(2)
    await f.seek(0)
    if head != b"\x1f\x8b":
        return RedirectResponse("/settings?berror=Это не архив .tgz#backup", status_code=303)
    try:
        path = await run_in_threadpool(backup.save_upload, f.file)
    except (ValueError, OSError) as e:
        return RedirectResponse("/settings?berror=" + str(e)[:200] + "#backup", status_code=303)
    r = await run_in_threadpool(backup.verify, path)
    users.audit(request, "backup_upload", os.path.basename(path), {"ok": r["ok"], "errors": r["errors"][:3]})
    if not r["ok"]:
        os.remove(path)
        return RedirectResponse("/settings?berror=" + "; ".join(r["errors"])[:300] + "#backup", status_code=303)
    return RedirectResponse(f"/settings?bsaved={os.path.basename(path)}#backup", status_code=303)
