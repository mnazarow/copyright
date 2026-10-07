# -*- coding: utf-8 -*-
"""Пользователи и роли, журнал, сравнение версий, документы Word, уведомления, цели и рекомендации, метрики."""
import http.server
import json
import os
import shutil
import socketserver
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import core, notify, users
from app.main import app
from app.pipeline import office
from conftest import TEXT, make_pdf

O = {"Origin": "http://testserver"}


@pytest.fixture(scope="module")
def admin():
    with TestClient(app, base_url="http://testserver") as c:
        r = c.post("/login", data={"login": "admin", "password": "тест-пароль"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


def wait(c, jid, timeout=240):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(1)
    raise AssertionError("задание не завершилось")


def upload(c, path, name, **form):
    with open(path, "rb") as fh:
        r = c.post("/api/jobs", files={"file": (name, fh)}, data=form, headers=O)
    assert r.status_code == 200, r.text
    return r.json()["jobs"][0]


def test_password_hashing():
    h = users.hash_pw("Пароль123")
    assert users.verify_pw("Пароль123", h) and not users.verify_pw("пароль123", h)
    assert users.pw_problem("short1") and users.pw_problem("абвгдежзик") and not users.pw_problem("Корректор2026")


def test_roles_and_audit(admin):
    r = admin.post("/admin/users/save", data={"login": "avtor", "name": "Автор Тест", "role": "author",
                                               "password": "Avtor12345", "active": "1", "email": "a@test.local",
                                               "department": "Маркетинг"}, headers=O, follow_redirects=False)
    assert "saved=avtor" in r.headers["location"]
    r = admin.post("/admin/users/save", data={"login": "korr", "role": "corrector", "password": "Korr123456", "active": "1"},
                   headers=O, follow_redirects=False)
    assert "saved=korr" in r.headers["location"]
    bad = admin.post("/admin/users/save", data={"login": "x1", "role": "author", "password": "123"}, headers=O, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    with TestClient(app, base_url="http://testserver") as a:
        assert a.post("/login", data={"login": "avtor", "password": "неверно"}, follow_redirects=False).headers["location"].startswith("/login?error")
        assert a.post("/login", data={"login": "avtor", "password": "Avtor12345"}, follow_redirects=False).status_code == 303
        assert a.get("/settings").status_code == 403
        assert a.get("/admin/users").status_code == 403
        assert a.get("/analytics").status_code == 200
        assert a.post("/api/analytics/bulk", json={"status": "accepted", "items": []}, headers=O).status_code == 403
        assert "Автор Тест" in a.get("/account").text
    with TestClient(app, base_url="http://testserver") as k:
        k.post("/login", data={"login": "korr", "password": "Korr123456"})
        assert k.post("/api/analytics/bulk", json={"status": "accepted", "items": []}, headers=O).status_code == 200
        assert k.get("/settings").status_code == 403
    page = admin.get("/admin/audit").text
    assert "пользователь сохранен" in page and "неудачный вход" in page
    assert admin.get("/admin/audit/export").content.startswith("﻿".encode())
    # отключенный пользователь не входит
    admin.post("/admin/users/save", data={"login": "korr", "role": "corrector", "active": ""}, headers=O)
    with TestClient(app, base_url="http://testserver") as k:
        r = k.post("/login", data={"login": "korr", "password": "Korr123456"}, follow_redirects=False)
        assert "error=disabled" in r.headers["location"]


def test_author_owner_can_delete_own(admin, sample_pdf):
    with TestClient(app, base_url="http://testserver") as a:
        a.post("/login", data={"login": "avtor", "password": "Avtor12345"})
        jid = upload(a, sample_pdf, "Свой.pdf")
        job = core.get_job(jid)
        assert job["owner"] == "avtor" and job["department"] == "Маркетинг" and job["author"] == "Автор Тест"
        wait(a, jid)
        other = upload(admin, sample_pdf, "Чужой.pdf")
        wait(admin, other)
        assert a.post(f"/job/{other}/delete", headers=O).status_code == 403
        assert a.post(f"/job/{jid}/delete", headers=O, follow_redirects=False).status_code == 303
        assert core.get_job(jid) is None


def test_compare_versions(admin, tmp_path):
    v1 = make_pdf(str(tmp_path / "Каталог_v1.pdf"))
    fixed = [t.replace("10-20", "10–20").replace("насосами.Оборудование", "насосами. Оборудование") for t in TEXT]
    fixed.append("Новая строка с ошибкой 5-7 мм.")
    v2 = make_pdf(str(tmp_path / "Каталог_v2.pdf"), lines=fixed)
    a = upload(admin, v1, "Каталог_v1.pdf")
    wait(admin, a)
    b = upload(admin, v2, "Каталог_v2.pdf")
    wait(admin, b)
    r = admin.get(f"/api/compare?a={a}&b={b}").json()
    assert r["same_family"]
    assert r["fixed"] >= 2 and r["remain"] >= 1 and r["new"] >= 1
    assert any(row[0] == "исправлено" and "10-20" in row[4] for row in r["details"])
    html = admin.get(f"/compare?job={b}").text
    assert "Исправлено" in html and f"№ {a}" in html
    assert admin.get(f"/compare/export?a={a}&b={b}").status_code == 200


@pytest.mark.skipif(not office.soffice(), reason="LibreOffice не установлен")
def test_word_upload(admin, tmp_path):
    from docx import Document
    d = Document()
    for t in TEXT:
        p = d.add_paragraph(t)
        for r in p.runs:
            r.font.name = "Liberation Serif"
    path = tmp_path / "Паспорт.docx"
    d.save(path)
    jid = upload(admin, str(path), "Паспорт.docx", author="Тест", department="Сервис")
    j = wait(admin, jid)
    assert j["status"] == "done", j.get("error")
    rules = {f["rule"] for f in j["findings_list"]}
    assert {"range", "dot"} <= rules, rules
    assert admin.get(f"/job/{jid}/file/source").content[:2] == b"PK"


class _Sink(socketserver.StreamRequestHandler):
    """Простейший SMTP-приемник для теста."""
    box = []

    def handle(self):
        w = lambda s: self.wfile.write((s + "\r\n").encode())
        w("220 test")
        data = False
        buf = []
        while True:
            line = self.rfile.readline().decode("utf-8", "replace")
            if not line:
                return
            if data:
                if line.rstrip("\r\n") == ".":
                    _Sink.box.append("".join(buf))
                    data = False
                    w("250 ok")
                else:
                    buf.append(line)
                continue
            cmd = line[:4].upper()
            if cmd in ("EHLO", "HELO"):
                w("250 test")
            elif cmd == "DATA":
                data = True
                w("354 go")
            elif cmd == "QUIT":
                w("221 bye")
                return
            else:
                w("250 ok")


class _Hook(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Hook.got.append((dict(self.headers), json.loads(body)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_notifications(admin, sample_pdf):
    smtp = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Sink)
    hook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    for srv in (smtp, hook):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    st = core.load_settings()
    st.update(email_enabled=True, smtp_host="127.0.0.1", smtp_port=smtp.server_address[1], smtp_security="none",
              smtp_from="korrektor@test.local", notify_to="boss@test.local",
              webhook_url=f"http://127.0.0.1:{hook.server_address[1]}/hook", webhook_secret="s3cret",
              notify_events=["done", "failed", "goal"], goal_density=0.5, goal_alert=True, service_url="http://k.local")
    core.save_settings(st)
    try:
        res = notify.send_test(core.load_settings())
        assert res == {"почта": "ok", "вебхук": "ok"}, res
        jid = upload(admin, sample_pdf, "Уведомление.pdf")
        wait(admin, jid)
        for _ in range(30):
            if len(_Hook.got) >= 3:
                break
            time.sleep(0.5)
        events = [p["event"] for _, p in _Hook.got]
        assert "done" in events and "goal" in events, events
        h, p = [x for x in _Hook.got if x[1]["event"] == "done"][-1]
        import hashlib
        import hmac
        body = json.dumps(p, ensure_ascii=False).encode()
        assert h["X-Korrektor-Signature"] == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert p["url"] == f"http://k.local/job/{jid}" and p["goal_exceeded"]
        assert any("Subject:" in m and "boss@test.local" in m for m in _Sink.box)
        assert notify.digest_text(core.load_settings())[0].startswith("Документов:")
    finally:
        st = core.load_settings()
        st.update(email_enabled=False, webhook_url="", goal_density=5.0)
        core.save_settings(st)
        smtp.shutdown()
        hook.shutdown()


def test_goals_recommendations_metrics(admin):
    a = admin.get("/api/analytics").json()
    assert set(a["goals"]) == {"density", "author_share", "precision", "verify_rate"}
    assert isinstance(a["recommendations"], list)
    assert any(r["key"] for r in a["by_author"])
    r = admin.post("/settings/apply", json={"key": "hanging_check", "value": False}, headers=O)
    assert r.json()["value"] is False
    assert admin.post("/settings/apply", json={"key": "smtp_host", "value": "x"}, headers=O).status_code == 400
    admin.post("/settings/apply", json={"key": "hanging_check", "value": True}, headers=O)
    assert admin.get("/metrics").status_code == 401           # клиент теста — не 127.0.0.1
    tok = admin.post("/settings/token", headers=O).json()["token"]
    m = admin.get("/metrics", headers={"Authorization": f"Bearer {tok}"})
    assert m.status_code == 200 and 'korrektor_jobs{status="done"}' in m.text
    assert admin.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    for x in ("Пользователи", "Журнал"):
        assert x in admin.get("/").text


def test_extract_merges_fragmented_lines(tmp_path):
    """Строка, разбитая на куски разными шрифтами (как в PDF из Word с подменой шрифта), собирается обратно."""
    import pymupdf
    from conftest import FONT
    doc = pymupdf.open()
    page = doc.new_page()
    kw = {"fontfile": FONT, "fontname": "dv"} if FONT else {}
    tw = pymupdf.TextWriter(page.rect)
    font = pymupdf.Font(fontfile=FONT) if FONT else pymupdf.Font("helv")
    tw.append((60, 100), "Диапазон давления", font=font, fontsize=11)
    x = 60 + font.text_length("Диапазон давления ", fontsize=11)
    tw.append((x, 100), "10-20", font=pymupdf.Font("helv"), fontsize=11)
    tw.write_text(page)
    page.insert_text((60, 140), "Обычная строка текста", fontsize=11, **kw)
    p = tmp_path / "frag.pdf"
    doc.save(p)
    import subprocess
    import sys
    from app.pipeline.run import SCRIPTS
    subprocess.run([sys.executable, os.path.join(SCRIPTS, "extract.py"), str(p)], cwd=tmp_path, check=True,
                   capture_output=True)
    dump = json.load(open(tmp_path / "dump.json", encoding="utf-8"))
    texts = [ln["text"] for ln in dump["lines"][0]["lines"]]
    assert any("Диапазон давления 10-20" in t for t in texts), texts
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_sessions_end_after_password_change(admin):
    admin.post("/admin/users/save", data={"login": "sess", "role": "author", "password": "Sess123456", "active": "1"},
               headers=O)
    with TestClient(app, base_url="http://testserver") as a, TestClient(app, base_url="http://testserver") as b:
        a.post("/login", data={"login": "sess", "password": "Sess123456"})
        b.post("/login", data={"login": "sess", "password": "Sess123456"})
        assert b.get("/api/jobs").status_code == 200
        r = a.post("/account/password", data={"old": "Sess123456", "new": "Novyi12345", "new2": "Novyi12345"},
                   headers=O, follow_redirects=False)
        assert "saved=1" in r.headers["location"]
        assert a.get("/api/jobs").status_code == 200            # текущий сеанс получил новый ключ
        assert b.get("/api/jobs").status_code == 401            # прежний сеанс (украденная cookie) закрыт
    # пересоздание пользователя с тем же логином не открывает старые сеансы
    with TestClient(app, base_url="http://testserver") as c:
        c.post("/login", data={"login": "sess", "password": "Novyi12345"})
        admin.post("/admin/users/delete", data={"login": "sess"}, headers=O)
        admin.post("/admin/users/save", data={"login": "sess", "role": "admin", "password": "Other12345", "active": "1"},
                   headers=O)
        assert c.get("/api/jobs").status_code == 401


def test_audit_export_formula_safe(admin):
    with TestClient(app, base_url="http://testserver") as x:
        x.post("/login", data={"login": "=HYPERLINK(1)", "password": "x"})
    csv = admin.get("/admin/audit/export").text
    assert "'=HYPERLINK(1)" in csv and ";=HYPERLINK" not in csv


def test_bad_email_rejected(admin):
    r = admin.post("/admin/users/save", data={"login": "mailx", "role": "author", "password": "Mail123456", "active": "1",
                                               "email": "a@x.ru, b@y.ru"}, headers=O, follow_redirects=False)
    assert "error=" in r.headers["location"]
