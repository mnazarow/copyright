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
CUSTOM_PROMPT = os.path.join(DATA, "custom_prompt.md")      # свой мастер-промпт: в папке данных (переживает обновления и Docker)
DB_FILE = os.path.join(DATA, "korrektor.db")
for d in (DATA, JOBS, BATCHES):
    os.makedirs(d, exist_ok=True)

log = logging.getLogger("korrektor")
logging.getLogger("httpx").setLevel(logging.WARNING)          # иначе в журнал попадает адрес с токеном бота Telegram
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
_lock = threading.RLock()            # повторный вход: load_settings может сохранить миграцию


SETTINGS_VERSION = 13


def _migrate(st):
    """Новые значения по умолчанию для сохраненных настроек прежних версий (выполняется один раз)."""
    v = int(st.get("_version") or 0)
    if v < 13:
        fm = list(st.get("report_formats") or [])
        if "docx_review" not in fm:                     # 1.3: правки в исходном Word
            st["report_formats"] = fm + ["docx_review"]
    st["_version"] = SETTINGS_VERSION
    return st


def load_settings():
    st = dict(DEFAULTS)
    saved = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            saved = json.load(open(SETTINGS_FILE, encoding="utf-8"))
        except Exception:  # noqa
            log.exception("settings.json поврежден, используются значения по умолчанию")
    st.update(saved)
    if saved and int(saved.get("_version") or 0) < SETTINGS_VERSION:
        _migrate(st)
        try:
            save_settings(st)
        except OSError:
            pass
    return st


def update_settings(fn):
    """Чтение → изменение → запись настроек под блокировкой (параллельные изменения не теряются)."""
    with _lock:
        st = load_settings()
        fn(st)
        clean = {k: v for k, v in st.items() if k in SCHEMA or k == "_version"}
        tmp = SETTINGS_FILE + ".tmp"
        json.dump(clean, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SETTINGS_FILE)
        return st


def save_settings(values):
    with _lock:
        clean = {k: v for k, v in values.items() if k in SCHEMA or k == "_version"}
        clean["_version"] = SETTINGS_VERSION
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
    from . import users
    users.init()
    old = os.path.join(APP_DIR, "prompts", "custom_prompt.md")     # перенос из версий до 1.2
    if os.path.exists(old) and not os.path.exists(CUSTOM_PROMPT):
        try:
            shutil.copy(old, CUSTOM_PROMPT)
        except OSError:
            log.warning("не удалось перенести custom_prompt.md в папку данных")
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, name TEXT, filename TEXT, created TEXT,
            status TEXT, progress REAL DEFAULT 0, step TEXT DEFAULT '', pages INTEGER DEFAULT 0,
            findings INTEGER DEFAULT 0, verify_ok INTEGER DEFAULT 0, verify_score TEXT DEFAULT '',
            error TEXT DEFAULT '', size INTEGER DEFAULT 0, settings TEXT DEFAULT '{}', finished TEXT)""")
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("started", "TEXT"), ("owner", "TEXT DEFAULT ''"), ("author", "TEXT DEFAULT ''"),
                         ("department", "TEXT DEFAULT ''"), ("doc_type", "TEXT DEFAULT ''")):   # миграции 1.0–1.2
            if col not in cols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch)")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)")
        c.execute("UPDATE jobs SET status='queued', progress=0 WHERE status='running'")


def job_dir(jid):
    return os.path.join(JOBS, str(jid))


def get_job(jid):
    with db() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return dict(r) if r else None


def list_jobs(limit=500, batch=None, owner=None):
    """owner — ограничение видимости: логин (только его задания) или users.Scope (свои + своего отдела)."""
    where, args = [], []
    scope = owner if owner is not None and not isinstance(owner, str) else None
    if batch:
        where.append("batch=?")
        args.append(batch)
    if isinstance(owner, str):
        where.append("owner=?")
        args.append(owner)
    sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
    with db() as c:
        if batch:
            rows = c.execute(sql + " ORDER BY id", args).fetchall()
        elif scope is not None:
            rows = c.execute(sql + " ORDER BY id DESC", args).fetchall()
        else:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
    rows = [dict(r) for r in rows]
    if scope is not None:
        rows = [r for r in rows if scope.ok(r.get("owner"), r.get("department"))]
        if not batch:
            rows = rows[:limit]
    return rows


def update_job(jid, **kw):
    if not kw:
        return
    keys = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*kw.values(), jid))


def create_job(batch, name, filename, size, settings, owner="", author="", department="", doc_type=""):
    with db() as c:
        cur = c.execute("INSERT INTO jobs(batch,name,filename,created,status,size,settings,owner,author,department,"
                        "doc_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (batch, name, filename, dt.datetime.now().isoformat(timespec="seconds"), "uploading", size,
                         json.dumps(settings, ensure_ascii=False), owner, author, department, doc_type))
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
def make_token(user="admin", ttl=86400 * 14, stamp=""):
    """Подписанный сеанс: логин | срок | отпечаток учетной записи (меняется при смене пароля) | подпись."""
    exp = str(int(time.time()) + ttl)
    sig = hmac.new(SECRET.encode(), f"{user}|{exp}|{stamp}".encode(), hashlib.sha256).hexdigest()
    return f"{user}|{exp}|{stamp}|{sig}"


def check_token(tok):
    """→ (логин, отпечаток) или None."""
    try:
        user, exp, stamp, sig = tok.split("|")
    except (ValueError, AttributeError):
        return None
    good = hmac.new(SECRET.encode(), f"{user}|{exp}|{stamp}".encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(sig.encode(), good.encode()) and exp.isdigit() and int(exp) > time.time():
        return user, stamp
    return None


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
    from .pipeline import office
    if not os.path.exists(os.path.join(d, "source.pdf")) and not office.find_source(d):
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
    try:
        from . import notify
        notify.job_event_async(jid)
    except Exception:  # noqa
        log.exception("уведомления")
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


PAUSED = threading.Event()          # пауза очереди: восстановление из резервной копии
BUSY = set()                        # задания, которые обрабатываются прямо сейчас


def worker_loop():
    while True:
        jid = None
        if PAUSED.is_set():
            time.sleep(1)
            continue
        try:
            with _lock:
                with db() as c:
                    r = None if PAUSED.is_set() else \
                        c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                    if r and c.execute("UPDATE jobs SET status='running' WHERE id=? AND status='queued'",
                                       (r["id"],)).rowcount:
                        jid = r["id"]
        except sqlite3.Error:
            log.exception("очередь заданий")
            time.sleep(5)
            continue
        if jid:
            BUSY.add(jid)
            try:
                _process(jid)
            except Exception:  # noqa — поток-обработчик не должен завершаться
                log.exception("обработчик: задание %s", jid)
                try:
                    update_job(jid, status="failed", error="внутренняя ошибка обработчика", step="Ошибка")
                except Exception:  # noqa
                    pass
            finally:
                BUSY.discard(jid)
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
            from . import users
            users.cleanup_audit(int(load_settings().get("audit_days") or 365))
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
    from . import backup, notify
    threading.Thread(target=notify.digest_loop, daemon=True).start()
    threading.Thread(target=notify.remind_loop, daemon=True).start()
    from . import report_pdf
    report_pdf.start()
    threading.Thread(target=backup.loop, daemon=True).start()


def new_batch_id():
    return uuid.uuid4().hex[:10]
