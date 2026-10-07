# -*- coding: utf-8 -*-
"""Общие заготовки тестов: временная папка данных, синтетический PDF с известными ошибками."""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_DATA = tempfile.mkdtemp(prefix="korrektor-test-")
os.environ["KORREKTOR_DATA"] = _DATA
os.environ["KORREKTOR_ADMIN_PASSWORD"] = "тест-пароль"
os.environ.pop("KORREKTOR_SECRET", None)

FONT = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                         "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf") if os.path.exists(p)), None)

TEXT = [
    "Каталог насосных установок",
    "Насосные установки предназначены для водоснабжения зданий.",
    "Диапазон давления 10-20 бар при температуре до 70 °C.",
    "Установка комплектуется насосами.Оборудование проходит испытания.",
    "Корпус соответствует ГОСТ P 52 и выполнен из стали.",
    "Двойной  пробел стоит в этой строке текста документа.",
    "Мощность 5,5кВт обеспечивает работу в течение года.",
]


def make_pdf(path, pages=2, lines=TEXT):
    import pymupdf
    doc = pymupdf.open()
    for p in range(pages):
        page = doc.new_page(width=595, height=842)
        kw = {"fontfile": FONT, "fontname": "dv"} if FONT else {}
        y = 80
        for ln in lines:
            page.insert_text((60, y), ln, fontsize=11, **kw)
            y += 22
        page.insert_text((60, 800), f"Страница {p + 1}", fontsize=9, **kw)
    doc.save(path)
    return path


@pytest.fixture(scope="session")
def data_dir():
    return _DATA


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory):
    return make_pdf(str(tmp_path_factory.mktemp("pdf") / "Тестовый_каталог.pdf"))
