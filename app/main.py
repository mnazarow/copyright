# -*- coding: utf-8 -*-
"""Веб-сервис «Корректор»: загрузка PDF, отчет о корректорской вычитке, настройки, документация."""
import collections
import contextlib
import datetime as dt
import io
import json
import os
import re
import asyncio
import hmac
import secrets
import time
import zipfile
import zlib
from urllib.parse import quote, urlsplit

import markdown
from markdown.extensions.toc import slugify_unicode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import analytics
from . import batch as batchmod
from . import core
from .pipeline import run
from .pipeline.common import AUTHOR, TYPES
from .settings_schema import DEFAULTS, GROUPS, PROFILES, S, SCHEMA, profile_values

@contextlib.asynccontextmanager
async def lifespan(_app):
    core.start_workers()
    yield


app = FastAPI(title="Корректор", version=core.VERSION, docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(core.APP_DIR, "static")), name="static")
DOCS_DIR = os.path.join(core.ROOT, "docs")
app.mount("/docs-files", StaticFiles(directory=DOCS_DIR), name="docs-files")
tpl = Jinja2Templates(directory=os.path.join(core.APP_DIR, "templates"))
tpl.env.globals.update(version=core.VERSION, TYPES=TYPES, AUTHOR=AUTHOR)

DOC_PAGES = [
    ("index", "Обзор и быстрый старт"),
    ("methodology", "Методология вычитки"),
    ("usage", "Работа с сервисом"),
    ("analytics", "Аналитика отчетов"),
    ("trends", "Тренды"),
    ("settings", "Справочник настроек"),
    ("installation", "Установка, обновление, удаление"),
    ("administration", "Администрирование и безопасность"),
    ("api", "REST API и интеграция"),
    ("faq", "Вопросы и неполадки"),
]
PUBLIC = ("/login", "/static", "/api/health", "/docs-files")


# ------------------------------------------------------------------ доступ
def authed(request: Request):
    """'cookie' — вход через браузер, 'token' — токен REST API (только /api/…), None — нет доступа."""
    st = core.load_settings()
    if not st.get("auth_required", True) or not core.ADMIN_PASSWORD:
        return "open"
    if core.check_token(request.cookies.get("korrektor", "")):
        return "cookie"
    tok = st.get("api_token")
    auth = request.headers.get("authorization", "")
    if tok and request.url.path.startswith("/api/") and hmac.compare_digest(auth.encode(), f"Bearer {tok}".encode()):
        return "token"
    return None


def same_origin(request: Request):
    """Защита от межсайтовых запросов: POST из браузера должен прийти со страницы этого же сервиса."""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src:
        return True                                   # не браузер (curl, скрипт) — браузеры всегда шлют Origin
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(src).netloc == host


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC):
        return await call_next(request)
    how = authed(request)
    if not how:
        if path.startswith("/api/"):
            return JSONResponse({"error": "требуется авторизация"}, status_code=401)
        return RedirectResponse("/login?next=" + quote(path), status_code=303)
    if request.method not in ("GET", "HEAD", "OPTIONS") and how == "cookie" and not same_origin(request):
        return JSONResponse({"error": "запрос с чужой страницы отклонен"}, status_code=403)
    return await call_next(request)


def safe_next(nxt):
    """Только относительный адрес внутри сервиса (защита от открытого перенаправления)."""
    nxt = (nxt or "/").strip()
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\") or "\n" in nxt or "\r" in nxt:
        return "/"
    return nxt


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return tpl.TemplateResponse(request, "login.html", {"next": next, "error": error, "page": "login"})


_fails = {}                                           # ip → [число неудач, время блокировки]


