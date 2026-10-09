# -*- coding: utf-8 -*-
"""Версия 1.6: скрипт правок InDesign, согласование, рисунки и таблицы, сверка языковых версий,
вход по сертификату, шифрование резервных копий."""
import json
import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app import core, users
from app.main import app
from app.pipeline import indesign
from conftest import make_pdf

O = {"Origin": "http://testserver"}
HERE = os.path.dirname(os.path.abspath(__file__))


def set_st(**kw):
    core.update_settings(lambda s: s.update(kw))


def login(c, user, pw):
    return c.post("/login", data={"login": user, "password": pw}, headers=O, follow_redirects=False)


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


# ------------------------------------------------------------------ InDesign
FINDINGS = [
    {"id": 1, "page": 1, "type": "Типографика", "src": "Давление 1-2 бар, температура 5-10 °C.", "bad": "1-2 … 5-10",
     "good": "1–2 … 5–10", "fix": "Давление 1–2 бар, температура 5–10 °C.", "note": "Тире в диапазонах.", "status": "new"},
    {"id": 2, "page": 2, "type": "Орфография", "src": "насос имеет высокий кпд и надежнасть", "bad": "надежнасть",
     "good": "надежность", "fix": "надежность", "note": "Опечатка.", "status": "accepted"},
    {"id": 3, "page": 2, "type": "Требует проверки автором", "src": "мощность 7,5 кВт", "bad": "7,5 кВт", "good": "—",
     "fix": "—", "note": "Сверьте с паспортом.", "status": "new"},
    {"id": 4, "page": 1, "type": "Типографика", "src": "цена 100 руб.", "bad": "100 руб.", "good": "100 ₽", "fix": "100 ₽",
     "note": "Знак рубля.", "status": "new"},
    {"id": 5, "page": 2, "type": "Типографика", "src": "Серия SPL^2", "bad": "SPL^2", "good": "SPL²", "fix": "SPL²",
     "note": "Степень.", "status": "new"},
    {"id": 6, "page": 2, "type": "Орфография", "src": "уже исправленое слово", "bad": "исправленое", "good": "исправленное",
     "fix": "исправленное", "note": "x", "status": "new"},
]


