# -*- coding: utf-8 -*-
"""Версия 1.3: правки в Word, «только свои документы», тип документа, единый вход OpenID Connect,
обучение на решениях, визуальное сравнение страниц, резервные копии."""
import base64
import http.server
import json
import os
import tarfile
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import backup, core
from app.main import app
from app.pipeline import office
from conftest import TEXT, make_pdf

O = {"Origin": "http://testserver"}


@pytest.fixture(scope="module")
def admin():
    with TestClient(app, base_url="http://testserver") as c:
        assert c.post("/login", data={"login": "admin", "password": "тест-пароль"}, follow_redirects=False).status_code == 303
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


def set_st(**kw):
    core.update_settings(lambda s: s.update(kw))


@pytest.mark.skipif(not office.soffice(), reason="LibreOffice не установлен")
def test_word_review_output(admin, tmp_path):
    from docx import Document
    d = Document()
    for t in TEXT:
        d.add_paragraph(t)
    path = tmp_path / "Рецензия.docx"
    d.save(path)
    jid = upload(admin, str(path), "Рецензия.docx")
    j = wait(admin, jid)
    assert j["status"] == "done" and "docx_review" in j["verify"]["files"], j["verify"]
    r = admin.get(f"/job/{jid}/file/docx_review")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    out = tmp_path / "out.docx"
    out.write_bytes(r.content)
    import zipfile
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode()
        assert "<w:del " in xml and "<w:ins " in xml and "commentRangeStart" in xml
        assert "word/comments.xml" in z.namelist()


def test_own_only_and_doc_type(admin, sample_pdf):
    for login in ("own1", "own2"):
        admin.post("/admin/users/save", data={"login": login, "role": "author", "password": "Own1234567", "active": "1"}, headers=O)
    set_st(authors_own_only=True, goal_density_by_type="Паспорт изделия: 0.1")
    try:
        with TestClient(app, base_url="http://testserver") as a, TestClient(app, base_url="http://testserver") as b:
            a.post("/login", data={"login": "own1", "password": "Own1234567"})
            b.post("/login", data={"login": "own2", "password": "Own1234567"})
            ja = upload(a, sample_pdf, "Мой_паспорт.pdf", doc_type="Паспорт изделия")
            wait(a, ja)
            assert core.get_job(ja)["doc_type"] == "Паспорт изделия"
            assert b.get(f"/job/{ja}").status_code == 404
            assert b.get(f"/api/jobs/{ja}").status_code == 404
            assert b.get(f"/job/{ja}/file/source").status_code == 404
            assert all(j["owner"] == "own2" for j in b.get("/api/jobs").json())
            assert b.get(f"/compare?a={ja}&b={ja}").status_code == 404
            assert b.get("/api/analytics").json()["kpi"]["docs"] == 0
            ka = a.get("/api/analytics").json()
            assert ka["kpi"]["docs"] >= 1 and any(r["key"] == "Паспорт изделия" for r in ka["by_doc_type"])
            assert ka["kpi"]["over_goal"] >= 1                 # своя цель для типа документа
            assert admin.get(f"/job/{ja}").status_code == 200   # администратор видит все
    finally:
        set_st(authors_own_only=False, goal_density_by_type="")


