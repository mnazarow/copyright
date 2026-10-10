# -*- coding: utf-8 -*-
"""Устойчивость службы: задание в отдельном процессе (авария библиотеки или нехватка памяти не роняют сервис),
задание, на котором служба падала, не крутится по кругу, база в режиме WAL с закрытием соединений, регулярные
выражения без катастрофического перебора, тайм-ауты модели подряд останавливают проход ИИ."""
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import analytics, core
from app.main import app
from app.pipeline import llm
from conftest import make_pdf
from test_ai import Fake, dump, go, settings

O = {"Origin": "http://testserver"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://testserver") as c:
        assert c.post("/login", data={"login": "admin", "password": "тест-пароль"}, headers=O,
                      follow_redirects=False).status_code == 303
        yield c


def wait(c, jid, timeout=240):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.5)
    raise AssertionError(f"задание {jid} не завершилось")


def upload(c, path, name="Устойчивость.pdf"):
    with open(path, "rb") as fh:
        r = c.post("/api/jobs", files={"file": (name, fh, "application/pdf")}, headers=O)
    assert r.status_code == 200, r.text
    return r.json()["jobs"][0]


def test_job_runs_in_child_process(client, tmp_path):
    """Обработка идет не в процессе веб-сервера: в журнале задания — номер другого процесса."""
    import re
    jid = upload(client, make_pdf(str(tmp_path / "a.pdf")))
    assert wait(client, jid)["status"] == "done"
    log = client.get(f"/api/jobs/{jid}/log?offset=0").json()["text"]
    m = re.search(r"процесс (\d+)", log)
    assert m and int(m.group(1)) != os.getpid(), log[:500]


@pytest.mark.parametrize("sig,words", [(signal.SIGSEGV, "SIGSEGV"), (signal.SIGKILL, "памят")])
def test_child_crash_fails_only_the_job(client, tmp_path, sig, words):
    """Аварийное завершение процесса задания (сбой MuPDF, убит из-за нехватки памяти): задание — «Ошибка» с
    понятной причиной, служба отвечает, следующее задание обрабатывается."""
    real = core._spawn

    def crashing(args):
        if args[0] == "job":
            return subprocess.Popen([sys.executable, "-c", f"import os, signal; os.kill(os.getpid(), {int(sig)})"],
                                    start_new_session=True)
        return real(args)
    core._spawn = crashing
    try:
        jid = upload(client, make_pdf(str(tmp_path / "crash.pdf")))
        j = wait(client, jid)
    finally:
        core._spawn = real
    assert j["status"] == "failed" and words in j["error"], j
    assert client.get("/api/health").json()["status"] == "ok"
    log = client.get(f"/api/jobs/{jid}/log?offset=0").json()["text"]
    assert words in log
    jid2 = upload(client, make_pdf(str(tmp_path / "next.pdf")))
    assert wait(client, jid2)["status"] == "done"


def test_crash_loop_guard(client):
    """Задание, которое уже MAX_ATTEMPTS раз прерывалось падением службы, при запуске не ставится в очередь снова."""
    jid = core.create_job("", "петля", "петля.pdf", 1, {})
    core.update_job(jid, status="running", attempts=core.MAX_ATTEMPTS)
    jid2 = core.create_job("", "один раз", "один.pdf", 1, {})
    core.update_job(jid2, status="running", attempts=1)
    core.PAUSED.set()                                # обработчики не должны забрать задания во время проверки
    try:
        core.init_db()
        assert core.get_job(jid)["status"] == "failed" and "прерывалась" in core.get_job(jid)["error"]
        assert core.get_job(jid2)["status"] == "queued"
    finally:
        core.update_job(jid2, status="failed")
        core.PAUSED.clear()
        core.delete_job(jid)
        core.delete_job(jid2)


def test_db_wal_and_connections_closed():
    core.init_db()
    with core.db() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    with pytest.raises(sqlite3.ProgrammingError):    # соединение закрыто при выходе из блока
        c.execute("SELECT 1")
    fds = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    import gc
    gc.disable()
    try:
        for _ in range(300):
            with core.db() as c:
                c.execute("SELECT COUNT(*) FROM jobs").fetchone()
        if fds is not None:
            assert len(os.listdir("/proc/self/fd")) - fds < 20     # раньше — 300 открытых файлов базы
    finally:
        gc.enable()


def test_settings_cache_sees_changes():
    st = core.load_settings()
    st["whitelist"] = "изменено-без-сохранения"      # копия: изменения не попадают в кэш
    assert core.load_settings().get("whitelist") != "изменено-без-сохранения"
    old = core.load_settings().get("glossary") or ""
    core.update_settings(lambda s: s.__setitem__("glossary", old + "\nкэш: кеш"))
    assert "кэш: кеш" in core.load_settings()["glossary"]
    core.update_settings(lambda s: s.__setitem__("glossary", old))
    assert core.load_settings()["glossary"] == old


def test_state_lock_is_reentrant_and_cross_process(tmp_path):
    jid = core.create_job("", "блок", "блок.pdf", 1, {})
    os.makedirs(core.job_dir(jid), exist_ok=True)
    try:
        with core.state_lock(jid):
            with core.state_lock(jid):              # повторный вход в том же потоке
                code = ("import fcntl,sys; f=open(sys.argv[1],'a'); "
                        "sys.exit(0 if fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB) is None else 1)")
                r = subprocess.run([sys.executable, "-c", code, os.path.join(core.job_dir(jid), core.STATE_LOCK)],
                                   capture_output=True)
                assert r.returncode != 0              # другой процесс не может взять блокировку
        r = subprocess.run([sys.executable, "-c", code, os.path.join(core.job_dir(jid), core.STATE_LOCK)])
        assert r.returncode == 0                      # после выхода — свободна
    finally:
        core.update_job(jid, status="failed")
        core.delete_job(jid)


