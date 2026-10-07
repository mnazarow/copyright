# -*- coding: utf-8 -*-
"""Конфигурация, настройки, база заданий, авторизация и фоновые обработчики."""
import collections
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import secrets
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

def _secret():
    """Ключ подписи сеансов: из KORREKTOR_SECRET или случайный, созданный один раз в папке данных."""
    s = os.environ.get("KORREKTOR_SECRET")
    if s:
        return s
    p = os.path.join(DATA, ".secret")
    if not os.path.exists(p):
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, secrets.token_hex(32).encode())
            os.close(fd)
        except FileExistsError:
            pass
    return open(p).read().strip()


SECRET = _secret()
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
        except (ValueError, OverflowError):
            return s["default"]
        return max(s.get("min", v), min(s.get("max", v), v))
    if t == "float":
        try:
            v = float(str(raw).replace(",", "."))
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
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
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        if "started" not in cols:                       # миграция с версии 1.0.0
            c.execute("ALTER TABLE jobs ADD COLUMN started TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch)")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)")
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


_state_locks = collections.defaultdict(threading.RLock)


def state_lock(jid):
    """Блокировка «прочитать → изменить → записать» state.json одного задания."""
    return _state_locks[int(jid)]


def load_state(jid):
    p = os.path.join(job_dir(jid), "state.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        log.exception("state.json задания %s не читается", jid)
        return None


def write_json_atomic(path, data, **kw):
    tmp = f"{path}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, **kw)
    os.replace(tmp, path)


def save_state(jid, state):
    write_json_atomic(os.path.join(job_dir(jid), "state.json"), state, indent=1)


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
    return hmac.compare_digest(sig.encode(), good.encode()) and exp.isdigit() and int(exp) > time.time()


def check_password(pw):
    return bool(ADMIN_PASSWORD) and hmac.compare_digest((pw or "").encode(), ADMIN_PASSWORD.encode())


# ------------------------------------------------------------------ обработчики
_queue_event = threading.Event()


def kick():
    _queue_event.set()


def _process(jid):
    from .pipeline import run
    from . import batch as batchmod
    job = get_job(jid)
    if not job:
        return
    st = load_settings()
    try:
        st.update(json.loads(job.get("settings") or "{}"))
    except ValueError:
        pass
    d = job_dir(jid)
    if not os.path.exists(os.path.join(d, "source.pdf")):
        update_job(jid, status="failed", error="исходный файл не найден", step="Ошибка")
        return
    logf = open(os.path.join(d, "job.log"), "a", encoding="utf-8")

    def jlog(msg):
        line = f"{dt.datetime.now():%H:%M:%S} {msg}"
        logf.write(line + "\n")
        logf.flush()
        log.info("job %s: %s", jid, msg)

    timing = []                                      # длительность этапов → timing.json (для аналитики)

    def progress(p, step=""):
        now = time.time()
        if step and (not timing or timing[-1][0] != step):
            timing.append([step, now])
        update_job(jid, progress=round(p, 3), step=step)

    update_job(jid, status="running", progress=0.01, step="Запуск",
               started=dt.datetime.now().isoformat(timespec="seconds"), error="")
    try:
        state = run.process(d, job["name"], st, jlog, progress, jid)
        v = state["verify"]
        update_job(jid, status="done", progress=1, step="Готово", pages=state["pages"],
                   findings=v.get("count", len(state["findings"])), verify_ok=1 if v.get("ok") else 0,
                   verify_score=f"{v.get('passed', 0)}/{v.get('total', 0)}",
                   finished=dt.datetime.now().isoformat(timespec="seconds"))
        _save_timing(d, timing)
    except Exception as e:  # noqa
        jlog("ОШИБКА: " + str(e))
        jlog(traceback.format_exc())
        update_job(jid, status="failed", error=str(e)[:500], step="Ошибка")
    finally:
        logf.close()
    if job.get("batch"):                             # сводка пакета — и при ошибке последнего задания
        try:
            batchmod.maybe_finish(job["batch"])
        except Exception:  # noqa
            log.exception("сводка пакета %s", job["batch"])


def _save_timing(d, timing):
    try:
        end = time.time()
        rows = [{"step": st, "sec": round((timing[i + 1][1] if i + 1 < len(timing) else end) - t0, 2)}
                for i, (st, t0) in enumerate(timing)]
        write_json_atomic(os.path.join(d, "timing.json"), rows)
    except Exception:  # noqa
        log.exception("timing.json")


def worker_loop():
    while True:
        jid = None
        try:
            with _lock:
                with db() as c:
                    r = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                    if r and c.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'",
                                       (r["id"],)).rowcount:
                        jid = r["id"]
        except sqlite3.Error:
            log.exception("очередь заданий")
            time.sleep(5)
            continue
        if jid:
            try:
                _process(jid)
            except Exception:  # noqa — поток-обработчик не должен завершаться
                log.exception("обработчик: задание %s", jid)
                try:
                    update_job(jid, status="failed", error="внутренняя ошибка обработчика", step="Ошибка")
                except Exception:  # noqa
                    pass
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


_started = False


def start_workers():
    global _started
    init_db()
    if _started:
        return
    _started = True
    n = int(load_settings().get("workers") or 1)
    for _ in range(max(1, n)):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()


def new_batch_id():
    return uuid.uuid4().hex[:10]
