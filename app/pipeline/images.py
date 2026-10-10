# -*- coding: utf-8 -*-
"""Изображения: одинаковые картинки у разных изделий.

В каталогах частая ошибка — фото или чертеж одного изделия, по недосмотру поставленный и к другому. Для каждого
изображения на странице считается «отпечаток» (разностный хеш dHash 9×8 по уменьшенной копии области страницы) и
подпись — ближайшая строка текста (заголовок или артикул рядом с картинкой). Одинаковые или почти одинаковые
картинки (расстояние Хэмминга ≤ порога) с разными подписями попадают в блок «Изображения» на странице задания.
Логотипы и значки (одна картинка больше чем в N местах), фон во всю страницу и мелкие пиктограммы не учитываются.
"""
import re
import pymupdf

MIN_SIDE = 40          # пт: меньше — пиктограмма
MAX_SHARE = 0.8        # доля площади страницы: больше — фон


def _dhash(dl, rect):
    """64-битный разностный хеш области страницы или None для однотонной области (заливка, пустая рамка).
    dl — список отображения страницы (DisplayList): строится один раз на страницу."""
    scale = 72.0 / max(rect.width, rect.height)        # область → примерно 72 пикселя по большей стороне
    pix = dl.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, colorspace=pymupdf.csGRAY, alpha=False)
    if not pix.samples or max(pix.samples) - min(pix.samples) < 12:
        return None
    small = pymupdf.Pixmap(pix, 9, 8, None)
    s, n = small.samples, small.n
    bits = 0
    for y in range(8):
        for x in range(8):
            a, b = s[(y * 9 + x) * n], s[(y * 9 + x + 1) * n]
            bits = (bits << 1) | (1 if a > b else 0)
    return bits


def _has_article(text, known, memo):
    """Есть ли в строке артикул справочника. Сравнение по обозначениям из строки с множеством артикулов
    (прежде строка сравнивалась с каждым из десятков тысяч артикулов — на каждую картинку и строку рядом)."""
    hit = memo.get(text)
    if hit is None:
        from .products import TOKEN, norm
        hit = memo[text] = any(norm(m.group(0)) in known for m in TOKEN.finditer(text))
    return hit


def _label(lines, rect, known=None, memo=None):
    """Подпись картинки: артикул из справочника рядом (если есть) или ближайшая строка текста.
    known — множество нормализованных артикулов справочника."""
    memo = memo if memo is not None else {}
    best, bestd = "", 1e9
    cx = (rect.x0 + rect.x1) / 2
    for (x0, y0, x1, y1), text in lines:
        t = text.strip()
        if len(t) < 3 or re.fullmatch(r"[\d\s.,–-]+", t):
            continue
        if x1 < rect.x0 - 60 or x0 > rect.x1 + 60:      # другая колонка
            continue
        dy = rect.y0 - y1 if y1 <= rect.y0 else (y0 - rect.y1 if y0 >= rect.y1 else 0)
        if dy > 120:
            continue
        d = dy + abs((x0 + x1) / 2 - cx) * 0.2
        if known and _has_article(t, known, memo):
            d -= 80                                     # строка с артикулом важнее
        if d < bestd:
            best, bestd = t, d
    return best[:80]


def check(pdf_path, st, articles=None):
    if not st.get("images_duplicates", True):
        return None
    thr = int(st.get("images_hash_distance") if st.get("images_hash_distance") is not None else 4)
    max_rep = int(st.get("images_max_repeats") or 3)
    from .products import norm as _anorm
    known_articles = {_anorm(a) for a in (articles or [])}
    memo = {}
    occ = []
    with pymupdf.open(pdf_path) as doc:
        for pno, page in enumerate(doc, 1):
            area = page.rect.width * page.rect.height
            lines = dl = None
            try:
                infos = page.get_image_info()               # положение картинок (без xref: тот не нужен и дорог)
            except Exception:  # noqa
                infos = []
            for info in infos:
                xref = info.get("xref") or 0
                for r in [pymupdf.Rect(info["bbox"])]:
                    r = r & page.rect
                    if r.is_empty or r.width < MIN_SIDE or r.height < MIN_SIDE or r.width * r.height > area * MAX_SHARE:
                        continue
                    try:
                        dl = dl or page.get_displaylist()
                        h = _dhash(dl, r)
                    except Exception:  # noqa
                        continue
                    if h is None:
                        continue
                    if lines is None:
                        lines = [((b[0], b[1], b[2], b[3]), b[4]) for b in page.get_text("blocks") if b[6] == 0]
                    occ.append({"page": pno, "xref": xref, "hash": h, "rect": [round(v, 1) for v in r],
                                "label": _label(lines, r, known_articles, memo)})
            if len(occ) > 3000:
                break
    # группы одинаковых картинок: тот же объект (xref) или близкий хеш
    groups, used = [], set()
    for i, a in enumerate(occ):
        if i in used:
            continue
        g = [a]
        used.add(i)
        for j in range(i + 1, len(occ)):
            if j in used:
                continue
            b = occ[j]
            if bin(a["hash"] ^ b["hash"]).count("1") <= thr:
                g.append(b)
                used.add(j)
        if len(g) > 1:
            groups.append(g)
    from .products import TOKEN, norm
    known = {norm(a) for a in (articles or [])}

    def models(label):
        """Обозначения изделий в подписи (SPL-1010, EVR 32, БТП-200); со справочником — только его артикулы."""
        toks = {norm(m.group(0)) for m in TOKEN.finditer(label or "") if any(c.isupper() for c in m.group(0))}
        return {t for t in toks if t in known} if known else toks

    dups = []
    for g in groups:
        if len(g) > max_rep:
            continue                                    # логотип, значок, повторяющийся элемент оформления
        sets = [models(o["label"]) for o in g]
        sets = [x for x in sets if x]
        if not any(a.isdisjoint(b) for i, a in enumerate(sets) for b in sets[i + 1:]):
            continue                                    # подписи без разных обозначений — повтор, скорее всего, намеренный
        dups.append({"kind": "same" if len({o["hash"] for o in g}) == 1 else "similar",
                     "places": [{"page": o["page"], "label": o["label"], "rect": o["rect"]} for o in g]})
    return {"images": len(occ), "duplicates": dups[:100], "ok": not dups}