@app.post("/login")
async def login(request: Request, password: str = Form(...), next: str = Form("/")):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    cnt, until = _fails.get(ip, [0, 0])
    if until > time.time():
        return RedirectResponse("/login?error=locked&next=" + quote(safe_next(next)), status_code=303)
    if not core.check_password(password):
        cnt += 1
        _fails[ip] = [cnt, time.time() + 300 if cnt >= 5 else 0]   # 5 ошибок подряд — пауза 5 минут
        if len(_fails) > 10000:
            _fails.clear()
        await asyncio.sleep(1)
        return RedirectResponse("/login?error=" + ("locked" if cnt >= 5 else "1") + "&next=" + quote(safe_next(next)),
                                status_code=303)
    _fails.pop(ip, None)
    r = RedirectResponse(safe_next(next), status_code=303)
    r.set_cookie("korrektor", core.make_token(), httponly=True, samesite="lax", max_age=86400 * 14,
                 secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie("korrektor")
    return r


# ------------------------------------------------------------------ страницы
def ctx(request, page, **kw):
    st = core.load_settings()
    kw.update(request=request, page=page, st=st,
              auth_on=bool(core.ADMIN_PASSWORD) and st.get("auth_required", True),
              ai_on=st.get("ai_provider") not in (None, "", "none"))
    return kw


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    jobs = core.list_jobs(6)
    return tpl.TemplateResponse(request, "index.html", ctx(request, "index", jobs=jobs, profiles=SCHEMA["profile"]["options"],
                                                           modes=SCHEMA["mode"]["options"]))


@app.post("/upload")
async def upload(request: Request, files: list[UploadFile] = File(...), profile: str = Form(""),
                 mode: str = Form(""), note: str = Form("")):
    jobs = await _create_jobs(files, profile, mode, note)
    if not jobs:
        raise HTTPException(400, "Не найдено ни одного PDF")
    if len(jobs) == 1:
        return RedirectResponse(f"/job/{jobs[0]}", status_code=303)
    return RedirectResponse(f"/batch/{core.get_job(jobs[0])['batch']}", status_code=303)


async def _create_jobs(files, profile="", mode="", note=""):
    st = core.load_settings()
    limit = int(st.get("max_upload_mb") or 200) * 1024 * 1024
    over = {}
    if profile and profile in PROFILES:
        over.update({k: v for k, v in profile_values(profile).items() if k not in ("ai_api_key", "api_token")})
        over = {k: v for k, v in over.items() if v != st.get(k)}
    if mode:
        over["mode"] = mode
    if note:
        over["report_extra_header"] = (st.get("report_extra_header") or "") + "\n" + note
    bid = core.new_batch_id()
    ids = []
    total = 0
    for f in files:
        name = os.path.basename(f.filename or "document.pdf")
        if f.size is not None and f.size > limit:          # отказ до чтения файла в память
            raise HTTPException(413, f"Файл {name} больше {st.get('max_upload_mb')} МБ")
        data = await f.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"Файл {name} больше {st.get('max_upload_mb')} МБ")
        total += len(data)
        if total > limit * 3:
            raise HTTPException(413, f"За одну загрузку — не больше {limit * 3 // 2**20} МБ")
        ids += await run_in_threadpool(_store_upload, bid, name, data, over, limit)
    core.kick()
    return ids


def _store_upload(bid, name, data, over, limit):
    """Сохраняет PDF или PDF из ZIP-архива как задания (выполняется в пуле потоков, не блокирует сервер)."""
    ids = []

    def add(fname, blob):
        stem = re.sub(r"\.pdf$", "", fname, flags=re.I)
        jid = core.create_job(bid, stem, fname, len(blob), over)
        d = core.job_dir(jid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "source.pdf"), "wb") as fh:
            fh.write(blob)
        core.update_job(jid, status="queued")
        ids.append(jid)

    if name.lower().endswith(".zip"):
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise HTTPException(400, f"Файл {name} — поврежденный ZIP-архив")
        with z:
            pdfs = [zi for zi in z.infolist() if zi.filename.lower().endswith(".pdf") and not zi.is_dir()]
            if sum(zi.file_size for zi in pdfs) > limit * 3 or len(pdfs) > 200:      # защита от «zip-бомб»
                raise HTTPException(413, f"Архив {name}: после распаковки больше {limit * 3 // 2**20} МБ "
                                         "или больше 200 файлов")
            for zi in pdfs:
                try:
                    blob = z.read(zi)
                except (zipfile.BadZipFile, zlib.error, RuntimeError):
                    raise HTTPException(400, f"Архив {name} поврежден или зашифрован ({zi.filename})")
                if blob[:5] == b"%PDF-":
                    add(os.path.basename(zi.filename), blob)
    elif data[:5] == b"%PDF-":
        add(name, data)
    return ids


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    return tpl.TemplateResponse(request, "jobs.html", ctx(request, "jobs", jobs=core.list_jobs()))


