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

from .pipeline.common import dur
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
    for k, v in saved.items():                        # значения проверяются по схеме: испорченный файл не роняет проверки
        if k in SCHEMA:
            try:
                st[k] = coerce(k, v)
            except Exception:  # noqa
                st[k] = DEFAULTS[k]
        else:
            st[k] = v
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
        clean = {k: v for k, v in st.items() if k in SCHEMA}
        clean["_version"] = SETTINGS_VERSION
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
        vals = list(raw) if isinstance(raw, (list, tuple)) else [raw] if raw else []
        allowed = {o[0] for o in s.get("options") or []}
        return [v for v in vals if isinstance(v, str) and (not allowed or v in allowed)]
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
                         ("department", "TEXT DEFAULT ''"), ("doc_type", "TEXT DEFAULT ''"),   # миграции 1.0–1.2
                         ("beat", "REAL DEFAULT 0")):          # 1.6: время последних признаков работы задания
            if col not in cols:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch)")
        c.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status)")
        c.execute("UPDATE jobs SET status='queued', progress=0 WHERE status='running'")
        c.execute("UPDATE jobs SET status='failed', error='загрузка прервана перезапуском службы', step='Ошибка' "
                  "WHERE status='uploading'")
    from . import workflow
    workflow.init()


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


def claim_idle(jid, status):
    """Атомарно переводит задание, которое сейчас не обрабатывается, в служебное состояние (удаление, перезапуск):
    обработчик не успеет взять его в работу между проверкой и действием. → True, если удалось."""
    with _lock:
        with db() as c:
            return c.execute("UPDATE jobs SET status=? WHERE id=? AND status IN ('done', 'failed', 'queued')",
                             (status, jid)).rowcount == 1


def delete_job(jid):
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    with db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
        try:
            c.execute("DELETE FROM job_events WHERE job=?", (jid,))   # история согласования удаленного задания
        except sqlite3.OperationalError:
            pass


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


def explain_error(e):
    """Понятное описание ошибки обработки (для страницы задания и журнала)."""
    import errno
    import subprocess
    if isinstance(e, MemoryError):
        return ("не хватило оперативной памяти сервера — уменьшите «Разрешение изображений страниц» или число "
                "обработчиков, либо проверяйте документ частями")
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "на диске сервера закончилось место — удалите старые задания или увеличьте диск"
    if isinstance(e, PermissionError):
        return f"нет прав на файл {e.filename or ''}: проверьте владельца папки данных ({DATA})".replace("  ", " ")
    if isinstance(e, subprocess.TimeoutExpired):
        return f"этап не уложился в {int(e.timeout or 0)} с и был прерван"
    if isinstance(e, sqlite3.OperationalError) and "locked" in str(e):
        return "база данных занята другой операцией (восстановление из копии?) — перезапустите проверку"
    text = str(e).strip()
    return text if text else e.__class__.__name__


CANCEL = set()                      # задания, которые пользователь попросил остановить


def cancel_job(jid):
    """Остановка задания: из очереди — сразу, выполняющееся — при ближайшей проверке (обычно 1–2 секунды; запрос
    к модели прерывается с разрывом соединения). → 'stopped', 'stopping' или None (задание уже завершено)."""
    with _lock:
        with db() as c:
            if c.execute("UPDATE jobs SET status='failed', error=?, step='Остановлено' WHERE id=? AND status='queued'",
                         ("остановлено пользователем до начала проверки", jid)).rowcount:
                return "stopped"
            row = c.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row or row["status"] != "running":
                return None
            if jid not in BUSY:             # «выполняется» в базе, но обработчика нет — сразу помечается остановленным
                c.execute("UPDATE jobs SET status='failed', error=?, step='Остановлено' WHERE id=?",
                          ("остановлено пользователем (обработчик задания не найден)", jid))
                return "stopped"
            CANCEL.add(jid)
            return "stopping"


