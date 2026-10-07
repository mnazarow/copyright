# -*- coding: utf-8 -*-
"""Сквозной тест: вход → загрузка PDF → отчет → правки → аналитика → тренды → выгрузки → безопасность."""
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import core
from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="http://testserver") as c:
        yield c


def login(c):
    r = c.post("/login", data={"password": "тест-пароль", "next": "/jobs"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert "korrektor" in r.cookies or c.cookies.get("korrektor")


def wait(c, jid, timeout=180):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(1)
    raise AssertionError("задание не завершилось")


def test_auth_required(client):
    assert client.get("/api/jobs").status_code == 401
    r = client.get("/analytics", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = client.post("/login", data={"password": "нет", "next": "//evil.com"}, follow_redirects=False)
    assert "evil" not in r.headers["location"].split("next=")[0]
    assert client.get("/api/health").json()["status"] == "ok"


def test_full_cycle(client, sample_pdf):
    login(client)
    with open(sample_pdf, "rb") as fh:
        r = client.post("/api/jobs", files={"file": ("Тестовый_каталог.pdf", fh, "application/pdf")},
                        headers={"Origin": "http://testserver"})
    assert r.status_code == 200, r.text
    jid = r.json()["jobs"][0]
    j = wait(client, jid)
    assert j["status"] == "done", j.get("error")
    assert j["verify"]["ok"], j["verify"]
    rules = {f["rule"] for f in j["findings_list"]}
    assert {"range", "dot", "dspace"} <= rules, rules
    assert rules & {"latin", "mixed"}, rules                 # «ГОСТ P» с латинской P
    docx = client.get(f"/api/jobs/{jid}/report?format=docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    assert client.get(f"/job/{jid}/file/source").content[:5] == b"%PDF-"
    assert client.get(f"/job/{jid}").status_code == 200

    # правка: отклонить замечание и пересобрать
    fid = j["findings_list"][0]["id"]
    r = client.post(f"/api/jobs/{jid}/findings/{fid}", json={"status": "rejected"}, headers={"Origin": "http://testserver"})
    assert r.json()["finding"]["status"] == "rejected"
    r = client.post(f"/api/jobs/{jid}/rebuild", json={}, headers={"Origin": "http://testserver"})
    assert r.json()["ok"] and r.json()["count"] == len(j["findings_list"]) - 1
    assert client.post(f"/api/jobs/{jid}/findings/{fid}", json=[1], headers={"Origin": "http://testserver"}).status_code == 400

    # аналитика и тренды
    a = client.get("/api/analytics").json()
    assert a["kpi"]["docs"] >= 1 and a["kpi"]["rejected"] >= 1
    assert sum(r["n"] for r in a["by_type"]) == a["kpi"]["findings"]
    assert client.get("/api/analytics?type=%D0%9B%D0%BE%D0%B3%D0%B8%D0%BA%D0%B0&period=7").status_code == 200
    t = client.get("/api/trends?gran=day&period=30").json()
    assert len(t["labels"]) == 30 and sum(t["series"]["docs"]) >= 1
    ex = client.get(f"/api/analytics/findings?job={jid}&limit=5").json()
    assert ex["total"] >= 3 and len(ex["rows"]) <= 5
    for u in ("/api/analytics/export?format=xlsx", "/api/analytics/export?format=csv",
              "/api/analytics/export?format=findings&as=xlsx", "/api/trends/export?format=xlsx",
              "/api/trends/export?format=json"):
        assert client.get(u).status_code == 200, u
    for page in ("/analytics", "/trends", "/settings", "/docs", "/jobs", "/"):
        assert client.get(page).status_code == 200, page

    # массовая отметка
    rows = ex["rows"][:2]
    r = client.post("/api/analytics/bulk", json={"status": "accepted", "items": [{"job": x["job"], "id": x["id"]} for x in rows]},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200 and sum(r.json()["changed"].values()) >= 1
    assert core.get_job(jid)["verify_ok"] == 1

    # белый список и глоссарий
    r = client.post("/api/analytics/whitelist", json={"words": ["Водокомфортный", "<script>"]}, headers={"Origin": "http://testserver"})
    assert r.json()["added"] == ["Водокомфортный"]
    r = client.post("/api/analytics/glossary", json={"good": "рым-болт", "bad": "рым болт"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert client.post("/api/analytics/glossary", json={"good": "a:b", "bad": "c"}, headers={"Origin": "http://testserver"}).status_code == 400

    # архив без непринятого отчета и удаление
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/job/{jid}/zip").content))
    assert any(n.endswith(".docx") for n in z.namelist())
    assert client.post("/job/99999/restart", headers={"Origin": "http://testserver"}).status_code == 404


def test_csrf_and_token(client):
    login(client)
    r = client.post("/settings/token", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    tok = client.post("/settings/token", headers={"Origin": "http://testserver"}).json()["token"]
    with TestClient(app) as anon:
        assert anon.get("/api/jobs", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
        r = anon.get("/settings/export?secrets_too=1", headers={"Authorization": f"Bearer {tok}"}, follow_redirects=False)
        assert r.status_code in (303, 403)                # токен API не открывает настройки


def test_upload_limits(client):
    login(client)
    bad = io.BytesIO(b"not a zip")
    r = client.post("/api/jobs", files={"file": ("a.zip", bad, "application/zip")}, headers={"Origin": "http://testserver"})
    assert r.status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.pdf", b"%PDF-" + b"0" * (650 * 1024 * 1024))
    r = client.post("/api/jobs", files={"file": ("bomb.zip", buf.getvalue(), "application/zip")}, headers={"Origin": "http://testserver"})
    assert r.status_code == 413