def run_jsx(js, stories, options, tmp_path):
    (tmp_path / "s.jsx").write_text(js)
    (tmp_path / "s.json").write_text(json.dumps({"stories": stories, "options": options}, ensure_ascii=False))
    r = subprocess.run(["node", os.path.join(HERE, "js", "indesign_mock.js"), str(tmp_path / "s.jsx"), str(tmp_path / "s.json")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js не установлен")
def test_indesign_script(tmp_path):
    js, n = indesign.build(FINDINGS, "Каталог «SPL»", 15, "https://k/job/15")
    assert all(ord(c) < 128 for c in js) and n == 7
    text = ("Давление 1-2 бар, температура 5-10 °C. Давление 1-2 бар в линии. Насос имеет высокий кпд и надежнасть. "
            "Мощность 7,5 кВт. Серия SPL^2. Уже исправленное слово.")
    r = run_jsx(js, [{"text": text, "pageBreaks": [44]}], [True, False, True, True], tmp_path)
    s = r["stories"][0]
    assert s.startswith("Давление 1–2 бар, температура 5–10 °C. Давление 1-2 бар в линии.")   # второе место не тронуто
    assert "надежность" in s and "SPL²" in s and "7,5 кВт" in s
    names = {(c["text"], c["name"]) for c in r["conditions"]}
    assert ("7,5 кВт", "Корректор: проверить") in names and ("надежность", "Корректор: исправлено") in names
    assert "уже исправлено" in r["log"] and "не найдено в макете" in r["log"]
    # только принятые корректором
    r2 = run_jsx(js, [{"text": text, "pageBreaks": [44]}], [True, True, False, False], tmp_path)
    assert "надежность" in r2["stories"][0] and "1-2 бар, температура" in r2["stories"][0] and not r2["conditions"]


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js не установлен")
def test_indesign_ambiguous(tmp_path):
    f = [{"id": 1, "page": 1, "type": "Орфография", "src": "насос", "bad": "насос", "good": "насосы", "fix": "насосы",
          "note": "", "status": "new"}]
    js, _ = indesign.build(f, "doc", 1)
    r = run_jsx(js, [{"text": "насос и насос", "pageBreaks": []}], [True, False, True, False], tmp_path)
    assert r["stories"][0] == "насос и насос" and "несколько похожих мест" in r["log"]


def test_indesign_endpoint(admin, tmp_path):
    pdf = make_pdf(str(tmp_path / "Макет.pdf"), pages=1)
    jid = upload(admin, pdf, "Макет.pdf")
    assert wait(admin, jid)["status"] == "done"
    r = admin.get(f"/job/{jid}/indesign.jsx")
    assert r.status_code == 200 and b"KORREKTOR" in r.content and max(r.content) < 128
    z = admin.get(f"/job/{jid}/zip")
    import io
    import zipfile
    assert any(n.endswith(".jsx") for n in zipfile.ZipFile(io.BytesIO(z.content)).namelist())


# ------------------------------------------------------------------ согласование
def test_workflow(admin, tmp_path):
    users.save("wf_kor", "Корректор", "corrector", "Parol12345", department="Маркетинг")
    users.save("wf_ruk", "Руководитель", "manager", "Parol12345", department="Маркетинг")
    users.save("wf_avt", "Автор", "author", "Parol12345", department="Маркетинг")
    set_st(workflow_enabled=True, workflow_sign_required=True, workflow_require_reviewed=True, workflow_require_verified=False)
    try:
        with TestClient(app, base_url="http://testserver") as a:
            login(a, "wf_avt", "Parol12345")
            pdf = make_pdf(str(tmp_path / "Согласование.pdf"), pages=1)
            jid = upload(a, pdf, "Согласование.pdf")
            assert wait(a, jid)["status"] == "done"
            page = a.get(f"/job/{jid}").text
            assert "Согласование" in page and "Взять в работу" not in page          # автору — без кнопок корректора
        with TestClient(app, base_url="http://testserver") as k:
            login(k, "wf_kor", "Parol12345")
            assert "Взять в работу" in k.get(f"/job/{jid}").text
            r = k.post(f"/job/{jid}/workflow", data={"action": "take"}, headers=O, follow_redirects=False)
            assert core.get_job(jid)["wf_state"] == "review", r.headers
            # согласовать нельзя: замечания не разобраны
            r = k.post(f"/job/{jid}/workflow", data={"action": "approve"}, headers=O, follow_redirects=False)
            assert "wf_error" in r.headers["location"] and core.get_job(jid)["wf_state"] == "review"
            # возврат без комментария не проходит
            r = k.post(f"/job/{jid}/workflow", data={"action": "return"}, headers=O, follow_redirects=False)
            assert "wf_error" in r.headers["location"]
            assert k.post(f"/job/{jid}/workflow", data={"action": "return", "comment": "Исправьте таблицу"},
                          headers=O, follow_redirects=False).status_code == 303
            assert core.get_job(jid)["wf_state"] == "returned"
        with TestClient(app, base_url="http://testserver") as a:
            login(a, "wf_avt", "Parol12345")
            r = a.post(f"/api/jobs/{jid}/workflow", json={"action": "fixed", "comment": "готово"}, headers=O)
            assert r.json()["ok"] and core.get_job(jid)["wf_state"] == ""
            assert a.post(f"/api/jobs/{jid}/workflow", json={"action": "approve"}, headers=O).status_code == 409
        # корректор отмечает все замечания и согласует
        state = core.load_state(jid)
        for f in state["findings"]:
            f["status"] = "accepted"
        core.save_state(jid, state)
        with TestClient(app, base_url="http://testserver") as k:
            login(k, "wf_kor", "Parol12345")
            assert k.post(f"/api/jobs/{jid}/workflow", json={"action": "approve"}, headers=O).json()["state"] == "approved"
            assert k.post(f"/api/jobs/{jid}/workflow", json={"action": "sign"}, headers=O).status_code == 409   # не руководитель
        with TestClient(app, base_url="http://testserver") as m:
            login(m, "wf_ruk", "Parol12345")
            assert m.post(f"/api/jobs/{jid}/workflow", json={"action": "sign"}, headers=O).json()["state"] == "signed"
            ctl = m.get("/control").text
            assert "Согласование" in ctl and "утверждены: " in ctl
        from app import workflow
        h = workflow.history(jid)
        assert [x["action"] for x in h] == ["take", "return", "fixed", "approve", "sign"]
        assert h[1]["comment"] == "Исправьте таблицу"
        # повторная проверка — согласование заново
        admin.post(f"/job/{jid}/restart", headers=O)
        assert core.get_job(jid)["wf_state"] == ""
    finally:
        set_st(workflow_enabled=False)


# ------------------------------------------------------------------ рисунки, таблицы, изображения
def test_figures_rule():
    from app.pipeline import autofix
    from app.pipeline.common import PageIndex
    from app.settings_schema import DEFAULTS
    L = {1: ["Рисунок 1 — Общий вид", "Схема на рис. 2 и в таблице 1.", "Таблица 1. Характеристики"],
         2: ["Рисунок 2 — Схема", "Рисунок 4 — Габариты", "см. рис. 7 и табл. 3"],
         3: ["Рисунок 2 — Повтор", "Таблица 1 (продолжение)", "Таблица 2 — Размеры"]}
    dump = {"lines": [{"pdf_page": p, "lines": [{"text": t, "block": 0, "line": i} for i, t in enumerate(v)],
                       "head": [], "foot": []} for p, v in L.items()]}
    notes = [h["note"] for h in autofix.figures(PageIndex(dump), dict(DEFAULTS))]
    assert any("пропущен номер 3" in n for n in notes)
    assert any("уже использован на стр. 2" in n for n in notes)
    assert any("«рис. 7»" in n for n in notes) and any("«табл. 3»" in n for n in notes)
    assert not any("Таблица 1" in n and "уже" in n for n in notes)          # «продолжение» не повтор


def _noise_png(seed):
    import random
    import pymupdf
    random.seed(seed)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 64, 64), False)
    for y in range(64):
        for x in range(64):
            v = random.randint(0, 255)
            pix.set_pixel(x, y, (v, (v * 3) % 256, 255 - v))
    return pix.tobytes("png")