def test_family_of_long_digit_names_fast():
    t = time.time()
    for n in (30, 60, 200):
        analytics.family_of("Каталог_v" + "1" * n + "x")
        analytics.family_of("Прайс ред " + "2024 10 09 " * n + "x")
    assert time.time() - t < 1
    assert analytics.family_of("Каталог БТП v2 3") == analytics.family_of("Каталог БТП v5")
    assert analytics.family_of("Каталог_ред_12") == "каталог"


def test_parse_json_whitespace_run_fast():
    t = time.time()
    items, err = llm.parse_json('```json\n{"findings": [' + " " * 200000 + "]}\n```")
    assert items == [] and err is None
    assert time.time() - t < 1
    items, err = llm.parse_json("[" * 50000)          # глубокая вложенность — без RecursionError наружу
    assert items == []


def test_consecutive_timeouts_stop_ai_pass(monkeypatch):
    """Модель принимает запросы и молчит: проход ИИ останавливается после нескольких тайм-аутов подряд, а не
    ждет все попытки всех частей (раньше — часы, очередь заданий стояла)."""
    monkeypatch.delenv("KORREKTOR_AI_MOCK", raising=False)
    monkeypatch.setattr(llm, "BACKOFF", 0.01)
    f = Fake()
    try:
        f.script.extend([("hang", 30)] * 40)
        st = settings(f, ai_timeout=0.6, ai_retries=1, ai_pages_per_request=1)
        t = time.time()
        out, rep, lines = go(st, dump(20))
        assert out == []
        assert len(f.calls) <= 5, len(f.calls)
        assert any("не получили ответа ни с одной попытки" in ln for ln in lines)
        assert len(rep["skipped"]) >= 15
        assert time.time() - t < 30
    finally:
        f.stop()


def test_health_is_async():
    from app import main
    import inspect
    route = next(r for r in main.app.routes if getattr(r, "path", "") == "/api/health")
    assert inspect.iscoroutinefunction(route.endpoint)


def test_pdf_lock_serializes_web_rendering(client, tmp_path):
    """Миниатюры и страницы рисуются под общей блокировкой PyMuPDF: параллельные запросы не падают."""
    jid = upload(client, make_pdf(str(tmp_path / "lock.pdf"), pages=3))
    assert wait(client, jid)["status"] == "done"
    for fn in os.listdir(os.path.join(core.job_dir(jid), "pages")):
        os.remove(os.path.join(core.job_dir(jid), "pages", fn))
    errs = []

    def hit(i):
        try:
            r = client.get(f"/job/{jid}/crop/{1 + i % 3}.png?r=50,50,300,300")
            r2 = client.get(f"/job/{jid}/page/{1 + i % 3}.png")
            if r.status_code != 200 or r2.status_code != 200:
                errs.append((r.status_code, r2.status_code))
        except Exception as e:  # noqa
            errs.append(e)
    ts = [threading.Thread(target=hit, args=(i,)) for i in range(24)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs, errs
    leftovers = [f for f in os.listdir(os.path.join(core.job_dir(jid), "pages")) if ".tmp" in f]
    assert not leftovers


def test_service_stop_requeues_job(client, tmp_path):
    """Остановка службы (SIGTERM процессу задания, код 75): задание не «Ошибка», а снова в очереди и затем
    выполняется."""
    real = core._spawn
    calls = []

    def once_interrupted(args):
        if args[0] == "job" and not calls:
            calls.append(args)
            return subprocess.Popen([sys.executable, "-c", f"import sys; sys.exit({core.EXIT_INTERRUPTED})"],
                                    start_new_session=True)
        return real(args)
    core._spawn, saved_delay = once_interrupted, core.INTERRUPT_REQUEUE
    core.INTERRUPT_REQUEUE = 1
    try:
        jid = upload(client, make_pdf(str(tmp_path / "stop.pdf")))
        j = wait(client, jid)
    finally:
        core._spawn, core.INTERRUPT_REQUEUE = real, saved_delay
    assert calls and j["status"] == "done", j
    log = client.get(f"/api/jobs/{jid}/log?offset=0").json()["text"]
    assert "прервана остановкой службы" in log
    assert core.get_job(jid)["attempts"] == 1          # прерванная остановкой попытка не считается падением


def test_worker_sigterm_exits_75_and_kills_group(client, tmp_path):
    """SIGTERM процессу задания: выход с кодом 75, дочерние процессы (extract.py) завершены."""
    pdf = make_pdf(str(tmp_path / "long.pdf"), pages=400)
    jid = core.create_job("", "долгий", "долгий.pdf", os.path.getsize(pdf), {})   # «uploading»: очередь не берет
    os.makedirs(core.job_dir(jid), exist_ok=True)
    import shutil
    shutil.copy(pdf, os.path.join(core.job_dir(jid), "source.pdf"))
    proc = core._spawn(["job", jid])
    try:
        time.sleep(3)
        assert proc.poll() is None, "задание завершилось слишком быстро для проверки"
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(30) == core.EXIT_INTERRUPTED
        time.sleep(0.5)
        with pytest.raises(ProcessLookupError):
            os.killpg(proc.pid, 0)                      # в группе процесса задания никого не осталось
    finally:
        core._kill_group(proc)
        core.update_job(jid, status="failed")
        core.delete_job(jid)
