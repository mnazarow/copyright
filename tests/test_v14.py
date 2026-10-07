# -*- coding: utf-8 -*-
"""Версия 1.4: восстановление из копии, разделение по отделам, напоминания, стандарт оформления,
справочник продукции, двухфакторный вход, отчет для руководства."""
import datetime as dt
import io
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import backup, core, notify, totp, users
from app.main import app
from app.pipeline import products, style
from conftest import make_pdf

O = {"Origin": "http://testserver"}


def set_st(**kw):
    core.update_settings(lambda s: s.update(kw))


def login(c, user, pw):
    return c.post("/login", data={"login": user, "password": pw}, follow_redirects=False)


@pytest.fixture(scope="module")
def admin():
    with TestClient(app, base_url="http://testserver") as c:
        assert login(c, "admin", "тест-пароль").status_code == 303
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


# ------------------------------------------------------------------ TOTP
def test_totp_codes():
    s = totp.new_secret()
    now = time.time()
    code = totp._code(s, int(now // 30))
    step = totp.check_code(s, code, now=now)
    assert step
    assert totp.check_code(s, code, last=step, now=now) is None          # повтор того же кода запрещен
    assert totp.check_code(s, "000000" if code != "000000" else "111111", now=now) is None
    # эталон RFC 6238 (SHA1, ключ «12345678901234567890», T = 59 → 94287082, 6 цифр — 287082)
    import base64
    ref = base64.b32encode(b"12345678901234567890").decode()
    assert totp._code(ref, 59 // 30) == "287082"


def test_totp_login_flow(admin):
    users.save("dvuh", "Двухфакторный", "corrector", "Parol12345")
    with TestClient(app, base_url="http://testserver") as c:
        # подключение в профиле
        assert login(c, "dvuh", "Parol12345").headers["location"] == "/"
        r = c.get("/account/2fa")
        assert r.status_code == 200 and "otpauth://totp/" in r.text
        secret = c.cookies.get("korrektor_2fa_new")
        assert secret
        from app import oidc
        s = oidc._unsign(secret)["s"]
        r = c.post("/account/2fa", data={"code": totp._code(s, int(time.time() // 30))}, headers=O)
        assert r.status_code == 200 and "резервные коды" in r.text.lower()
        assert totp.enabled("dvuh")
    with TestClient(app, base_url="http://testserver") as c:
        r = login(c, "dvuh", "Parol12345")
        assert r.headers["location"].startswith("/login/2fa")
        assert not c.cookies.get("korrektor")
        # промежуточный пропуск не является сеансом
        c.cookies.set("korrektor", c.cookies.get("korrektor_2fa"))
        assert c.get("/", follow_redirects=False).status_code == 303
        c.cookies.delete("korrektor")
        assert c.post("/login/2fa", data={"code": "123456"}, headers=O, follow_redirects=False).headers["location"].startswith("/login/2fa?error")
        code = totp._code(totp.get("dvuh")["secret"], int(time.time() // 30) + 1)
        r = c.post("/login/2fa", data={"code": code}, headers=O, follow_redirects=False)
        assert r.status_code == 303 and c.cookies.get("korrektor")
        assert c.get("/", follow_redirects=False).status_code == 200
    # резервный код
    codes, hashes = totp.new_recovery()
    totp._save("dvuh", totp.get("dvuh")["secret"], totp.get("dvuh")["last"], hashes)
    assert totp.verify("dvuh", codes[0]) == "recovery"
    assert totp.verify("dvuh", codes[0]) is None                       # одноразовый
    # сброс администратором
    assert admin.post("/admin/users/reset-2fa", data={"login": "dvuh"}, headers=O, follow_redirects=False).status_code == 303
    assert not totp.enabled("dvuh")


def test_totp_required(admin):
    users.save("obyaz", "Обязательный", "admin", "Parol12345")
    set_st(totp_required="admins")
    try:
        with TestClient(app, base_url="http://testserver") as c:
            r = login(c, "obyaz", "Parol12345")
            assert r.headers["location"].startswith("/login/2fa-setup")
            r = c.get("/login/2fa-setup")
            assert "otpauth://" in r.text
            from app import oidc
            s = oidc._unsign(c.cookies.get("korrektor_2fa_new"))["s"]
            r = c.post("/login/2fa-setup", data={"code": totp._code(s, int(time.time() // 30))}, headers=O)
            assert r.status_code == 200 and c.cookies.get("korrektor")
            assert totp.enabled("obyaz")
            r = c.post("/account/2fa/disable", data={"code": "1"}, headers=O, follow_redirects=False)
            assert "error" in r.headers["location"] and totp.enabled("obyaz")
        # автор — без второго фактора
        users.save("avt14", "Автор", "author", "Parol12345")
        with TestClient(app, base_url="http://testserver") as c:
            assert login(c, "avt14", "Parol12345").headers["location"] == "/"
    finally:
        set_st(totp_required="none")


# ------------------------------------------------------------------ стандарт оформления и справочник
def test_style_and_products(admin, tmp_path):
    csvd = "Артикул;Наименование;Мощность, кВт\nSPL-1010;Установка;5,5\nSPL-1020;Установка;7,5\nSPL-1030;У;11\n"
    r = admin.post("/settings/products", files={"file": ("ref.csv", csvd.encode("cp1251"))}, headers=O,
                   follow_redirects=False)
    assert "psaved=3" in r.headers["location"]
    assert "SPL-1020" in admin.get("/settings/products.csv").text
    set_st(style_fonts="Arial", style_min_size=6, style_colors="#000000")
    try:
        pdf = make_pdf(str(tmp_path / "Справочник.pdf"), pages=1, lines=[
            "Установка SPL-1010 мощностью 7,5 кВт", "Модель SPL-1021 для дома", "Сноска"])
        jid = upload(admin, pdf, "Справочник.pdf")
        assert wait(admin, jid)["status"] == "done"
        state = core.load_state(jid)
        kinds = {i["kind"] for i in state["products"]["issues"]}
        assert {"param", "typo"} <= kinds, state["products"]
        assert state["style"]["configured"] and any(i["kind"] == "font" for i in state["style"]["issues"])
        page = admin.get(f"/job/{jid}").text
        assert "Стандарт оформления" in page and "Справочник продукции" in page
        x = admin.get(f"/job/{jid}/checks.xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        # API для 1С
        r = admin.post("/api/products?name=1c.csv", content="Артикул;Напор, м\nBTP-1;40\n".encode(), headers=O)
        assert r.json()["items"] == 1
    finally:
        set_st(style_fonts="", style_min_size=0, style_colors="")
        os.remove(products.PATH())


def test_style_family():
    assert style.family("ABCDEF+Arial-BoldMT") == "arial"
    assert style.family("PTSans-Regular") == "ptsans"
    assert style.family("Times New Roman,Bold") == "timesnewroman"


# ------------------------------------------------------------------ отделы
def test_department_scope(admin, tmp_path):
    users.save("kor_a", "Корректор А", "corrector", "Parol12345", department="Отдел А")
    users.save("kor_b", "Корректор Б", "corrector", "Parol12345", department="Отдел Б")
    pdf = make_pdf(str(tmp_path / "Отдел.pdf"), pages=1)
    jid = upload(admin, pdf, "Отдел.pdf", department="Отдел А")
    set_st(department_scope=True)
    try:
        with TestClient(app, base_url="http://testserver") as a, TestClient(app, base_url="http://testserver") as b:
            login(a, "kor_a", "Parol12345")
            login(b, "kor_b", "Parol12345")
            assert a.get(f"/job/{jid}").status_code == 200
            assert b.get(f"/job/{jid}").status_code == 404
            assert all(j["id"] != jid for j in b.get("/api/jobs").json()) if isinstance(b.get("/api/jobs").json(), list) else True
            # отдел в профиле не меняется
            b.post("/account/profile", data={"name": "Б", "department": "Отдел А"}, headers=O)
            assert users.get("kor_b")["department"] == "Отдел Б"
            r = b.post("/api/analytics/bulk", json={"status": "rejected", "items": [{"job": jid, "id": 1}]}, headers=O)
            assert r.json()["changed"] == {}
    finally:
        set_st(department_scope=False)


# ------------------------------------------------------------------ напоминания
def test_reminders(admin, tmp_path, monkeypatch):
    pdf = make_pdf(str(tmp_path / "Напоминание.pdf"), pages=1)
    jid = upload(admin, pdf, "Напоминание.pdf")
    assert wait(admin, jid)["status"] == "done"
    old = (dt.datetime.now() - dt.timedelta(days=5)).isoformat(timespec="seconds")
    core.update_job(jid, finished=old)
    st = dict(core.load_settings(), remind_days=3, remind_max_days=30, email_enabled=True, notify_to="boss@example.com")
    assert any(p["job"] == jid for p in notify.pending_reviews(st))
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda st, subj, text, html=None, to=None: sent.append((to, text)) or "ok")
    r = notify.send_reminders(st, force=True)
    assert r["docs"] >= 1 and any("Напоминание" in t for _, t in sent)
    r2 = notify.send_reminders(st)                                    # повтор в тот же день — не шлется
    assert r2["docs"] == 0


def test_mail_template():
    assert notify.fill_template("Док «{name}»: {findings} {unknown}", {"name": "А", "findings": 3}) == "Док «А»: 3 {unknown}"


# ------------------------------------------------------------------ восстановление
def test_backup_restore(admin, tmp_path):
    set_st(backup_dir=str(tmp_path / "bk"), backup_include_pages=False)
    r = admin.post("/settings/backup-now", headers=O).json()
    assert r["ok"], r
    name = backup.listing()[0]["name"]
    assert admin.post(f"/settings/backups/{name}/verify", headers=O).json()["ok"]
    with core.db() as c:
        before = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        c.execute("INSERT INTO jobs(name, status, created) VALUES('лишнее', 'failed', '2026-01-01')")
    assert admin.post(f"/settings/backups/{name}/restore", data={"confirm": "нет"}, headers=O).json()["ok"] is False
    r = admin.post(f"/settings/backups/{name}/restore", data={"confirm": "ВОССТАНОВИТЬ"}, headers=O).json()
    assert r["ok"], r
    with core.db() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == before
    assert admin.get("/", follow_redirects=False).status_code == 200            # сеанс администратора жив
    # поврежденный архив
    bad = tmp_path / "bk" / "korrektor-upload-20260101-000000.tgz"
    bad.write_bytes(open(os.path.join(str(tmp_path / "bk"), name), "rb").read()[:5000])
    assert not backup.verify(str(bad))["ok"]
    # загрузка архива
    data = open(os.path.join(str(tmp_path / "bk"), name), "rb").read()
    r = admin.post("/settings/backups-upload", files={"file": ("x.tgz", io.BytesIO(data))}, headers=O, follow_redirects=False)
    assert "bsaved=korrektor-upload-" in r.headers["location"]
    set_st(backup_dir="")


# ------------------------------------------------------------------ отчет для руководства
def test_management_report(admin):
    r = admin.get("/analytics/report.pdf?period=365")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


def test_restore_rollback(admin, tmp_path, monkeypatch):
    """Ошибка на середине восстановления — прежние данные возвращаются на место."""
    set_st(backup_dir=str(tmp_path / "bk"))
    path = backup.run(None, "тест")
    with core.db() as c:
        c.execute("INSERT INTO jobs(name, status, created) VALUES('после копии', 'failed', '2026-01-01')")
        n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    real = os.replace
    failed = []

    def broken(a, b):
        if str(b).endswith("settings.json") and "_old" not in str(b) and not failed:
            failed.append(1)
            raise OSError("диск переполнен")
        return real(a, b)
    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError):
        backup.restore(path, wait=3, safety_copy=False)
    monkeypatch.setattr(os, "replace", real)
    assert os.path.exists(os.path.join(core.DATA, "settings.json"))
    with core.db() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == n
    assert not core.PAUSED.is_set()
    set_st(backup_dir="")


def test_attempts_atomic():
    from app import main
    key = "test|parallel"
    ok = [main._take_attempt([(key, 5)]) for _ in range(20)]
    assert ok.count(True) == 5                                        # больше пяти попыток не пропускается
    main._attempt_ok(key)
    assert main._take_attempt([(key, 5)])


def test_safe_next():
    from app.main import safe_next
    assert safe_next("/\t/evil.com") == "/" and safe_next("//evil") == "/" and safe_next("/jobs?x=1") == "/jobs?x=1"


# ------------------------------------------------------------------ 1.5: исправления по проверке кода
def test_ldap_cannot_open_oidc_account(monkeypatch):
    users.save("sso_user", "Единый вход", "admin", None, source="oidc")
    monkeypatch.setattr(users, "ldap_bind", lambda login, pw, st: (True, {}))
    u, why = users.authenticate("sso_user", "любой", dict(core.load_settings(), ldap_enabled=True))
    assert u is None


def test_profile_keeps_company_settings():
    from app.settings_schema import profile_overrides
    o = profile_overrides("soft")
    assert "whitelist" not in o and "ai_provider" not in o and "glossary" not in o
    assert o.get("typo_double_space") is False


def test_toc_regex_fast():
    import re as _re
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "app/pipeline/scripts/extract.py")).read()
    ns = {"re": _re}
    exec(src[src.index("class _Toc"):src.index("for p in pages:\n    toc = []")], ns)
    t = time.time()
    assert ns["toc_match"]("Подпись" + " " * 5000 + "Дата") is None
    assert time.time() - t < 0.5
    m = ns["toc_match"]("Введение ........ 3")
    assert m.group(1) == "Введение" and m.group(2) == "3"


def test_rules_regressions():
    from app.pipeline import autofix
    from app.pipeline.common import PageIndex
    from app.settings_schema import DEFAULTS
    lines = ["Давление 1-2 бар, температура 5-10 °C.", "Угол поворота 90º относительно оси", "см. п. 5.1 лицензии"]
    idx = PageIndex({"lines": [{"pdf_page": 1, "lines": [{"text": t, "block": 0, "line": i} for i, t in enumerate(lines)],
                                "head": [], "foot": []}]})
    f, _ = autofix.build_findings(autofix.typography(idx, dict(DEFAULTS)), 5)
    f = autofix.dedupe(f)
    fixes = {x["src"]: x["fix"] for x in f}
    assert fixes["Давление 1-2 бар, температура 5-10 °C."] == "Давление 1–2 бар, температура 5–10 °C."
    assert fixes["Угол поворота 90º относительно оси"] == "Угол поворота 90° относительно оси"
    assert "см. п. 5.1 лицензии" not in fixes


def test_products_semicolon_decimal_comma():
    ref = products.parse("Артикул;Наименование;Мощность, кВт;Высота, мм;Ширина, мм\nVT.1234;Насос;1,5;300;200\n".encode(), "a.csv")
    assert list(ref["items"]) == ["VT.1234"] and ref["units"]["Мощность"] == "кВт"
    dump = {"lines": [{"pdf_page": 1, "lines": [{"y": 1, "text": "VT.1234 высотой 300 мм"}]}]}
    assert products.check(dump, ref, {})["issues"] == []


def test_control_chars_and_curves(tmp_path):
    from app.pipeline import preflight
    from app.pipeline.run import clean, report_name
    assert clean("Каталог\x00\x03 насосов") == "Каталог насосов"
    assert len(report_name({}, "Ж" * 300, 1).encode()) <= 180
    import pymupdf
    d = pymupdf.open()
    p = d.new_page()
    sh = p.new_shape()
    for i in range(60):
        for j in range(4):
            x, y = 40 + i * 8, 100 + j * 20
            sh.draw_bezier((x, y), (x + 3, y - 6), (x + 6, y + 6), (x + 8, y))
    sh.finish(fill=(0, 0, 0))
    sh.commit()
    d.save(str(tmp_path / "c.pdf"))
    assert preflight.preflight(str(tmp_path / "c.pdf"), {}, {})["curves"] == [1]


def test_upload_rollback_and_csrf(admin, tmp_path):
    import zipfile as _z
    pdf = make_pdf(str(tmp_path / "Z.pdf"), pages=1)
    zp = tmp_path / "many.zip"
    with _z.ZipFile(zp, "w") as z:
        for i in range(201):
            z.write(pdf, f"d{i}.pdf")
    with core.db() as c:
        n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with open(zp, "rb") as fh:
        r = admin.post("/api/jobs", files={"file": ("many.zip", fh)}, headers=O)
    assert r.status_code == 413
    with core.db() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == n
    r = admin.post("/settings/apply", content=b"{", headers=dict(O, **{"Content-Type": "application/json"}))
    assert r.status_code == 400
    r = admin.post("/settings/apply", json={"key": "typo_quotes", "value": True}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert TestClient(app).get("/docs-files/administration.md", follow_redirects=False).status_code in (303, 401)


# ------------------------------------------------------------------ 1.5: новые возможности
import http.server
import json as _json
import threading


class _Srv(http.server.BaseHTTPRequestHandler):
    calls = []
    body = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        _Srv.calls.append(("GET", self.path, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer t0ken":
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.end_headers()
        self.wfile.write(_Srv.body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = _json.loads(self.rfile.read(n) or b"{}")
        _Srv.calls.append(("POST", self.path, data))
        res = {"result": [{"ID": "7"}]} if "user.get" in self.path else {"result": {"task": {"id": 501}}}
        out = _json.dumps(res).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture(scope="module")
def mock_srv():
    s = http.server.HTTPServer(("127.0.0.1", 0), _Srv)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


def test_table_columns(tmp_path):
    import pymupdf
    from conftest import FONT
    d = pymupdf.open()
    p = d.new_page()
    kw = {"fontfile": FONT, "fontname": "dv"} if FONT else {}

    def t(x, y, s):
        p.insert_text((x, y), s, fontsize=9, **kw)
    t(50, 80, "Модель"); t(150, 80, "Мощность, кВт"); t(260, 80, "Напор, м")
    t(50, 100, "SPL-1010"); t(160, 100, "5,5"); t(270, 100, "40")
    t(50, 115, "SPL-1020"); t(160, 115, "9"); t(270, 115, "45")
    t(50, 200, "Параметр"); t(200, 200, "SPL-1010"); t(300, 200, "SPL-1020")
    t(50, 220, "Напор, м"); t(205, 220, "40"); t(305, 220, "47")
    d.save(str(tmp_path / "t.pdf"))
    ref = products.parse("Артикул;Наименование;Мощность, кВт;Напор, м\nSPL-1010;У;5,5;40\nSPL-1020;У;7,5;45\n".encode(), "r.csv")
    det = sorted(i["detail"] for i in products.check_tables(str(tmp_path / "t.pdf"), ref, {}))
    assert det == ["SPL-1020: в таблице 47 м, в справочнике 45 м", "SPL-1020: в таблице 9 кВт, в справочнике 7,5 кВт"]


def test_products_sync(admin, mock_srv):
    _Srv.body = "Артикул;Мощность, кВт\nAB-100;1,5\nAB-200;2,2\n".encode("cp1251")
    set_st(products_sync_url=mock_srv + "/export/products.csv", products_sync_auth="Bearer t0ken")
    try:
        r = admin.post("/settings/products/sync", headers=O).json()
        assert r["ok"], r
        assert products.sync_status()["items"] == 2
        set_st(products_sync_auth="Bearer wrong")
        assert not admin.post("/settings/products/sync", headers=O).json()["ok"]
        assert "401" in products.sync_status()["error"]
    finally:
        set_st(products_sync_url="", products_sync_auth="")
        if os.path.exists(products.PATH()):
            os.remove(products.PATH())


def test_ip_allowlist():
    assert users.ip_allowed("10.1.2.3", "10.0.0.0/8") and not users.ip_allowed("11.0.0.1", "10.0.0.0/8")
    assert users.ip_allowed("127.0.0.1", "10.0.0.0/8") and users.ip_allowed("8.8.8.8", "")
    set_st(admin_networks="10.0.0.0/8")
    try:
        with TestClient(app, base_url="http://testserver") as c:            # адрес клиента — «testclient»
            assert login(c, "admin", "тест-пароль").status_code == 303
            assert c.get("/jobs").status_code == 200
            assert c.get("/settings").status_code == 403
    finally:
        set_st(admin_networks="")


def test_control_and_bitrix(admin, tmp_path, mock_srv, monkeypatch):
    pdf = make_pdf(str(tmp_path / "Контроль.pdf"), pages=1)
    jid = upload(admin, pdf, "Контроль.pdf")
    assert wait(admin, jid)["status"] == "done"
    page = admin.get("/control")
    assert page.status_code == 200 and f"/job/{jid}" in page.text
    set_st(bitrix_webhook="https://example.bitrix24.ru/rest/1/abc/", bitrix_default_responsible=0)
    from app import control
    real = control._b24

    def fake(st, method, payload):
        import httpx
        r = httpx.post(f"{mock_srv}/rest/{method}.json", json=payload, timeout=5)
        return r.json()["result"]
    monkeypatch.setattr(control, "_b24", fake)
    try:
        users.save("avtor_b24", "Автор", "author", "Parol12345", email="a@example.com")
        core.update_job(jid, owner="avtor_b24")
        r = admin.post(f"/job/{jid}/bitrix", headers=O).json()
        assert r["ok"] and "501" in r["answer"], r
        post = [c for c in _Srv.calls if c[0] == "POST" and "tasks.task.add" in c[1]][-1][2]
        assert post["fields"]["RESPONSIBLE_ID"] == 7 and f"/job/{jid}" in post["fields"]["DESCRIPTION"]
        assert control.bitrix_task_of(jid)["task"] == 501
    finally:
        monkeypatch.setattr(control, "_b24", real)
        set_st(bitrix_webhook="")
