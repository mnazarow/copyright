# -*- coding: utf-8 -*-
"""Конфигурация, настройки, база заданий, авторизация и фоновые обработчики."""
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import traceback
import uuid

from .settings_schema import DEFAULTS, SCHEMA

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(APP_DIR)
VERSION = open(os.path.join(ROOT, "VERSION")).read().strip() if os.path.exists(os.path.join(ROOT, "VERSION")) else "dev"
DATA = os.environ.get("KORREKTOR_DATA", os.path.join(ROOT, "data"))
JOBS = os.path.join(DATA, "jobs")
BATCHES = os.path.join(DATA, "batches")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
DB_FILE = os.path.join(DATA, "korrektor.db")
for d in (DATA, JOBS, BATCHES):
    os.makedirs(d, exist_ok=True)

log = logging.getLogger("korrektor")
logging.basicConfig(level=os.environ.get("KORREKTOR_LOG", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")

SECRET = os.environ.get("KORREKTOR_SECRET") or hashlib.sha256((DATA + "korrektor").encode()).hexdigest()
ADMIN_PASSWORD = os.environ.get("KORREKTOR_ADMIN_PASSWORD", "")


# ------------------------------------------------------------------ настройки
_lock = threading.Lock()


def load_settings():
    st = dict(DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        try:
            st.update(json.load(open(SETTINGS_FILE, encoding="utf-8")))
        except Exception:  # noqa
            log.exception("settings.json поврежден, используются значения по умолчанию")
    return st


def save_settings(values):
    with _lock:
        clean = {k: v for k, v in values.items() if k in SCHEMA}
        tmp = SETTINGS_FILE + ".tmp"
        json.dump(clean, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)


def coerce(key, raw):
    s = SCHEMA[key]
    t = s["type"]
    if t == "bool":
        return raw in (True, "on", "1", "true", "yes")
    if t == "int":
        try:
            v = int(float(str(raw).replace(",", ".")))
        except ValueError:
            return s["default"]
        return max(s.get("min", v), min(s.get("max", v), v))
    if t == "float":
        try:
            v = float(str(raw).replace(",", "."))
        except ValueError:
            return s["default"]
        return max(s.get("min", v), min(s.get("max", v), v))
    if t == "multi":
        return list(raw) if isinstance(raw, (list, tuple)) else [raw] if raw else []
    if t == "select":
        allowed = [o[0] for o in s["options"]]
        return raw if raw in allowed else s["default"]
    return str(raw if raw is not None else "")


# ------------------------------------------------------------------ база
def db():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, name TEXT, filename TEXT, created TEXT,
            status TEXT, progress REAL DEFAULT 0, step TEXT DEFAULT '', pages INTEGER DEFAULT 0,
            findings INTEGER DEFAULT 0, verify_ok INTEGER DEFAULT 0, verify_score TEXT DEFAULT '',
            error TEXT DEFAULT '', size INTEGER DEFAULT 0, settings TEXT DEFAULT '{}', finished TEXT)""")
        c.execute("UPDATE jobs SET status='queued', progress=0 WHERE status='running'")


def job_dir(jid):
    return os.path.join(JOBS, str(jid))


def get_job(jid):
    with db() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return dict(r) if r else None


def list_jobs(limit=500, batch=None):
    with db() as c:
        if batch:
            rows = c.execute("SELECT * FROM jobs WHERE batch=? ORDER BY id", (batch,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def update_job(jid, **kw):
    if not kw:
        return
    keys = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*kw.values(), jid))


def create_job(batch, name, filename, size, settings):
    with db() as c:
        cur = c.execute("INSERT INTO jobs(batch,name,filename,created,status,size,settings) VALUES(?,?,?,?,?,?,?)",
                        (batch, name, filename, dt.datetime.now().isoformat(timespec="seconds"), "uploading", size,
                         json.dumps(settings, ensure_ascii=False)))
        return cur.lastrowid


def delete_job(jid):
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    with db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))


def load_state(jid):
    p = os.path.join(job_dir(jid), "state.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def save_state(jid, state):
    json.dump(state, open(os.path.join(job_dir(jid), "state.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ авторизация
def make_token(user="admin", ttl=86400 * 14):
    exp = str(int(time.time()) + ttl)
    sig = hmac.new(SECRET.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user}|{exp}|{sig}"


def check_token(tok):
    try:
        user, exp, sig = tok.split("|")
    except (ValueError, AttributeError):
        return False
    good = hmac.new(SECRET.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and int(exp) > time.time()


def check_password(pw):
    return bool(ADMIN_PASSWORD) and hmac.compare_digest(pw or "", ADMIN_PASSWORD)


# ------------------------------------------------------------------ обработчики
_queue_event = threading.Event()


def kick():
    _queue_event.set()


def _process(jid):
    from .pipeline import run
    from . import batch as batchmod
    job = get_job(jid)
    st = load_settings()
    st.update(json.loads(job.get("settings") or "{}"))
    d = job_dir(jid)
    logf = open(os.path.join(d, "job.log"), "a", encoding="utf-8")

    def jlog(msg):
        line = f"{dt.datetime.now():%H:%M:%S} {msg}"
        logf.write(line + "\n")
        logf.flush()
        log.info("job %s: %s", jid, msg)

    def progress(p, step=""):
        update_job(jid, progress=round(p, 3), step=step)

    update_job(jid, status="running", progress=0.01, step="Запуск")
    try:
        state = run.process(d, job["name"], st, jlog, progress, jid)
        v = state["verify"]
        update_job(jid, status="done", progress=1, step="Готово", pages=state["pages"],
                   findings=v.get("count", len(state["findings"])), verify_ok=1 if v.get("ok") else 0,
                   verify_score=f"{v.get('passed', 0)}/{v.get('total', 0)}",
                   finished=dt.datetime.now().isoformat(timespec="seconds"))
        if job.get("batch"):
            batchmod.maybe_finish(job["batch"])
    except Exception as e:  # noqa
        jlog("ОШИБКА: " + str(e))
        jlog(traceback.format_exc())
        update_job(jid, status="failed", error=str(e)[:500], step="Ошибка")
    finally:
        logf.close()


def worker_loop():
    while True:
        jid = None
        with _lock:
            with db() as c:
                r = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if r:
                    jid = r["id"]
                    c.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
        if jid:
            _process(jid)
            continue
        _queue_event.wait(5)
        _queue_event.clear()


def cleanup_loop():
    while True:
        try:
            days = int(load_settings().get("retention_days") or 0)
            if days:
                limit = (dt.datetime.now() - dt.timedelta(days=days)).isoformat()
                for j in list_jobs(100000):
                    if j["created"] < limit and j["status"] in ("done", "failed"):
                        delete_job(j["id"])
                        log.info("Удалено устаревшее задание %s", j["id"])
        except Exception:  # noqa
            log.exception("очистка")
        time.sleep(3600)


def start_workers():
    init_db()
    n = int(load_settings().get("workers") or 1)
    for _ in range(max(1, n)):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()


def new_batch_id():
    return uuid.uuid4().hex[:10]