class _IdP(http.server.BaseHTTPRequestHandler):
    base = ""
    nonce = ""
    groups = ["korrektor-correctors"]
    login = "sso.user"
    sub = "uid-1"

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/.well-known/openid-configuration"):
            self._json({"issuer": _IdP.base, "authorization_endpoint": _IdP.base + "/auth",
                        "token_endpoint": _IdP.base + "/token", "userinfo_endpoint": _IdP.base + "/userinfo"})
        elif self.path.startswith("/userinfo"):
            self._json({"email": "sso.user@example.local", "department": "Маркетинг"})
        else:
            self._json({}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        if form.get("code") != ["good-code"] or not form.get("code_verifier"):
            return self._json({"error": "invalid_grant"}, 400)
        enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()
        idt = enc({"alg": "RS256"}) + "." + enc({"iss": _IdP.base, "aud": "korrektor", "exp": time.time() + 300,
                                                 "nonce": _IdP.nonce, "preferred_username": _IdP.login, "sub": _IdP.sub,
                                                 "name": "Единый Вход", "groups": _IdP.groups}) + ".sig"
        self._json({"access_token": "at", "id_token": idt, "token_type": "Bearer"})

    def log_message(self, *a):
        pass


def test_oidc_login(admin):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _IdP)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _IdP.base = f"http://127.0.0.1:{srv.server_address[1]}"
    set_st(oidc_enabled=True, oidc_issuer=_IdP.base, oidc_client_id="korrektor", oidc_client_secret="s",
           oidc_admin_values="korrektor-admins", oidc_corrector_values="korrektor-correctors")
    try:
        with TestClient(app, base_url="http://testserver") as c:
            assert "Войти через" in c.get("/login").text
            r = c.get("/login/oidc?next=/analytics", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith(_IdP.base + "/auth")
            q = parse_qs(urlparse(r.headers["location"]).query)
            assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["korrektor"]
            _IdP.nonce = q["nonce"][0]
            bad = c.get("/login/oidc/callback?code=good-code&state=wrong", follow_redirects=False)
            assert "error=oidc" in bad.headers["location"]
            r = c.get(f"/login/oidc/callback?code=good-code&state={q['state'][0]}", follow_redirects=False)
            assert r.headers["location"] == "/analytics", r.headers["location"]
            me = c.get("/account").text
            assert "Единый Вход" in me and "корректор" in me and "sso.user@example.local" in me
            assert c.post("/api/analytics/bulk", json={"status": "accepted", "items": []}, headers=O).status_code == 200
        # группу сняли на сервере учетных записей — роль понижается при следующем входе
        def sso_login(client):
            r = client.get("/login/oidc", follow_redirects=False)
            q = parse_qs(urlparse(r.headers["location"]).query)
            _IdP.nonce = q["nonce"][0]
            return client.get(f"/login/oidc/callback?code=good-code&state={q['state'][0]}", follow_redirects=False)
        _IdP.groups = []
        with TestClient(app, base_url="http://testserver") as c:
            sso_login(c)
            assert "автор" in c.get("/account").text
        # другой пользователь сервера с логином существующей учетной записи не получает к ней доступ
        admin.post("/admin/users/save", data={"login": "boss", "role": "admin", "password": "Boss123456", "active": "1"}, headers=O)
        _IdP.login, _IdP.sub = "boss", "uid-attacker"
        with TestClient(app, base_url="http://testserver") as c:
            assert "error=oidc" in sso_login(c).headers["location"]
        # тот же человек сменил логин на сервере — учетная запись прежняя (по sub)
        _IdP.login, _IdP.sub = "sso.renamed", "uid-1"
        with TestClient(app, base_url="http://testserver") as c:
            sso_login(c)
            assert "sso.user" in c.get("/account").text
        _IdP.login, _IdP.groups = "sso.user", ["korrektor-correctors"]
        # повторная подмена nonce отклоняется
        with TestClient(app, base_url="http://testserver") as c:
            r = c.get("/login/oidc", follow_redirects=False)
            q = parse_qs(urlparse(r.headers["location"]).query)
            _IdP.nonce = "другой"
            r = c.get(f"/login/oidc/callback?code=good-code&state={q['state'][0]}", follow_redirects=False)
            assert "error=oidc" in r.headers["location"]
    finally:
        set_st(oidc_enabled=False)
        srv.shutdown()


def test_learning_hides_rejected(admin, sample_pdf):
    set_st(learn_reject_threshold=2)
    try:
        ids = []
        for i in range(2):
            jid = upload(admin, sample_pdf, f"Обучение_{i}.pdf")
            j = wait(admin, jid)
            ids.append(jid)
            f = next(x for x in j["findings_list"] if x["rule"] == "dspace")
            admin.post(f"/api/jobs/{jid}/findings/{f['id']}", json={"status": "rejected"}, headers=O)
        jid = upload(admin, sample_pdf, "Обучение_3.pdf")
        j = wait(admin, jid)
        f = next(x for x in j["findings_list"] if x["rule"] == "dspace")
        assert f["status"] == "rejected" and f.get("auto") == "learned"
        assert "Скрыто по опыту" in f["note"]
        assert "скрыто по опыту" in admin.get(f"/job/{jid}").text
        # корректор вернул замечание — пометка снимается
        r = admin.post(f"/api/jobs/{jid}/findings/{f['id']}", json={"status": "accepted"}, headers=O).json()
        assert "auto" not in r["finding"]
    finally:
        set_st(learn_reject_threshold=3)


def test_visual_page_diff(admin, tmp_path):
    a = upload(admin, make_pdf(str(tmp_path / "Вид_v1.pdf")), "Вид_v1.pdf")
    wait(admin, a)
    b = upload(admin, make_pdf(str(tmp_path / "Вид_v2.pdf"), lines=TEXT + ["Добавленная строка текста."]), "Вид_v2.pdf")
    wait(admin, b)
    html = admin.get(f"/compare?a={a}&b={b}").text
    assert "Изменения на страницах" in html
    for side in ("a", "b"):
        r = admin.get(f"/compare/page.png?a={a}&b={b}&p=1&side={side}")
        assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert admin.get(f"/compare/page.png?a={a}&b={b}&p=99").status_code == 404


def test_backup(admin, tmp_path):
    set_st(backup_dir=str(tmp_path / "bk"), backup_keep=2)
    try:
        for _ in range(3):
            r = admin.post("/settings/backup-now", headers=O).json()
            assert r["ok"], r
            time.sleep(1.1)
        files = sorted(f for f in os.listdir(tmp_path / "bk") if not f.endswith(".sha256"))
        assert len(files) == 2 and all(backup.NAME_RE.match(f) for f in files)
        assert len(os.listdir(tmp_path / "bk")) == 4                      # у каждой копии — контрольная сумма
        with tarfile.open(tmp_path / "bk" / files[-1]) as t:
            names = t.getnames()
        assert any(n.endswith("korrektor.db") for n in names) and not any("/pages/" in n for n in names)
        assert admin.get(f"/settings/backups/{files[-1]}").status_code == 200
        assert admin.get("/settings/backups/..%2Fsettings.json").status_code == 404
        assert "Резервные копии" in admin.get("/settings").text
    finally:
        set_st(backup_dir="/var/backups/korrektor", backup_keep=14)


def test_noncanonical_ids_rejected(admin, sample_pdf):
    admin.post("/admin/users/save", data={"login": "own3", "role": "author", "password": "Own1234567", "active": "1"}, headers=O)
    jid = upload(admin, sample_pdf, "Чужой_канон.pdf")
    wait(admin, jid)
    set_st(authors_own_only=True)
    try:
        with TestClient(app, base_url="http://testserver") as a:
            a.post("/login", data={"login": "own3", "password": "Own1234567"})
            for p in (f"/api/jobs/+{jid}", f"/api/jobs/{jid}.0", f"/api/jobs/%20{jid}", f"/job/{jid}.0",
                      f"/job/+{jid}/file/source", f"/compare?a=+{jid}&b=+{jid}", f"/compare/page.png?a=+{jid}&b={jid}&p=1"):
                assert a.get(p).status_code in (403, 404), p
            r = a.post(f"/api/jobs/+{jid}/findings/1", json={"status": "rejected"}, headers=O)
            assert r.status_code in (403, 404)
    finally:
        set_st(authors_own_only=False)
    with TestClient(app, base_url="http://testserver") as a:            # и без режима «только свои»: роль не обойти
        a.post("/login", data={"login": "own3", "password": "Own1234567"})
        assert a.post(f"/api/jobs/+{jid}/findings/1", json={"status": "rejected"}, headers=O).status_code in (403, 404)
        assert a.post(f"/api/jobs/{jid}/findings/1", json={"status": "rejected"}, headers=O).status_code == 403