@app.get("/batch/{bid}", response_class=HTMLResponse)
def batch_page(request: Request, bid: str):
    jobs = core.list_jobs(batch=bid)
    if not jobs:
        raise HTTPException(404)
    return tpl.TemplateResponse(request, "batch.html", ctx(request, "jobs", bid=bid, jobs=jobs, res=batchmod.load(bid)))


@app.get("/job/{jid}", response_class=HTMLResponse)
def job_page(request: Request, jid: int):
    job = core.get_job(jid)
    if not job:
        raise HTTPException(404)
    state = core.load_state(jid)
    logtxt = ""
    lp = os.path.join(core.job_dir(jid), "job.log")
    if os.path.exists(lp):
        logtxt = open(lp, encoding="utf-8").read()[-6000:]
    bsize = len(core.list_jobs(batch=job["batch"])) if job.get("batch") else 1
    return tpl.TemplateResponse(request, "job.html", ctx(request, "jobs", job=job, state=state, log=logtxt, bsize=bsize))


@app.get("/job/{jid}/file/{kind}")
def job_file(jid: int, kind: str):
    job = core.get_job(jid)
    if not job:
        raise HTTPException(404)
    if kind == "source":
        src = os.path.join(core.job_dir(jid), "source.pdf")
        if not os.path.exists(src):
            raise HTTPException(404)
        return FileResponse(src, filename=job["filename"])
    state = core.load_state(jid)
    if not state:
        raise HTTPException(404, "Отчет еще не готов")
    st = core.load_settings()
    v = state.get("verify", {})
    fn = v.get("files", {}).get(kind)
    if kind == "docx" and not v.get("ok") and not st.get("report_allow_unverified"):
        raise HTTPException(403, "Отчет не прошел машинную проверку (п. 0.9). Скачивание непринятого отчета "
                                 "можно разрешить в настройках: «Отчет» → «Разрешать скачивание непринятого отчета».")
    if not fn:
        raise HTTPException(404, "Этот формат не создавался — включите его в настройках «Отчет» → «Форматы отчета»")
    path = os.path.join(core.job_dir(jid), fn)
    return FileResponse(path, filename=os.path.basename(fn))


@app.get("/job/{jid}/page/{n}.png")
def job_page_img(jid: int, n: int):
    p = os.path.join(core.job_dir(jid), "pages", f"p{n:03d}.png")
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


@app.get("/job/{jid}/zip")
def job_zip(jid: int):
    state = core.load_state(jid)
    if not state:
        raise HTTPException(404)
    hide_docx = not state["verify"].get("ok") and not core.load_settings().get("report_allow_unverified")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for kind, fn in state["verify"].get("files", {}).items():
            if kind == "docx" and hide_docx:             # п. 0.9: непринятый отчет не выдается
                continue
            p = os.path.join(core.job_dir(jid), fn)
            if os.path.exists(p):
                z.write(p, os.path.basename(fn))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=report_{jid}.zip"})


def _idle_job(jid):
    job = core.get_job(jid)
    if not job:
        raise HTTPException(404)
    if job["status"] in ("running", "uploading"):
        raise HTTPException(409, "Задание сейчас выполняется — дождитесь окончания проверки")
    return job


@app.post("/job/{jid}/delete")
def job_delete(jid: int):
    _idle_job(jid)
    core.delete_job(jid)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/job/{jid}/restart")
def job_restart(jid: int):
    _idle_job(jid)
    d = core.job_dir(jid)
    if not os.path.exists(os.path.join(d, "source.pdf")):
        raise HTTPException(404, "Исходный файл задания не найден")
    with core.state_lock(jid):
        for fn in os.listdir(d):
            p = os.path.join(d, fn)
            if fn != "source.pdf" and os.path.isfile(p):
                os.remove(p)
        core.update_job(jid, status="queued", progress=0, step="В очереди", error="")
    core.kick()
    return RedirectResponse(f"/job/{jid}", status_code=303)


