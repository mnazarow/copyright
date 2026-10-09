# -*- coding: utf-8 -*-
"""Журнал обработки в реальном времени, кнопка «Остановить», этапы с длительностью, страницы ошибок."""
import time

import pytest
from fastapi.testclient import TestClient

from app import core
from app.main import app, read_log
from app.pipeline import llm
from conftest import make_pdf
from test_ai import Fake

O = {"Origin": "http://testserver"}
AI_KEYS = ("ai_provider", "ai_base_url", "ai_model", "ai_timeout", "ai_heartbeat", "ai_retries", "mode",
           "ai_pages_per_request", "ai_stall_timeout", "ai_parallel")


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://testserver") as c:
        assert c.post("/login", data={"login": "admin", "password": "тест-пароль"}, headers=O,
                      follow_redirects=False).status_code == 303
        yield c


@pytest.fixture()
def ai_fake(monkeypatch):
    monkeypatch.delenv("KORREKTOR_AI_MOCK", raising=False)
    monkeypatch.setattr(llm, "BACKOFF", 0.05)
    saved = {k: v for k, v in core.load_settings().items() if k in AI_KEYS}
    f = Fake()
    yield f
    f.stop()
    core.update_settings(lambda s: s.update(saved))


def upload(c, path, name="Журнал.pdf"):
    with open(path, "rb") as fh:
        r = c.post("/api/jobs", files={"file": (name, fh, "application/pdf")}, headers=O)
    assert r.status_code == 200, r.text
    return r.json()["jobs"][0]


def follow(c, jid, until, timeout=60):
    """Читает журнал как страница задания (offset), пока until(текст, ответ) не станет истинным."""
    off, text, j = 0, "", None
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/jobs/{jid}/log?offset={off}").json()
        text += j["text"]
        off = j["offset"]
        if until(text, j):
            return text, j
        time.sleep(0.3)
    raise AssertionError("не дождались: " + text[-2000:])


def test_live_log_heartbeat_and_cancel(client, ai_fake, tmp_path):
    ai_fake.script.extend([("hang", 60)] * 6)
    core.update_settings(lambda s: s.update(ai_provider="openai", ai_base_url=ai_fake.url + "/v1", ai_model="m",
                                            ai_timeout=40, ai_heartbeat=5, ai_retries=0, mode="auto"))
    pdf = make_pdf(str(tmp_path / "a.pdf"), pages=2)
    jid = upload(client, pdf)
    jid2 = upload(client, pdf, "Второй.pdf")                         # ждет в очереди за первым (один обработчик)
    text, j = follow(client, jid, lambda t, j: "ждем начала ответа" in t)
    assert "▶ Извлечение текста" in text and "✓ Правила" in text and "ИИ ▶ часть 1/1" in text
    assert j["status"] == "running" and j["idle"] is not None and j["idle"] < 30
    assert "ИИ:" in j["step"]                                         # строка состояния — ход ожидания модели
    r = client.post(f"/api/jobs/{jid2}/cancel", headers=O).json()
    assert r == {"ok": True, "result": "stopped"}                    # из очереди — сразу
    r = client.post(f"/api/jobs/{jid}/cancel", headers=O).json()
    assert r["result"] == "stopping"
    t0 = time.time()
    text2, j = follow(client, jid, lambda t, j: j["status"] in ("done", "failed"), timeout=20)
    assert time.time() - t0 < 10 and ai_fake.closed                  # запрос к модели прерван с разрывом соединения
    assert j["status"] == "failed" and j["error"] == "остановлено пользователем"
    assert "■ Остановлено пользователем на этапе «ИИ: вычитка смысла" in text + text2
    j2 = client.get(f"/api/jobs/{jid2}").json()
    assert j2["status"] == "failed" and "до начала" in j2["error"]
    assert client.post(f"/api/jobs/{jid}/cancel", headers=O).status_code == 409   # уже завершено
    page = client.get(f"/job/{jid}", headers={"Accept": "text/html"}).text
    assert "Проверка остановлена" in page and "Проверить заново" in page
    audit = client.get("/admin/audit").text
    assert "остановка задания" in audit


def test_ai_failure_does_not_fail_job(client, ai_fake, tmp_path):
    ai_fake.script.append(("status", 401, {"error": {"message": "bad key"}}))
    core.update_settings(lambda s: s.update(ai_provider="openai", ai_base_url=ai_fake.url + "/v1", ai_model="m",
                                            ai_timeout=30, ai_retries=0, mode="auto", ai_pages_per_request=1,
                                            ai_parallel=1))
    jid = upload(client, make_pdf(str(tmp_path / "b.pdf"), pages=3))
    text, j = follow(client, jid, lambda t, j: j["status"] in ("done", "failed"), timeout=120)
    assert j["status"] == "done", j["error"]
    assert "ИИ ⛔" in text and "ИИ ⏭" in text
    st = core.load_state(jid)
    assert st["ai"]["failed"] and len(st["ai"]["skipped"]) == 2
    assert any("не проверены" in h for h in st["meta"]["header_lines"])
    assert st["verify"]["ok"], st["verify"]                          # строка в шапке не ломает 25 проверок
    page = client.get(f"/job/{jid}", headers={"Accept": "text/html"}).text
    assert "Языковая модель проверила не все страницы" in page


def test_read_log_chunks(tmp_path):
    jid = 987654
    d = core.job_dir(jid)
    import os
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "job.log"), "w", encoding="utf-8") as fh:
        fh.write("строка один\nстрока два\nнедописан")
    r = read_log(jid, 0)
    assert r["text"] == "строка один\nстрока два\n" and not r["reset"]
    r2 = read_log(jid, r["offset"])
    assert r2["text"] == ""                                          # неполная строка — в следующий раз
    assert read_log(jid, 10 ** 6)["reset"]                           # журнал начат заново
    tail = read_log(jid, -1, limit=16)
    assert tail["truncated"] and tail["text"] == ""                   # хвост начался посреди недописанной строки
    tail = read_log(jid, -1, limit=40)
    assert tail["text"] == "строка два\n"                             # с начала ближайшей целой строки
    core.delete_job(jid)


def test_error_pages(client):
    r = client.get("/job/999999", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "Не найдено" in r.text and "<html" in r.text.lower()
    r = client.get("/api/jobs/999999")
    assert r.status_code == 404 and r.json()["error"]
    r = client.get("/api/jobs/1/log?offset=abc")
    assert r.status_code == 422 and "«offset» — нужно целое число" in r.json()["error"]
    r = client.get("/job/1/delete", headers={"Accept": "text/html"})
    assert r.status_code == 405 and "кнопкой" in r.text


def test_unhandled_error_has_id(monkeypatch):
    with TestClient(app, base_url="http://testserver", raise_server_exceptions=False) as c:
        c.post("/login", data={"login": "admin", "password": "тест-пароль"}, headers=O)
        monkeypatch.setattr(core, "list_jobs", lambda *a, **k: 1 / 0)
        r = c.get("/api/jobs")
        assert r.status_code == 500 and r.json()["error_id"] and "Номер ошибки" in r.json()["error"]
        r = c.get("/jobs", headers={"Accept": "text/html"})
        assert r.status_code == 500 and "Номер ошибки" in r.text
