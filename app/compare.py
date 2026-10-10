# -*- coding: utf-8 -*-
"""Сравнение двух версий документа: какие замечания исправлены, какие остались, какие появились.

Замечание старой версии считается:
  «исправлено»        — в новой версии такого замечания нет и ошибочного фрагмента нет в тексте;
  «осталось»          — такое же замечание есть в новой версии (то же правило, та же ошибка и исправление)
                        или ошибочный фрагмент по-прежнему есть в тексте;
  «нет в отчете»      — замечания нет, но фрагмент в тексте остался (правило выключено или замечание отклонено).
Замечания новой версии без пары в старой — «новые». Отклоненные корректором (✕) замечания не учитываются.
"""
import difflib
import json
import os
import re

from . import analytics, core
from .pipeline.common import PairCache, norm, safe_dpi

SKIP_RULES = ("batch", "candidate")


def page_texts(jid):
    p = os.path.join(core.job_dir(jid), "dump.json")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as fh:
        dump = json.load(fh)
    return [" ".join(pg.get("head", []) + [ln["text"] for ln in pg.get("lines", [])] + pg.get("foot", []))
            for pg in dump.get("lines", [])]


SPACE_RULES = ("dspace", "dot", "digitword", "unit", "equals", "times", "pct", "thousands", "mwc", "degree", "range")


def still_there(f, raw, normed, flat=None):
    """Ошибочный фрагмент остался в тексте новой версии?
    Пробельные и знаковые правила, а также правки только регистра, пробелов или «ё» сравниваются по точному тексту.
    flat — текст с одиночными пробелами (вычисляется один раз на сравнение, а не на каждое замечание)."""
    bad = f.get("bad") or ""
    good = f.get("good") or ""
    nb = norm(bad).lower()
    exact = (f.get("rule") in SPACE_RULES or f.get("rule") in ("latin", "mixed", "brand", "quotes")
             or (good not in ("", "—") and nb == norm(good).lower()))
    if exact:
        flat = flat if flat is not None else re.sub(r"[ \t]+", " ", raw)
        return len(bad.strip()) >= 2 and (bad in raw or re.sub(r"[ \t]+", " ", bad) in flat)
    return len(nb) >= 3 and nb in normed


def key(f):
    return (f.get("rule") or "", norm(f.get("bad")).lower(), norm(f.get("good")).lower())


_cache = PairCache()


def compare(a, b):
    ja, jb = core.get_job(a), core.get_job(b)
    if not (ja and jb):
        return None
    key = ("versions", a, b, PairCache.stamp(core.job_dir(a)), PairCache.stamp(core.job_dir(b)),
           ja.get("name"), jb.get("name"), ja.get("status"), jb.get("status"))
    return _cache.get(key, lambda: _compare(a, b, ja, jb))


def similarity(x, y):
    """Сходство текстов страниц, %: по словам (символьное сравнение длинных страниц квадратичное — на каталоге
    в тысячу страниц оно занимало десятки минут). Очень длинные страницы — оценкой по составу слов."""
    wx, wy = x.split(), y.split()
    if not wx or not wy:
        return 0.0
    if len(wx) + len(wy) > 6000:                       # очень длинные страницы — по общим тройкам слов (линейно)
        ga = {tuple(wx[i:i + 3]) for i in range(max(1, len(wx) - 2))}
        gb = {tuple(wy[i:i + 3]) for i in range(max(1, len(wy) - 2))}
        return len(ga & gb) / max(1, len(ga | gb))
    return difflib.SequenceMatcher(None, wx, wy, autojunk=False).ratio()


def _compare(a, b, ja, jb):
    sa, sb = core.load_state(a), core.load_state(b)
    if not (ja and jb and sa and sb):
        return None
    fa = [f for f in sa["findings"] if f.get("status") != "rejected" and f.get("rule") not in SKIP_RULES]
    fb = [f for f in sb["findings"] if f.get("status") != "rejected" and f.get("rule") not in SKIP_RULES]
    tb = page_texts(b)
    raw_b = " ".join(tb)
    text_b = norm(raw_b).lower()
    flat_b = re.sub(r"[ \t]+", " ", raw_b)
    used = set()
    fixed, remain, hidden = [], [], []
    # три прохода: точная пара (правило, ошибка, исправление) → (правило, ошибка) → только ошибка:
    # ИИ и LanguageTool от запуска к запуску формулируют исправление по-разному
    keys = (key, lambda f: (f.get("rule") or "", norm(f.get("bad")).lower()), lambda f: norm(f.get("bad")).lower())
    pending = list(fa)
    for kf in keys:
        idx = {}
        for x in fb:
            if id(x) not in used:
                idx.setdefault(kf(x), []).append(x)
        rest = []
        for f in pending:
            cand = [x for x in idx.get(kf(f), []) if id(x) not in used] if kf(f) not in ("", ("", "")) else []
            if cand:
                m = min(cand, key=lambda x: abs(x["page"] - f["page"]))
                used.add(id(m))
                remain.append({"old": f, "new": m})
            else:
                rest.append(f)
        pending = rest
    for f in pending:
        if still_there(f, raw_b, text_b, flat_b):
            hidden.append({"old": f})
        else:
            fixed.append({"old": f})
    new = [f for f in fb if id(f) not in used]

    # какие страницы изменились (по тексту)
    ta = page_texts(a)
    changed = []
    for i in range(max(len(ta), len(tb))):
        x = ta[i] if i < len(ta) else ""
        y = tb[i] if i < len(tb) else ""
        if norm(x) != norm(y):
            r = similarity(x, y)
            changed.append({"page": i + 1, "similarity": round(100 * r)})
    da = round(10.0 * len(fa) / max(1, sa.get("pages") or 1), 2)
    db = round(10.0 * len(fb) / max(1, sb.get("pages") or 1), 2)
    return {
        "a": dict(ja, settings=None, pages=sa.get("pages"), density=da, count=len(fa)),
        "b": dict(jb, settings=None, pages=sb.get("pages"), density=db, count=len(fb)),
        "fixed": fixed, "remain": remain, "hidden": hidden, "new": new,
        "fixed_pct": round(100.0 * len(fixed) / len(fa), 1) if fa else None,
        "density_change": round(100.0 * (db - da) / da, 1) if da else None,
        "changed_pages": changed,
        "same_family": analytics.family_of(ja["name"]) == analytics.family_of(jb["name"]),
    }