# ------------------------------------------------------------------ API заданий
@app.get("/api/health")
def health():
    return {"status": "ok", "version": core.VERSION}


@app.get("/api/jobs")
def api_jobs():
    return core.list_jobs(200)


@app.post("/api/jobs")
async def api_create(file: list[UploadFile] = File(...), profile: str = Form(""), mode: str = Form("")):
    ids = await _create_jobs(file, profile, mode)
    return {"jobs": ids}


@app.get("/api/jobs/{jid}")
def api_job(jid: int):
    job = core.get_job(jid)
    if not job:
        raise HTTPException(404)
    job.pop("settings", None)
    state = core.load_state(jid)
    if state:
        job["verify"] = {k: state["verify"].get(k) for k in ("ok", "passed", "total", "files")}
        job["findings_list"] = state["findings"]
    return job


@app.get("/api/jobs/{jid}/report")
def api_report(jid: int, format: str = "docx"):
    return job_file(jid, format)


async def json_body(request, default=None):
    try:
        body = await request.json()
    except ValueError:
        if default is not None:
            return default
        raise HTTPException(400, "Ожидается JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Ожидается JSON-объект")
    return body


def rebuild_job(jid, state=None, summary=None):
    """Пересобирает DOCX и машинную проверку задания. Вызывать под core.state_lock(jid)."""
    job = core.get_job(jid)
    state = state or core.load_state(jid)
    if not state or not job:
        raise HTTPException(404)
    if summary:
        state["meta"]["summary"] = summary
        state["summary_edited"] = True
    st = core.load_settings()
    try:
        st.update(json.loads(job.get("settings") or "{}"))
    except ValueError:
        pass
    state["verify"] = run.build_report(core.job_dir(jid), state, st, lambda m: None)
    core.save_state(jid, state)
    v = state["verify"]
    core.update_job(jid, findings=v.get("count", 0), verify_ok=1 if v.get("ok") else 0,
                    verify_score=f"{v.get('passed', 0)}/{v.get('total', 0)}")
    return v


def _edit_finding(jid, fid, body):
    with core.state_lock(jid):
        state = core.load_state(jid)
        if not state:
            raise HTTPException(404)
        for f in state["findings"]:
            if f["id"] == fid:
                if body.get("status") in ("new", "accepted", "rejected"):
                    f["status"] = body["status"]
                for k in ("fix", "good", "note", "type"):
                    if isinstance(body.get(k), str):
                        if k == "type" and body[k] not in TYPES:
                            continue
                        if body[k] != f.get(k) and k in ("fix", "good"):
                            f.setdefault("orig_" + k, f.get(k))
                            f["edited"] = True
                        f[k] = body[k][:2000]
                core.save_state(jid, state)
                return {"ok": True, "finding": f}
    raise HTTPException(404)


@app.post("/api/jobs/{jid}/findings/{fid}")
async def api_finding(jid: int, fid: int, request: Request):
    body = await json_body(request)
    return await run_in_threadpool(_edit_finding, jid, fid, body)


def _rebuild(jid, summary):
    with core.state_lock(jid):
        return rebuild_job(jid, summary=summary)


@app.post("/api/jobs/{jid}/rebuild")
async def api_rebuild(jid: int, request: Request):
    body = await json_body(request, {})
    summary = body.get("summary") if isinstance(body.get("summary"), str) else None
    v = await run_in_threadpool(_rebuild, jid, summary)
    return {"ok": v.get("ok"), "passed": v.get("passed"), "total": v.get("total"), "count": v.get("count"),
            "checks": v.get("checks")}


# ------------------------------------------------------------------ аналитика и тренды
def _q(request):
    return dict(request.query_params)


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    return tpl.TemplateResponse(request, "analytics.html", ctx(request, "analytics"))


