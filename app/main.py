# -*- coding: utf-8 -*-
"""Веб-сервис «Корректор»: загрузка PDF, отчет о корректорской вычитке, настройки, документация."""
import datetime as dt
import io
import json
import os
import re
import secrets
import zipfile

import markdown
from markdown.extensions.toc import slugify_unicode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import batch as batchmod
from . import core
from .pipeline import run
from .pipeline.common import AUTHOR, TYPES
from .settings_schema import DEFAULTS, GROUPS, PROFILES, S, SCHEMA, profile_values

app = FastAPI(title="Корректор", version=core.VERSION, docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(core.APP_DIR, "static")), name="static")
DOCS_DIR = os.path.join(core.ROOT, "docs")
app.mount("/docs-files", StaticFiles(directory=DOCS_DIR), name="docs-files")
tpl = Jinja2Templates(directory=os.path.join(core.APP_DIR, "templates"))
tpl.env.globals.update(version=core.VERSION, TYPES=TYPES, AUTHOR=AUTHOR)

DOC_PAGES = [
    ("index", "Обзор и быстрый старт"),
    ("methodology", "Методология вычитки"),
    ("usage", "Работа с сервисом"),
    ("settings", "Справочник настроек"),
    ("installation", "Установка, обновление, удаление"),
    ("administration", "Администрирование и безопасность"),
    ("api", "REST API и интеграция"),
    ("faq", "Вопросы и неполадки"),
]
PUBLIC = ("/login", "/static", "/api/health", "/docs-files")


@app.on_event("startup")
def startup():
    core.start_workers()


# ------------------------------------------------------------------ доступ
def authed(request: Request):
    st = core.load_settings()
    if not st.get("auth_required", True) or not core.ADMIN_PASSWORD:
        return True
    if core.check_token(request.cookies.get("korrektor", "")):
        return True
    tok = st.get("api_token")
    auth = request.headers.get("authorization", "")
    return bool(tok) and auth == f"Bearer {tok}"


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if not path.startswith(PUBLIC) and not authed(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "требуется авторизация"}, status_code=401)
        return RedirectResponse("/login?next=" + path, status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str = ""):
    return tpl.TemplateResponse(request, "login.html", {"next": next, "error": error, "page": "login"})


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if not core.check_password(password):
        return RedirectResponse(f"/login?error=1&next={next}", status_code=303)
    r = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    r.set_cookie("korrektor", core.make_token(), httponly=True, samesite="lax", max_age=86400 * 14)
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
    items = []
    for f in files:
        data = await f.read()
        if len(data) > limit:
            raise HTTPException(413, f"Файл {f.filename} больше {st.get('max_upload_mb')} МБ")
        name = os.path.basename(f.filename or "document.pdf")
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for zi in z.infolist():
                    if zi.filename.lower().endswith(".pdf") and not zi.is_dir():
                        items.append((os.path.basename(zi.filename), z.read(zi)))
        elif data[:5] == b"%PDF-":
            items.append((name, data))
    ids = []
    for name, data in items:
        stem = re.sub(r"\.pdf$", "", name, flags=re.I)
        jid = core.create_job(bid, stem, name, len(data), over)
        d = core.job_dir(jid)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "source.pdf"), "wb").write(data)
        core.update_job(jid, status="queued")
        ids.append(jid)
    core.kick()
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
    state = core.load_state(jid)
    if not job or not state:
        raise HTTPException(404)
    st = core.load_settings()
    v = state.get("verify", {})
    if kind == "source":
        return FileResponse(os.path.join(core.job_dir(jid), "source.pdf"), filename=job["filename"])
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for kind, fn in state["verify"].get("files", {}).items():
            p = os.path.join(core.job_dir(jid), fn)
            if os.path.exists(p):
                z.write(p, os.path.basename(fn))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=report_{jid}.zip"})


@app.post("/job/{jid}/delete")
def job_delete(jid: int):
    core.delete_job(jid)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/job/{jid}/restart")
def job_restart(jid: int):
    d = core.job_dir(jid)
    for fn in os.listdir(d):
        if fn not in ("source.pdf",):
            p = os.path.join(d, fn)
            if os.path.isfile(p):
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


@app.post("/api/jobs/{jid}/findings/{fid}")
async def api_finding(jid: int, fid: int, request: Request):
    body = await request.json()
    state = core.load_state(jid)
    if not state:
        raise HTTPException(404)
    for f in state["findings"]:
        if f["id"] == fid:
            if "status" in body and body["status"] in ("new", "accepted", "rejected"):
                f["status"] = body["status"]
            for k in ("fix", "good", "note", "type"):
                if k in body and isinstance(body[k], str):
                    if k == "type" and body[k] not in TYPES:
                        continue
                    f[k] = body[k]
            core.save_state(jid, state)
            return {"ok": True, "finding": f}
    raise HTTPException(404)


@app.post("/api/jobs/{jid}/rebuild")
async def api_rebuild(jid: int, request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa
        pass
    state = core.load_state(jid)
    job = core.get_job(jid)
    if not state or not job:
        raise HTTPException(404)
    if body.get("summary"):
        state["meta"]["summary"] = body["summary"]
        state["summary_edited"] = True
    st = core.load_settings()
    st.update(json.loads(job.get("settings") or "{}"))
    state["verify"] = run.build_report(core.job_dir(jid), state, st, lambda m: None)
    core.save_state(jid, state)
    v = state["verify"]
    core.update_job(jid, findings=v.get("count", 0), verify_ok=1 if v.get("ok") else 0,
                    verify_score=f"{v.get('passed', 0)}/{v.get('total', 0)}")
    return {"ok": v.get("ok"), "passed": v.get("passed"), "total": v.get("total"), "count": v.get("count"),
            "checks": v.get("checks")}


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
    data = json.loads((await file.read()).decode("utf-8"))
    st = core.load_settings()
    for k, v in data.items():
        if k in SCHEMA and not (SCHEMA[k]["type"] == "secret" and not v):
            st[k] = v
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
        text = (await file.read()).decode("utf-8")
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