def _process(jid):
    from .pipeline import run
    from .pipeline.common import Cancelled
    from . import batch as batchmod
    job = get_job(jid)
    if not job:
        return
    d = job_dir(jid)
    try:
        os.makedirs(d, exist_ok=True)
        logf = open(os.path.join(d, "job.log"), "a", encoding="utf-8")
    except OSError as e:
        update_job(jid, status="failed", error="журнал задания не открывается: " + explain_error(e), step="Ошибка")
        return
    wlock = threading.Lock()
    beat = {"t": 0.0, "line": time.time()}
    t_start = time.time()
    stage = {"name": "Запуск", "t": t_start}
    timing = []                                      # длительность этапов → timing.json (для аналитики)

    def touch(now=None):
        now = now or time.time()
        if now - beat["t"] >= 3:                     # признак работы — не чаще раза в 3 секунды
            beat["t"] = now
            try:
                update_job(jid, beat=now)
            except sqlite3.Error:
                pass

    def jlog(msg):
        line = f"{dt.datetime.now():%H:%M:%S} {msg}"
        beat["line"] = time.time()
        with wlock:
            try:
                logf.write(line + "\n")
                logf.flush()
            except (OSError, ValueError):             # диск переполнен или журнал уже закрыт — задание не падает
                pass
        log.info("job %s: %s", jid, msg)
        touch()

    def should_stop():
        return jid in CANCEL

    def progress(p, step=""):
        if jid in CANCEL:
            raise Cancelled()
        now = time.time()
        if step and step != stage["name"]:
            jlog(f"  ✓ {stage['name']} — {dur(now - stage['t'])}")
            if step != "Готово":
                jlog(f"▶ {step}")
            stage.update(name=step, t=now)
            timing.append([step, now])
        beat["t"] = now
        update_job(jid, progress=round(p, 3), step=step, beat=now)

    def status(msg):
        """Строка состояния без записи в журнал (ход ожидания ответа модели)."""
        if jid in CANCEL:
            return
        now = time.time()
        beat["t"] = now
        try:
            update_job(jid, step=str(msg)[:300], beat=now)
        except sqlite3.Error:
            pass

    quiet = threading.Event()

    def watchdog():
        """Долгий этап без записей в журнал (словарь на тысяче страниц, сборка отчета): раз в минуту — строка
        «этап идет N мин», чтобы было видно, на чем задание и сколько оно на этом этапе."""
        while not quiet.wait(5):
            now = time.time()
            if now - beat["line"] >= 60:
                jlog(f"  … этап «{stage['name']}» идет {dur(now - stage['t'])}")

    update_job(jid, status="running", progress=0.01, step="Запуск", beat=t_start,
               started=dt.datetime.now().isoformat(timespec="seconds"), error="")
    threading.Thread(target=watchdog, daemon=True, name=f"job-{jid}-watchdog").start()
    try:
        st = load_settings()
        try:
            st.update(json.loads(job.get("settings") or "{}"))
        except ValueError:
            jlog("⚠ настройки задания повреждены — используются общие настройки")
        from .pipeline import office
        if not os.path.exists(os.path.join(d, "source.pdf")) and not office.find_source(d):
            raise RuntimeError("исходный файл задания не найден (удален или не восстановлен из копии)")
        prov = st.get("ai_provider")
        use_ai = st.get("mode") == "rules_ai" or (st.get("mode", "auto") == "auto" and prov not in (None, "", "none"))
        jlog(f"■ Задание № {jid}: «{job.get('filename')}», " + f"{(job.get('size') or 0) / 2 ** 20:.1f}".replace(".", ",")
             + f" МБ — Корректор {VERSION}")
        jlog(f"Профиль «{st.get('profile')}», режим «{st.get('mode')}», проходы: {', '.join(st.get('passes') or []) or '—'}; "
             + (f"ИИ: {prov}, модель {st.get('ai_model')}" if use_ai and prov not in (None, "", "none")
                else "ИИ: не используется"))
        jlog(f"▶ {stage['name']}")
        state = run.process(d, job["name"], st, jlog, progress, jid, status=status, should_stop=should_stop)
        v = state["verify"]
        jlog(f"■ Готово за {dur(time.time() - t_start)}: замечаний в отчете {v.get('count', 0)}, машинная проверка "
             f"{v.get('passed', 0)}/{v.get('total', 0)}" + ("" if v.get("ok") else " — отчет не принят"))
        update_job(jid, status="done", progress=1, step="Готово", pages=state["pages"],
                   findings=v.get("count", 0), verify_ok=1 if v.get("ok") else 0,
                   verify_score=f"{v.get('passed', 0)}/{v.get('total', 0)}", error=v.get("error") or "",
                   finished=dt.datetime.now().isoformat(timespec="seconds"), beat=time.time())
        _save_timing(d, timing)
    except Cancelled:
        jlog(f"■ Остановлено пользователем на этапе «{stage['name']}» через {dur(time.time() - t_start)} после запуска")
        update_job(jid, status="failed", error="остановлено пользователем", step="Остановлено",
                   finished=dt.datetime.now().isoformat(timespec="seconds"))
    except BaseException as e:  # noqa — любая ошибка (и MemoryError) завершает задание с понятной причиной
        msg = explain_error(e)
        jlog(f"✗ ОШИБКА на этапе «{stage['name']}»: {msg}")
        jlog("Подробности для администратора:\n" + traceback.format_exc())
        try:
            update_job(jid, status="failed", error=f"{msg} (этап «{stage['name']}»)"[:500], step="Ошибка",
                       finished=dt.datetime.now().isoformat(timespec="seconds"))
        except Exception:  # noqa
            log.exception("задание %s: статус ошибки не записан", jid)
        if not isinstance(e, Exception):
            raise                                    # KeyboardInterrupt, SystemExit — не глотаются
    finally:
        quiet.set()
        CANCEL.discard(jid)
        with wlock:
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
                        BUSY.add(jid)                # под той же блокировкой, что и «Остановить» (cancel_job)
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
            finally:
                BUSY.discard(jid)
            continue
        _queue_event.wait(5)
        _queue_event.clear()