@app.get("/trends", response_class=HTMLResponse)
def trends_page(request: Request):
    return tpl.TemplateResponse(request, "trends.html", ctx(request, "trends"))


@app.get("/api/analytics")
def api_analytics(request: Request):
    return JSONResponse(analytics.nan_safe(analytics.analytics(_q(request))))


@app.get("/api/trends")
def api_trends(request: Request):
    return JSONResponse(analytics.nan_safe(analytics.trends(_q(request))))


@app.get("/api/analytics/findings")
def api_explore(request: Request, limit: int = 200, offset: int = 0):
    return JSONResponse(analytics.explore(_q(request), max(1, min(limit, 2000)), max(0, offset)))


def _export(sheets, fmt, base):
    stamp = dt.date.today().isoformat()
    if fmt == "csv":
        return Response(analytics.to_csv(sheets), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={base}_{stamp}.csv"})
    return Response(analytics.to_xlsx(sheets, base), headers={"Content-Disposition": f"attachment; filename={base}_{stamp}.xlsx"},
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/analytics/export")
def api_analytics_export(request: Request, format: str = "xlsx"):
    q = _q(request)
    if format == "json":
        return JSONResponse(analytics.nan_safe(analytics.analytics(q)),
                            headers={"Content-Disposition": "attachment; filename=korrektor_analytics.json"})
    if format == "findings":
        ex = analytics.explore(q, 10 ** 6)
        head = ["Задание", "Документ", "Дата", "Стр.", "Тип", "Правило", "Источник", "Ошибка", "Как правильно",
                "Статус", "Системно, мест"]
        rows = [[r["job"], r["name"], r["created"], r["page"], r["type"], r["rule_label"], r["source_label"], r["bad"],
                 r["good"], analytics.STATUS.get(r["status"], r["status"]), r["systemic"] or ""] for r in ex["rows"]]
        return _export([("Замечания", head, rows)], q.get("as", "xlsx"), "korrektor_findings")
    return _export(analytics.export_rows(analytics.analytics(q)), format, "korrektor_analytics")


@app.get("/api/trends/export")
def api_trends_export(request: Request, format: str = "xlsx"):
    t = analytics.trends(_q(request))
    if format == "json":
        return JSONResponse(analytics.nan_safe(t), headers={"Content-Disposition": "attachment; filename=korrektor_trends.json"})
    return _export(analytics.trend_rows(t), format, "korrektor_trends")


@app.post("/api/analytics/whitelist")
async def api_whitelist(request: Request):
    """Добавить слова в белый список словаря (из таблицы «Слова вне словаря»)."""
    body = await json_body(request)
    words = body.get("words") if isinstance(body.get("words"), list) else []
    words = [w.strip() for w in words[:500] if isinstance(w, str) and re.fullmatch(r"[\w\-]{2,40}", w.strip())]
    st = core.load_settings()
    cur = (st.get("whitelist") or "").split()
    low = {w.lower() for w in cur}
    added = [w for w in dict.fromkeys(words) if w.lower() not in low]
    st["whitelist"] = " ".join(cur + added)
    core.save_settings(st)
    return {"ok": True, "added": added}


@app.post("/api/analytics/glossary")
async def api_glossary(request: Request):
    """Добавить пару «правильно: неправильно» в глоссарий компании (из таблицы «Повторяющиеся ошибки»)."""
    body = await json_body(request)
    good, bad = str(body.get("good", "")).strip(), str(body.get("bad", "")).strip()
    if not good or not bad or good == bad or any(c in good + bad for c in ":\n,") or len(good + bad) > 120:
        raise HTTPException(400, "Нужны непустые «правильно» и «неправильно» без двоеточий, запятых и переводов строк")
    st = core.load_settings()
    lines = (st.get("glossary") or "").splitlines()
    for i, ln in enumerate(lines):
        if ":" in ln and ln.split(":", 1)[0].strip().lower() == good.lower():
            wrong = [w.strip() for w in ln.split(":", 1)[1].split(",") if w.strip()]
            if bad.lower() in (w.lower() for w in wrong):
                return {"ok": True, "added": False}
            lines[i] = f"{ln.split(':', 1)[0].strip()}: " + ", ".join(wrong + [bad])
            break
    else:
        lines.append(f"{good}: {bad}")
    st["glossary"] = "\n".join(lines)
    core.save_settings(st)
    return {"ok": True, "added": True}


@app.post("/api/analytics/bulk")
async def api_bulk(request: Request):
    """Массовая отметка замечаний из обозревателя: [{job, id}] + status; затронутые отчеты пересобираются."""
    body = await json_body(request)
    status = body.get("status")
    if status not in ("new", "accepted", "rejected"):
        raise HTTPException(400, "status: new | accepted | rejected")
    items = collections.defaultdict(set)
    for it in (body.get("items") if isinstance(body.get("items"), list) else [])[:5000]:
        try:
            items[int(it["job"])].add(int(it["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    done = await run_in_threadpool(_bulk, items, status, body.get("rebuild", True) is not False)
    return {"ok": True, "changed": done}


def _bulk(items, status, rebuild):
    done = {}
    for jid, ids in items.items():
        with core.state_lock(jid):
            state = core.load_state(jid)
            if not state:
                continue
            n = 0
            for f in state["findings"]:
                if f["id"] in ids and f.get("status") != status:
                    f["status"] = status
                    n += 1
            if not n:
                continue
            core.save_state(jid, state)
            if rebuild:
                rebuild_job(jid, state)
            done[jid] = n
    return done


# ------------------------------------------------------------------ настройки
def grouped():
    return [(g, t, d, [s for s in S if s["group"] == g]) for g, t, d in GROUPS]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, applied: str = ""):
    st = core.load_settings()
    if applied and applied in PROFILES:
        keep = {k: st.get(k) for k in ("ai_api_key", "api_token", "ai_provider", "ai_base_url", "ai_model")}
        st = profile_values(applied)
        st.update(keep)
    prompt_info = run_prompt_info()
    return tpl.TemplateResponse(request, "settings.html", ctx(request, "settings", groups=grouped(), vals=st, saved=saved,
                                                              applied=applied, prompt=prompt_info))


def run_prompt_info():
    pd = os.path.join(core.APP_DIR, "prompts")
    custom = os.path.join(pd, "custom_prompt.md")
    files = sorted(f for f in os.listdir(pd) if f.endswith(".md") and f != "custom_prompt.md")
    active = custom if os.path.exists(custom) else os.path.join(pd, files[-1])
    first = open(active, encoding="utf-8").readline().strip("# \n")
    return {"name": os.path.basename(active), "title": first, "custom": os.path.exists(custom),
            "size": os.path.getsize(active)}


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    st = core.load_settings()
    for s in S:
        k = s["key"]
        if s["type"] == "bool":
            st[k] = k in form
        elif s["type"] == "multi":
            st[k] = form.getlist(k)
        elif s["type"] == "secret":
            v = form.get(k, "")
            if v and set(v) != {"•"}:
                st[k] = v.strip()
            elif form.get(k + "__clear"):
                st[k] = ""
        elif k in form:
            st[k] = core.coerce(k, form.get(k))
    core.save_settings(st)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/settings/export")
def settings_export(secrets_too: int = 0):
    st = core.load_settings()
    if not secrets_too:
        for s in S:
            if s["type"] == "secret":
                st[s["key"]] = ""
    return Response(json.dumps(st, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=korrektor-settings.json"})


@app.post("/settings/import")
async def settings_import(file: UploadFile = File(...)):
    try:
        data = json.loads((await file.read()).decode("utf-8"))
        assert isinstance(data, dict)
    except Exception:  # noqa
        raise HTTPException(400, "Файл не является выгрузкой настроек Корректора (ожидается JSON-объект)")
    st = core.load_settings()
    for k, v in data.items():
        if k in SCHEMA and not (SCHEMA[k]["type"] == "secret" and not v):
            st[k] = v if SCHEMA[k]["type"] in ("multi",) else core.coerce(k, v)
    core.save_settings(st)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/reset")
def settings_reset():
    st = core.load_settings()
    keep = {k: st.get(k) for k in ("ai_api_key", "api_token")}
    new = dict(DEFAULTS)
    new.update(keep)
    core.save_settings(new)
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/prompt")
async def settings_prompt(file: UploadFile = File(None), reset: str = Form("")):
    custom = os.path.join(core.APP_DIR, "prompts", "custom_prompt.md")
    if reset:
        if os.path.exists(custom):
            os.remove(custom)
    elif file is not None:
        try:
            text = (await file.read()).decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, "Мастер-промпт должен быть текстовым файлом в кодировке UTF-8 (.md)")
        if "МАСТЕР-ПРОМПТ" not in text[:300]:
            raise HTTPException(400, "Файл не похож на мастер-промпт: в первой строке нет «МАСТЕР-ПРОМПТ»")
        open(custom, "w", encoding="utf-8").write(text)
    return RedirectResponse("/settings?saved=1#ai", status_code=303)


@app.get("/settings/prompt/download")
def prompt_download():
    info = run_prompt_info()
    return FileResponse(os.path.join(core.APP_DIR, "prompts", info["name"]), filename=info["name"])


@app.post("/settings/test-ai")
def test_ai():
    from .pipeline import llm
    st = core.load_settings()
    try:
        ans = llm.call(st, "Ты проверяешь связь. Ответь строго JSON {\"findings\": []}.", "Проверка связи", print)
        return {"ok": True, "answer": ans[:300]}
    except Exception as e:  # noqa
        return {"ok": False, "error": str(e)[:500]}


@app.post("/settings/test-lt")
def test_lt():
    import httpx
    st = core.load_settings()
    try:
        r = httpx.post(st["lt_url"].rstrip("/") + "/v2/check", data={"text": "Это тестовый текст текст.",
                                                                    "language": "ru-RU"}, timeout=20)
        r.raise_for_status()
        return {"ok": True, "answer": f"LanguageTool отвечает, найдено замечаний: {len(r.json().get('matches', []))}"}
    except Exception as e:  # noqa
        return {"ok": False, "error": str(e)[:300]}


@app.post("/settings/token")
def new_token():
    st = core.load_settings()
    st["api_token"] = secrets.token_hex(24)
    core.save_settings(st)
    return {"token": st["api_token"]}


# ------------------------------------------------------------------ документация
def render_md(name, single=False):
    text = open(os.path.join(DOCS_DIR, name + ".md"), encoding="utf-8").read()
    text = re.sub(r"\]\(images/", "](/docs-files/images/", text)
    if single:
        text = re.sub(r"\]\((\w+)\.md(#[^)]+)?\)", lambda m: f"]({m.group(2) or '#doc-' + m.group(1)})", text)
    else:
        text = re.sub(r"\]\((\w+)\.md(#[^)]+)?\)", lambda m: f"](/docs/{m.group(1)}{m.group(2) or ''})", text)
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "toc", "attr_list", "admonition"],
                             extension_configs={"toc": {"slugify": slugify_unicode}})
    return re.sub(r'<pre><code class="language-mermaid">(.*?)</code></pre>',
                  lambda m: '<pre class="mermaid">' + m.group(1) + "</pre>", html, flags=re.S)


@app.get("/docs/all", response_class=HTMLResponse)
def docs_all(request: Request):
    html = "".join(f'<section id="doc-{n}" style="page-break-before:always">{render_md(n, True)}</section>' for n, _ in DOC_PAGES)
    return tpl.TemplateResponse(request, "docs.html", ctx(request, "docs", html=html, pages=DOC_PAGES, current="all",
                                                          title="Вся документация"))


@app.get("/docs", response_class=HTMLResponse)
@app.get("/docs/{name}", response_class=HTMLResponse)
def docs(request: Request, name: str = "index"):
    names = dict(DOC_PAGES)
    if name not in names:
        raise HTTPException(404)
    return tpl.TemplateResponse(request, "docs.html", ctx(request, "docs", html=render_md(name), pages=DOC_PAGES,
                                                          current=name, title=names[name]))


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return RedirectResponse("/docs/methodology", status_code=303)
