# -*- coding: utf-8 -*-
"""Сверка языковых версий документа (например, русский и английский каталоги).

Переводчик и верстальщик чаще всего ошибаются не в словах, а в данных: потерянная строка таблицы, «5,5» вместо «7,5»,
другой артикул. Сверка сравнивает то, что не зависит от языка:
  * значения с единицами измерения — «5,5 кВт» = «5.5 kW», «1 200 мм» = «1,200 mm»;
  * числа без единиц (с учетом десятичной запятой в русском и точки в английском);
  * обозначения изделий (SPL-1010, EVR32, DN50).
При одинаковом числе страниц сравнение идет постранично, иначе — по документу целиком.
"""
import json
import os
import re
from collections import Counter

from . import core
from .pipeline.products import TOKEN, norm as anorm

UNITS = [  # (класс, регулярное выражение единицы; русские и английские написания)
    ("кВт", r"кВт|kW"), ("Вт", r"Вт|W"), ("МВт", r"МВт|MW"), ("м3/ч", r"м[3³]\s?/\s?ч|m[3³]\s?/\s?h|cmh"),
    ("л/мин", r"л\s?/\s?мин|l\s?/\s?min|lpm"), ("л/с", r"л\s?/\s?с|l\s?/\s?s"), ("бар", r"бар|bar"),
    ("МПа", r"МПа|MPa"), ("кПа", r"кПа|kPa"), ("Па", r"Па|Pa"), ("мм", r"мм|mm"), ("см", r"см|cm"), ("м", r"м|m"),
    ("кг", r"кг|kg"), ("л", r"л|l|L"), ("°C", r"°\s?[CС]"), ("В", r"В|V"), ("А", r"А|A"),
    ("Гц", r"Гц|Hz"), ("об/мин", r"об\s?/\s?мин|rpm|min-1|min⁻¹"), ("дБ", r"дБ\(?А?\)?|dB\(?A?\)?"),
    ("%", r"%"), ("ч", r"ч|h"), ("мин", r"мин|min"),
]
UNIT_RX = "|".join(f"(?P<u{i}>{rx})" for i, (_, rx) in enumerate(UNITS))
NUM_RU = r"\d{1,3}(?:[   ]\d{3})+(?:,\d+)?|\d+(?:,\d+)?"
NUM_EN = r"\d{1,3}(?:[,\u00a0\u202f ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


def lang_of(text):
    cyr = len(re.findall(r"[А-Яа-яЁё]", text))
    lat = len(re.findall(r"[A-Za-z]", text))
    return "ru" if cyr >= lat else "en"


def _canon(raw, lang):
    s = raw.replace(" ", " ").replace(" ", " ")
    if lang == "ru":
        s = s.replace(" ", "").replace(",", ".")
    else:
        s = s.replace(",", "").replace(" ", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _blank(text, spans):
    """Заменяет участки текста пробелами за один проход (без квадратичного копирования строки)."""
    if not spans:
        return text
    out, pos = [], 0
    for a, b in sorted(spans):
        if a < pos:
            a = pos
        if b <= a:
            continue
        out.append(text[pos:a])
        out.append(" " * (b - a))
        pos = b
    out.append(text[pos:])
    return "".join(out)


def extract(text, lang):
    """→ (значения с единицами, числа без единиц, обозначения) — счетчики."""
    num = NUM_RU if lang == "ru" else NUM_EN
    rx = re.compile(rf"(?<![\w.,])({num})\s?(?:{UNIT_RX})(?![\w])")
    with_units, spans = Counter(), []
    for m in rx.finditer(text):
        v = _canon(m.group(1), lang)
        unit = next((UNITS[i][0] for i in range(len(UNITS)) if m.group(f"u{i}")), "")
        if v is not None and unit:
            with_units[(v, unit)] += 1
            spans.append((m.start(), m.end()))
    rest = _blank(text, spans)
    models, mspans = Counter(), []
    for m in TOKEN.finditer(rest):                       # обозначения; числа внутри них не считаются отдельно
        tok = m.group(0)
        mspans.append((m.start(), m.end()))
        if any(c.isupper() for c in tok) and re.search(r"[A-ZА-Я]", tok):
            models[anorm(tok)] += 1
    rest = _blank(rest, mspans)
    plain = Counter()
    for m in re.finditer(rf"(?<![\w.,])({num})(?![\w]|[.,]\d)", rest):
        v = _canon(m.group(1), lang)
        if v is not None and v not in ("0", "1"):         # «1», «0» — слишком часто в тексте
            plain[v] += 1
    return with_units, plain, models


def _pages(jid):
    p = os.path.join(core.job_dir(jid), "dump.json")
    if not os.path.exists(p):
        return None
    dump = json.load(open(p, encoding="utf-8"))
    return [" ".join(ln["text"] for ln in pg.get("lines", [])) for pg in dump.get("lines", [])]


def _fmt(item, lang):
    if isinstance(item, tuple):
        v, u = item
        v = v.replace(".", ",") if lang == "ru" else v
        return f"{v} {u}" if u != "%" else f"{v}%"
    return item.replace(".", ",") if lang == "ru" and re.fullmatch(r"[\d.]+", item) else item


def diff(ca, cb):
    only_a, only_b = ca - cb, cb - ca
    return sorted(only_a.elements(), key=str), sorted(only_b.elements(), key=str)


def compare(a, b):
    ja, jb = core.get_job(a), core.get_job(b)
    pa, pb = _pages(a), _pages(b)
    if not (ja and jb and pa is not None and pb is not None):
        return None
    la, lb = lang_of(" ".join(pa)), lang_of(" ".join(pb))
    by_page = len(pa) == len(pb)
    blocks = list(zip(range(1, len(pa) + 1), pa, pb)) if by_page else [(0, " ".join(pa), " ".join(pb))]
    rows, totals = [], Counter()
    for page, ta, tb in blocks:
        ea, eb = extract(ta, la), extract(tb, lb)
        for kind, i in (("units", 0), ("models", 2), ("numbers", 1)):
            oa, ob = diff(ea[i], eb[i])
            totals[kind + "_a"] += sum(ea[i].values())
            if oa or ob:
                rows.append({"page": page, "kind": kind, "only_a": [_fmt(x, la) for x in oa][:40],
                             "only_b": [_fmt(x, lb) for x in ob][:40]})
                totals[kind] += len(oa) + len(ob)
    labels = {"units": "значения с единицами", "models": "обозначения изделий", "numbers": "числа"}
    for r in rows:
        r["label"] = labels[r["kind"]]
    return {"a": {"id": a, "name": ja["name"], "pages": len(pa), "lang": la},
            "b": {"id": b, "name": jb["name"], "pages": len(pb), "lang": lb},
            "by_page": by_page, "rows": rows, "totals": dict(totals),
            "ok": not rows, "same_lang": la == lb}


def rows_csv(res):
    yield ["Страница", "Что", f"Только в № {res['a']['id']}", f"Только в № {res['b']['id']}"]
    for r in res["rows"]:
        yield [r["page"] or "весь документ", r["label"], "; ".join(r["only_a"]), "; ".join(r["only_b"])]