def test_duplicate_images(tmp_path):
    import pymupdf
    from conftest import FONT
    from app.pipeline import images
    a, b = _noise_png(1), _noise_png(2)
    d = pymupdf.open()
    kw = {"fontfile": FONT, "fontname": "dv"} if FONT else {}
    for label, img in (("Насос SPL-1010", a), ("Насос SPL-2020", a), ("Насос SPL-3030", b), ("Насос SPL-3030 вид сбоку", b)):
        p = d.new_page()
        p.insert_text((60, 90), label, fontsize=12, **kw)
        p.insert_image(pymupdf.Rect(60, 100, 260, 300), stream=img)
    d.save(str(tmp_path / "img.pdf"))
    r = images.check(str(tmp_path / "img.pdf"), {})
    assert r["images"] == 4 and len(r["duplicates"]) == 1
    assert sorted(p["page"] for p in r["duplicates"][0]["places"]) == [1, 2]


# ------------------------------------------------------------------ языковые версии
def test_lang_compare(admin, tmp_path):
    from app import langcmp
    a, b = langcmp.extract("Мощность 5,5 кВт, масса 1 200 кг, модель EVR32, 4 ступени", "ru"), \
        langcmp.extract("Power 7.5 kW, weight 1,200 kg, model EVR33, 4 stages", "en")
    assert langcmp.diff(a[0], b[0]) == ([("5.5", "кВт")], [("7.5", "кВт")])
    assert langcmp.diff(a[2], b[2]) == (["EVR32"], ["EVR33"])
    assert langcmp.diff(a[1], b[1]) == ([], [])
    ru = make_pdf(str(tmp_path / "Каталог_RU.pdf"), pages=1, lines=["Насос SPL-1010", "Мощность 5,5 кВт, напор 40 м"])
    en = make_pdf(str(tmp_path / "Catalog_EN.pdf"), pages=1, lines=["Pump SPL-1010", "Power 7.5 kW, head 40 m"])
    ja, jb = upload(admin, ru, "Каталог_RU.pdf"), upload(admin, en, "Catalog_EN.pdf")
    assert wait(admin, ja)["status"] == "done" and wait(admin, jb)["status"] == "done"
    r = admin.get(f"/api/compare/lang?a={ja}&b={jb}").json()
    assert r["a"]["lang"] == "ru" and r["b"]["lang"] == "en" and r["by_page"]
    units = [x for x in r["rows"] if x["kind"] == "units"]
    assert units and units[0]["only_a"] == ["5,5 кВт"] and units[0]["only_b"] == ["7.5 кВт"]
    page = admin.get(f"/compare/lang?a={ja}&b={jb}")
    assert page.status_code == 200 and "5,5 кВт" in page.text
    assert admin.get(f"/compare/lang/export?a={ja}&b={jb}").status_code == 200