def candidates(jid, owner=None):
    """Задания для сравнения: сначала версии того же семейства, затем остальные готовые."""
    job = core.get_job(jid)
    fam = analytics.family_of(job["name"]) if job else ""
    rows = [j for j in core.list_jobs(2000, owner=owner) if j["status"] == "done" and j["id"] != jid]
    same = [j for j in rows if analytics.family_of(j["name"]) == fam]
    other = [j for j in rows if analytics.family_of(j["name"]) != fam][:200]
    return same, other


def default_pair(jid, owner=None):
    """Для задания — предыдущая версия того же семейства (по дате загрузки)."""
    job = core.get_job(jid)
    same, _ = candidates(jid, owner)
    older = [j for j in same if (j["created"], j["id"]) < (job["created"], job["id"])]
    if older:
        return max(older, key=lambda j: (j["created"], j["id"]))["id"], jid
    newer = [j for j in same if (j["created"], j["id"]) > (job["created"], job["id"])]
    if newer:
        return jid, min(newer, key=lambda j: (j["created"], j["id"]))["id"]
    return None, jid


def rows_csv(res):
    out = [["Результат", "Стр. было", "Стр. стало", "Тип", "Ошибка", "Как правильно", "Пояснение"]]
    for r in res["fixed"]:
        f = r["old"]
        out.append(["исправлено", f["page"], "", f["type"], f["bad"], f["good"], f["note"]])
    for r in res["remain"]:
        f, g = r["old"], r["new"]
        out.append(["осталось", f["page"], g["page"], f["type"], f["bad"], f["good"], f["note"]])
    for r in res["hidden"]:
        f = r["old"]
        out.append(["нет в отчете, фрагмент остался", f["page"], "", f["type"], f["bad"], f["good"], f["note"]])
    for f in res["new"]:
        out.append(["новое", "", f["page"], f["type"], f["bad"], f["good"], f["note"]])
    return out


def safe_name(s):
    return re.sub(r"[^\w.-]+", "_", s)[:60]


def page_diff_png(a, b, page, side, dpi=110):
    """Изображение страницы с выделенными изменениями: на старой версии — удаленные слова (красные рамки),
    на новой — добавленные (зеленые). → bytes PNG или None."""
    import pymupdf
    pa = os.path.join(core.job_dir(a), "source.pdf")
    pb = os.path.join(core.job_dir(b), "source.pdf")
    if not (os.path.exists(pa) and os.path.exists(pb)):
        return None
    from .pipeline.common import pdf_lock
    with pdf_lock():                                 # PyMuPDF — по одному потоку (см. PDF_LOCK)
        with pymupdf.open(pa) as da, pymupdf.open(pb) as db:
            if page < 1 or (side == "a" and page > da.page_count) or (side == "b" and page > db.page_count):
                return None
            wa = da[page - 1].get_text("words") if page <= da.page_count else []
            wb = db[page - 1].get_text("words") if page <= db.page_count else []
    ta, tb = [w[4] for w in wa], [w[4] for w in wb]
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)       # сравнение — без блокировки PDF
    removed, added = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("delete", "replace"):
            removed += wa[i1:i2]
        if op in ("insert", "replace"):
            added += wb[j1:j2]
    path, words, color = (pa, removed, (0.82, 0.23, 0.23)) if side == "a" else (pb, added, (0.05, 0.64, 0.05))
    with pdf_lock(), pymupdf.open(path) as doc:
        pg = doc[page - 1]
        try:
            dpi = safe_dpi(pg, dpi)
        except ValueError:                             # страница огромного размера — изображения нет
            return None
        for w in words:
            r = pymupdf.Rect(w[:4]) + (-1, -1, 1, 1)
            pg.draw_rect(r, color=color, width=1.4, fill=color, fill_opacity=0.18, overlay=True)
        return pg.get_pixmap(dpi=dpi).tobytes("png"), len(words)
