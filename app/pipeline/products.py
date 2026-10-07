# -*- coding: utf-8 -*-
"""Сверка с справочником продукции (выгрузка из 1С, ERP или PIM в CSV / XLSX).

Справочник: первая колонка — артикул, далее — наименование и характеристики. Единица измерения берется из
заголовка колонки: «Мощность, кВт», «Напор (м)», «Расход [м3/ч]». Колонки без единицы не сверяются.

Проверки документа:
  * артикул похож на артикул справочника, но отличается 1–2 знаками → «возможна опечатка в артикуле»;
  * артикул с префиксом серии из справочника, которого в справочнике нет → «артикул не найден в справочнике»;
  * в строке (строке таблицы) с одним артикулом справочника значение с единицей колонки отличается от
    справочного больше допуска → «расхождение с справочником».
"""
import csv
import io
import json
import os
import re
from collections import Counter, defaultdict

def PATH():
    return os.path.join(os.environ.get("KORREKTOR_DATA") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data"), "products.json")


LOOK = str.maketrans("АВЕКМНОРСТХУаеоркхсу", "ABEKMHOPCTXYAEOPKXCY")
TOKEN = re.compile(r"(?<![\w\-/.])(?=[\w\-/.]*\d)(?=[\w\-/.]*[A-Za-zА-Яа-яЁё])[A-Za-zА-Яа-яЁё0-9][\w\-/.]{1,38}[A-Za-zА-Яа-яЁё0-9](?![\w\-/])")
UNIT_IN_HEADER = re.compile(r"^(.*?)\s*(?:,|\(|\[)\s*([^()\[\],]{1,15}?)\s*[)\]]?\s*$")


def norm(a):
    return re.sub(r"\s+", "", str(a or "")).upper().translate(LOOK).replace("Ё", "Е")


def norm_unit(u):
    return (u or "").replace("³", "3").replace("²", "2").replace(" ", "").replace(" ", "").lower()


def num(v):
    s = str(v or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _rows_from_bytes(data, filename):
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [["" if c is None else str(c) for c in row] for row in ws.iter_rows(values_only=True)]
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("файл не в UTF-8 и не в Windows-1251")
    sample = text[:5000]
    delim = max(";,\t", key=sample.count)
    return list(csv.reader(io.StringIO(text), delimiter=delim))


def parse(data, filename="products.csv"):
    """→ справочник {"columns": [...], "units": {...}, "items": {нормированный артикул: {...}}, "file": имя}."""
    rows = [r for r in _rows_from_bytes(data, filename) if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("в файле нет строк: нужна строка заголовков и хотя бы одна строка данных")
    head = [str(h).strip() for h in rows[0]]
    units = {}
    for i, h in enumerate(head[1:], 1):
        m = UNIT_IN_HEADER.match(h)
        if m and m.group(2).strip() and not m.group(2).strip().isdigit():
            units[i] = (m.group(1).strip() or h, m.group(2).strip())
    items = {}
    for r in rows[1:]:
        art = str(r[0]).strip() if r else ""
        if not art or len(art) > 60:
            continue
        params = {}
        for i, (name, unit) in units.items():
            if i < len(r) and num(r[i]) is not None:
                params[name] = {"value": num(r[i]), "unit": unit, "raw": str(r[i]).strip()}
        items[norm(art)] = {"article": art, "name": str(r[1]).strip() if len(r) > 1 and 1 not in units else "",
                            "params": params}
    if not items:
        raise ValueError("не найдено ни одного артикула в первой колонке")
    return {"columns": head, "units": {v[0]: v[1] for v in units.values()}, "items": items,
            "file": os.path.basename(filename)}


def save(data, filename, path):
    ref = parse(data, filename)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ref, fh, ensure_ascii=False)
    os.replace(tmp, path)
    return ref


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _lev(a, b, cap=2):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _rows(dump):
    """Строки страниц, склеенные по высоте: ячейки одной строки таблицы → одна строка."""
    for pg in dump.get("lines", []):
        groups = []
        for ln in sorted(pg.get("lines", []), key=lambda x: x.get("y", 0)):
            if groups and abs(groups[-1][0] - ln.get("y", 0)) <= 2.5:
                groups[-1][1].append(ln["text"])
            else:
                groups.append([ln.get("y", 0), [ln["text"]]])
        for _, texts in groups:
            yield pg.get("pdf_page", pg.get("page", 1)), "  ".join(t.strip() for t in texts)


def check(dump, ref, st):
    items = ref.get("items") or {}
    if not items:
        return None
    tol = float(st.get("products_tolerance") or 0.5) / 100
    by_prefix = defaultdict(list)                      # индекс: (2 первых знака, длина) → артикулы
    for k in items:
        by_prefix[(k[:2], len(k))].append(k)
    series = Counter(m.group(0) for k in items for m in [re.match(r"[A-Z]{2,}", k)] if m)
    series = {p for p, n in series.items() if n >= 3}
    found, issues, seen = set(), [], set()
    unit_re = {}
    for name, unit in (ref.get("units") or {}).items():
        u = r"\s?".join(re.escape(ch) for ch in norm_unit(unit))
        unit_re[name] = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*" + u + r"(?![\w])", re.I)
    for page, row in _rows(dump):
        known = []
        for m in TOKEN.finditer(row):
            tok = m.group(0)
            k = norm(tok)
            if k in items:
                found.add(k)
                known.append(k)
                continue
            if (k, page) in seen or len(k) < 4 or not any(ch.isupper() for ch in tok):
                continue                                # «м3/ч», «мм2» и т. п. — единицы, а не артикулы
            cap = 2 if len(k) >= 8 else 1
            cand = [a for n in range(len(k) - cap, len(k) + cap + 1) for a in by_prefix.get((k[:2], n), [])][:3000]
            close = sorted((d, abs(len(a) - len(k)), a) for a in cand for d in [_lev(k, a, cap)] if d <= cap)
            close = [a for _, _, a in close]
            if close:
                seen.add((k, page))
                issues.append({"kind": "typo", "page": page, "article": tok, "suggest": items[close[0]]["article"],
                               "text": row[:200], "title": "Возможна опечатка в артикуле",
                               "detail": f"«{tok}» нет в справочнике; похожий артикул: «{items[close[0]]['article']}»"})
            elif st.get("products_unknown", True) and any(k.startswith(p) and len(k) > len(p) and not k[len(p)].isalpha()
                                                          for p in series):
                seen.add((k, page))
                issues.append({"kind": "unknown", "page": page, "article": tok, "suggest": "", "text": row[:200],
                               "title": "Артикул не найден в справочнике",
                               "detail": f"«{tok}» похож на артикул серии из справочника, но в справочнике его нет"})
        if len(set(known)) != 1 or not unit_re:
            continue
        it = items[known[0]]
        low = row.replace("³", "3").replace("²", "2").lower()
        for name, rx in unit_re.items():
            p = it["params"].get(name)
            if not p:
                continue
            vals = [num(v) for v in rx.findall(low)]
            vals = [v for v in vals if v is not None]
            if not vals or any(abs(v - p["value"]) <= max(abs(p["value"]) * tol, 1e-9) for v in vals):
                continue
            issues.append({"kind": "param", "page": page, "article": it["article"], "suggest": f"{p['raw']} {p['unit']}",
                           "text": row[:200], "title": f"Расхождение со справочником: {name}",
                           "detail": f"{it['article']}: в документе {str(round(vals[0], 6)).rstrip('0').rstrip('.').replace('.', ',')} {p['unit']}, в справочнике {p['raw']} {p['unit']}"})
        if len(issues) >= 500:
            break
    grouped = {}                                       # одно замечание на артикул и расхождение — со списком страниц
    for it in issues:
        g = grouped.setdefault((it["kind"], it["article"], it["detail"]), dict(it, pages=[]))
        if it["page"] not in g["pages"]:
            g["pages"].append(it["page"])
    issues = list(grouped.values())
    return {"loaded": len(items), "file": ref.get("file", ""), "found": len(found),
            "found_list": sorted(items[k]["article"] for k in found)[:300], "issues": issues[:500],
            "ok": not issues}