def expired_jobs(st, now=None):
    """Задания старше срока хранения. Документ на согласовании (взят в работу, возвращен, ждет утверждения)
    не удаляется, пока согласование не завершено."""
    days = int(st.get("retention_days") or 0)
    if not days:
        return []
    limit = ((now or dt.datetime.now()) - dt.timedelta(days=days)).isoformat()
    out = []
    for j in list_jobs(100000):
        if j["status"] not in ("done", "failed") or (j.get("finished") or j["created"]) >= limit:
            continue
        if st.get("workflow_enabled") and (j.get("wf_state") or "") in ("review", "returned", "approved"):
            continue
        out.append(j)
    return out


def cleanup_loop():
    while True:
        try:
            for j in expired_jobs(load_settings()):
                delete_job(j["id"])
                log.info("Удалено устаревшее задание %s", j["id"])
            from . import users
            users.cleanup_audit(int(load_settings().get("audit_days") or 365))
        except Exception:  # noqa
            log.exception("очистка")
        time.sleep(3600)


_started = False


def apply_log_level(st=None):
    """Настройка «Подробность журнала» (действует сразу после сохранения; KORREKTOR_LOG — только до запуска)."""
    lvl = (st or load_settings()).get("log_level") or "INFO"
    logging.getLogger().setLevel(getattr(logging, lvl, logging.INFO))
    log.setLevel(getattr(logging, lvl, logging.INFO))


def start_workers():
    global _started
    init_db()
    apply_log_level()
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
    from .pipeline import products
    threading.Thread(target=products.sync_loop, args=(load_settings, log), daemon=True).start()
    threading.Thread(target=backup.loop, daemon=True).start()


def new_batch_id():
    return uuid.uuid4().hex[:10]
