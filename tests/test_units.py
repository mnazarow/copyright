# -*- coding: utf-8 -*-
"""Модульные тесты: безопасность, настройки, аналитика, экспорт."""
import datetime as dt

from app import analytics, core
from app.main import safe_next
from app.pipeline import exports
from app.pipeline.run import report_name


def test_safe_next_blocks_open_redirect():
    assert safe_next("/jobs") == "/jobs"
    for bad in ("//evil.com", "/\\evil.com", "https://evil.com", "", "javascript:alert(1)", "/a\r\nSet-Cookie: x"):
        assert safe_next(bad) == "/"


def test_password_and_token_unicode():
    assert core.check_password("тест-пароль")
    assert not core.check_password("неверно")
    assert not core.check_password("")
    tok = core.make_token()
    assert core.check_token(tok)
    assert not core.check_token(tok[:-1] + ("0" if tok[-1] != "0" else "1"))
    assert not core.check_token("admin|абв|где")          # не ASCII — без исключения
    assert not core.check_token("мусор")


def test_secret_is_random_and_persistent(data_dir):
    import os
    assert core.SECRET and len(core.SECRET) >= 32
    assert os.path.exists(os.path.join(data_dir, ".secret"))
    assert core._secret() == core.SECRET


def test_coerce_limits():
    assert core.coerce("workers", "inf") == core.SCHEMA["workers"]["default"]
    assert core.coerce("workers", "1e999") == core.SCHEMA["workers"]["default"]
    assert core.coerce("auth_required", "on") is True


def test_formula_injection_guard():
    assert exports.safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert exports.safe("+7 495") == "'+7 495"
    assert exports.safe("обычный текст") == "обычный текст"
    assert exports.safe(5) == 5


def test_report_name_bad_template():
    assert report_name({"report_filename": "{foo}_{0}"}, "Док", 3) == "Док_корректура"
    assert report_name({"report_filename": "{name}_{job}"}, "Док/1", 3) == "Док_1_3"


def test_family_of_versions():
    a = analytics.family_of("Каталог_SPL_v2")
    assert a == analytics.family_of("Каталог SPL v3") == analytics.family_of("Каталог_SPL (1)")
    assert analytics.family_of("Каталог БТП. Москва. Страницы") == "каталог бтп москва"


def test_linfit_and_moving_avg():
    a, b = analytics.linfit([1, 2, None, 4])
    assert abs(b - 1) < 1e-9 and abs(a - 1) < 1e-9
    assert analytics.linfit([None, 5]) is None
    assert analytics.moving_avg([1, 2, 3, None], 3) == [1, 1.5, 2, 2.5]


def test_buckets():
    d = dt.date(2026, 3, 18)                              # среда
    assert analytics.bucket_of(d, "week") == dt.date(2026, 3, 16)
    assert analytics.bucket_of(d, "month") == dt.date(2026, 3, 1)
    assert analytics.bucket_next(dt.date(2026, 12, 1), "month") == dt.date(2027, 1, 1)


def test_compare_verdicts():
    c = analytics.compare({"density": 3, "precision": 90, "docs": 5}, {"density": 4, "precision": 80, "docs": 4})
    assert c["density"]["verdict"] == "better"            # меньше ошибок — лучше
    assert c["precision"]["verdict"] == "better"
    assert c["docs"]["verdict"] == "up"


def test_parse_filters_edge_cases():
    f = analytics.parse_filters({"period": "99999999999"})
    assert f["from"] is not None                          # без OverflowError
    f = analytics.parse_filters({"period": "custom", "from": "2026-05-01", "to": "2026-04-01"})
    assert f["from"] < f["to"]
    f = analytics.parse_filters({"period": "custom", "from": "мусор"})
    assert f["from"] is None


def _st(**kw):
    base = {"id": 1, "name": "A", "family": "a", "batch": "", "created": "2026-05-01T10:00:00", "duration": 10.0,
            "pages": 10, "verify_ok": True, "verify_fail": [], "preflight": {}, "auto_rejected": 0, "stages": [],
            "findings": [{"id": 1, "page": 1, "type": "Орфография", "rule": "spell", "source": "spell",
                          "status": "accepted", "systemic": 0, "bad": "слво", "good": "слово"},
                         {"id": 2, "page": 9, "type": "Требует проверки автором", "rule": "spell", "source": "spell",
                          "status": "rejected", "systemic": 0, "bad": "термин", "good": "—"}]}
    base.update(kw)
    return base


def test_metrics():
    m = analytics.metrics([_st(), _st(id=2, verify_ok=False)])
    assert m["docs"] == 2 and m["pages"] == 20 and m["findings"] == 4
    assert m["density"] == 2.0 and m["precision"] == 50.0 and m["author_share"] == 50.0
    assert m["verify_rate"] == 50.0 and m["avg_duration"] == 10.0
    assert analytics.metrics([])["density"] is None


def test_xlsx_csv_export():
    import io
    import openpyxl
    sheets = [("Лист", ["А", "Б"], [["=1+1", 1.5], [None, "x"]])]
    wb = openpyxl.load_workbook(io.BytesIO(analytics.to_xlsx(sheets, "т")))
    assert wb["Лист"]["A2"].value == "'=1+1"
    csv = analytics.to_csv(sheets)
    assert "1,5" in csv and "'=1+1" in csv