# ------------------------------------------------------------------ вход по сертификату
def test_cert_login(admin, monkeypatch):
    from app import certlogin
    assert certlogin.parse_dn(r"emailAddress=ivanov@corp.ru,CN=\D0\98\D0\B2\D0\B0\D0\BD\D0\BE\D0\B2,O=A\, B")["CN"] == "Иванов"
    users.save("cert_user", "Иванов", "corrector", "Parol12345", email="ivanov@corp.ru")
    set_st(cert_login_enabled=True, cert_proxy_secret="s3cret-0123456789", cert_map="email", cert_autocreate=False)
    H = {"X-SSL-Client-Verify": "SUCCESS", "X-SSL-Client-S-DN": "emailAddress=ivanov@corp.ru,CN=Ivanov",
         "X-Korrektor-Proxy-Secret": "s3cret-0123456789"}
    try:
        with TestClient(app, base_url="http://testserver") as c:
            r = c.get("/login/cert", headers=H, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/" and c.cookies.get("korrektor")
            assert c.get("/jobs", follow_redirects=False).status_code == 200
        with TestClient(app, base_url="http://testserver") as c:              # без секрета прокси — отказ
            r = c.get("/login/cert", headers=dict(H, **{"X-Korrektor-Proxy-Secret": "x"}), follow_redirects=False)
            assert "error=cert" in r.headers["location"] and not c.cookies.get("korrektor")
            r = c.get("/login/cert", headers=dict(H, **{"X-SSL-Client-Verify": "FAILED:certificate has expired"}),
                      follow_redirects=False)
            assert "error=cert" in r.headers["location"]
            r = c.get("/login/cert", headers=dict(H, **{"X-SSL-Client-S-DN": "emailAddress=nobody@corp.ru"}),
                      follow_redirects=False)
            assert "error=cert" in r.headers["location"]
        set_st(cert_autocreate=True, cert_default_role="author")
        with TestClient(app, base_url="http://testserver") as c:
            r = c.get("/login/cert", headers=dict(H, **{"X-SSL-Client-S-DN": "emailAddress=petrov@corp.ru,CN=Petrov,OU=Sales"}),
                      follow_redirects=False)
            assert r.headers["location"] == "/" and users.get("petrov")["source"] == "cert"
            assert users.get("petrov")["department"] == "Sales"
        set_st(cert_proxy_secret="short")                                    # короткий секрет — вход отключен
        with TestClient(app, base_url="http://testserver") as c:
            assert "error=cert" in c.get("/login/cert", headers=dict(H, **{"X-Korrektor-Proxy-Secret": "short"}),
                                         follow_redirects=False).headers["location"]
    finally:
        set_st(cert_login_enabled=False, cert_autocreate=False)


# ------------------------------------------------------------------ шифрование копий
def test_encrypted_backup(admin, tmp_path):
    from app import backup, crypt
    src = tmp_path / "plain.bin"
    src.write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    crypt.encrypt_file(str(src), str(tmp_path / "x.enc"), "пароль-1")
    assert crypt.is_encrypted(str(tmp_path / "x.enc"))
    crypt.decrypt_file(str(tmp_path / "x.enc"), str(tmp_path / "y.bin"), "пароль-1")
    assert (tmp_path / "y.bin").read_bytes() == src.read_bytes()
    with pytest.raises(crypt.CryptoError):
        crypt.decrypt_file(str(tmp_path / "x.enc"), str(tmp_path / "z.bin"), "неверный")
    assert not (tmp_path / "z.bin").exists()
    data = bytearray((tmp_path / "x.enc").read_bytes())
    data[100] ^= 1                                                     # подмена одного бита
    (tmp_path / "w.enc").write_bytes(bytes(data))
    with pytest.raises(crypt.CryptoError):
        crypt.decrypt_file(str(tmp_path / "w.enc"), str(tmp_path / "w.bin"), "пароль-1")

    set_st(backup_dir=str(tmp_path / "bk"), backup_password="Секрет-копий-2026")
    try:
        r = admin.post("/settings/backup-now", headers=O).json()
        assert r["ok"] and ".tgz.enc" in r["answer"], r
        name = backup.listing()[0]["name"]
        assert name.endswith(".tgz.enc") and crypt.is_encrypted(str(tmp_path / "bk" / name))
        v = admin.post(f"/settings/backups/{name}/verify", headers=O).json()
        assert v["ok"] and "зашифрованная" in v["answer"], v
        with core.db() as c:
            n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            c.execute("INSERT INTO jobs(name, status, created) VALUES('лишнее', 'failed', '2026-01-01')")
        r = admin.post(f"/settings/backups/{name}/restore", data={"confirm": "ВОССТАНОВИТЬ"}, headers=O).json()
        assert r["ok"], r
        with core.db() as c:
            assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == n
        assert not [f for f in os.listdir(core.DATA) if "decrypt" in f]                     # временные файлы удалены
        set_st(backup_password="другой")
        v = admin.post(f"/settings/backups/{name}/verify", headers=O).json()
        assert not v["ok"] and "неверный пароль" in v["answer"]
    finally:
        set_st(backup_dir="", backup_password="")


# ------------------------------------------------------------------ исправления по проверке кода 1.6
def test_workflow_separation_of_duties(admin, tmp_path):
    users.save("wf_solo", "Сам себе руководитель", "manager", "Parol12345")
    set_st(workflow_enabled=True, workflow_require_reviewed=False, workflow_require_verified=False, workflow_allow_self=False)
    try:
        with TestClient(app, base_url="http://testserver") as m:
            login(m, "wf_solo", "Parol12345")
            jid = upload(m, make_pdf(str(tmp_path / "Свой.pdf"), pages=1), "Свой.pdf")
            assert wait(m, jid)["status"] == "done"
            assert m.post(f"/api/jobs/{jid}/workflow", json={"action": "take"}, headers=O).json()["ok"]
            assert m.post(f"/api/jobs/{jid}/workflow", json={"action": "approve"}, headers=O).status_code == 409
        set_st(workflow_allow_self=True)
        with TestClient(app, base_url="http://testserver") as m:
            login(m, "wf_solo", "Parol12345")
            assert m.post(f"/api/jobs/{jid}/workflow", json={"action": "approve"}, headers=O).json()["ok"]
            set_st(workflow_allow_self=False)
            # согласовал сам — утвердить сам не может
            assert m.post(f"/api/jobs/{jid}/workflow", json={"action": "sign"}, headers=O).status_code == 409
    finally:
        set_st(workflow_enabled=False, workflow_allow_self=False)


def test_summary_follows_marks_and_yo(admin, tmp_path):
    jid = upload(admin, make_pdf(str(tmp_path / "Итог.pdf"), pages=1), "Итог.pdf")
    assert wait(admin, jid)["status"] == "done"
    state = core.load_state(jid)
    n = len(state["findings"])
    assert n >= 2
    f0 = state["findings"][0]
    admin.post(f"/api/jobs/{jid}/findings/{f0['id']}", json={"status": "rejected"}, headers=O)
    admin.post(f"/api/jobs/{jid}/rebuild", json={}, headers=O)
    s = core.load_state(jid)
    assert not s.get("summary_edited") and f"найдено {n - 1} " in s["meta"]["summary"]
    # правка с «ё» сохраняется с «е», проверка 25 проходит
    f1 = next(f for f in s["findings"] if f["type"] != "Требует проверки автором" and f["id"] != f0["id"])
    r = admin.post(f"/api/jobs/{jid}/findings/{f1['id']}", json={"note": "Ещё одно пояснение — всё верно."}, headers=O)
    assert r.status_code == 200 and "ё" not in r.json()["finding"]["note"]
    assert admin.post(f"/api/jobs/{jid}/findings/{f1['id']}", json={"note": "  "}, headers=O).status_code == 400
    v = admin.post(f"/api/jobs/{jid}/rebuild", json={"summary": "Итог: всё проверено, ещё раз."}, headers=O).json()
    s = core.load_state(jid)
    assert s["summary_edited"] and "ё" not in s["meta"]["summary"] and v["passed"] == v["total"]


def test_plurals_and_number_options():
    from app.pipeline import autofix, run
    from app.pipeline.common import PageIndex, plural
    from app.settings_schema import DEFAULTS
    assert [plural(n, "замечание", "замечания", "замечаний") for n in (1, 2, 5, 11, 21, 22)] == \
        ["замечание", "замечания", "замечаний", "замечаний", "замечание", "замечания"]
    s = run.make_summary([{"type": "Орфография", "src": "x", "page": 1}], 3)
    assert "найдено 1 замечание" in s and "3 страницы" in s
    lines = ['Масса 1200 кг, длина 12000 мм, год 2026, артикул SPL-1200', 'Цена "дешево" и 1,5 mm']
    idx = PageIndex({"lines": [{"pdf_page": 1, "lines": [{"text": t, "block": 0, "line": i} for i, t in enumerate(lines)],
                                "head": [], "foot": []}]})
    h4 = autofix.typography(idx, dict(DEFAULTS, thousands="space4", quote_style="en", decimal_separator="dot"))
    got = {(h["rule"], h["bad"]) for h in h4}
    assert ("thousands", "1200") in got and ("thousands", "12000") in got and ("thousands", "2026") not in got
    assert ("quotes", '"дешево"') in got and ("decimal", "1,5") in got


def test_settings_save_keeps_concurrent_additions(admin):
    page = admin.get("/settings").text
    import re as _re
    h = _re.search(r'name="h__whitelist" value="([0-9a-f]+)"', page).group(1)
    wl = core.load_settings().get("whitelist") or ""
    core.update_settings(lambda s: s.update(whitelist=(wl + " гидрострелка").strip()))    # добавил корректор
    form = {"whitelist": wl, "h__whitelist": h}                                          # форма администратора старая
    admin.post("/settings", data=form, headers=O, follow_redirects=False)
    assert "гидрострелка" in core.load_settings()["whitelist"]


def test_backup_env_without_secrets(tmp_path, monkeypatch):
    import io
    import tarfile
    from app import backup
    env = tmp_path / "korrektor.env"
    env.write_text("KORREKTOR_PORT=8080\nKORREKTOR_SECRET=abc\nKORREKTOR_ADMIN_PASSWORD=Pass\n")
    monkeypatch.setattr(backup, "ENV_FILE", str(env))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        backup._add_env(t, encrypted=False)
    buf.seek(0)
    text = tarfile.open(fileobj=buf).extractfile("etc/korrektor.env").read().decode()
    assert "KORREKTOR_PORT=8080" in text and "abc" not in text and "Pass" not in text


def test_retention_and_stuck_uploads(admin, tmp_path):
    import datetime as dt
    jid = upload(admin, make_pdf(str(tmp_path / "Старый.pdf"), pages=1), "Старый.pdf")
    assert wait(admin, jid)["status"] == "done"
    old = (dt.datetime.now() - dt.timedelta(days=100)).isoformat(timespec="seconds")
    core.update_job(jid, created=old, finished=old, wf_state="approved")
    set_st(retention_days=30, workflow_enabled=True)
    try:
        assert jid not in [j["id"] for j in core.expired_jobs(core.load_settings())]   # на согласовании — не удаляется
        core.update_job(jid, wf_state="signed")
        assert jid in [j["id"] for j in core.expired_jobs(core.load_settings())]
    finally:
        set_st(retention_days=0, workflow_enabled=False)
    with core.db() as c:
        c.execute("INSERT INTO jobs(name, status, created) VALUES('обрыв', 'uploading', '2026-01-01')")
        stuck = c.execute("SELECT max(id) FROM jobs").fetchone()[0]
    core.init_db()
    assert core.get_job(stuck)["status"] == "failed"


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js не установлен")
def test_indesign_name_cannot_break_out(tmp_path):
    js, _ = indesign.build(FINDINGS[:1], "Каталог\r\nalert('x'); //", 1)
    assert "\r" not in js
    for line in js.split("\n"):                      # имя документа — только в комментарии или внутри строки JSON
        if "alert('x')" in line:
            assert line.lstrip().startswith("//") or line.index('"') < line.index("alert('x')")
    r = run_jsx(js, [{"text": "Давление 1-2 бар, температура 5-10 °C.", "pageBreaks": []}], [True, False, False, False], tmp_path)
    assert not any("x" == a for a in r["alerts"]) and len(r["alerts"]) == 1      # только итоговое окно скрипта


def test_crop_rejects_bad_numbers(admin, tmp_path):
    jid = upload(admin, make_pdf(str(tmp_path / "Кадр.pdf"), pages=1), "Кадр.pdf")
    assert wait(admin, jid)["status"] == "done"
    assert admin.get(f"/job/{jid}/crop/1.png?r=0,0,1e308,1e308").status_code == 400
    assert admin.get(f"/job/{jid}/crop/1.png?r=nan,0,10,10").status_code == 400
    assert admin.get(f"/job/{jid}/crop/1.png?r=50,50,200,200").status_code == 200
