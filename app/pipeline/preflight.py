# -*- coding: utf-8 -*-
"""Технический контроль PDF (preflight), OCR страниц-сканов, PDF с пометками."""
from collections import Counter

import pymupdf

from .common import safe_dpi


def preflight(pdf_path, st, dump):
    with pymupdf.open(pdf_path) as doc:
        return _preflight(doc, st, dump)


def _image_boxes(page):
    """Положение и размер (в пикселях) всех картинок страницы за один проход. page.get_image_rects(xref) на каждую
    картинку распаковывал ее целиком и пересчитывал отпечатки всех картинок страницы — на каталоге с крупными
    фотографиями это сотни мегабайт и минуты на страницу."""
    try:
        return [(pymupdf.Rect(i["bbox"]), i.get("width") or 0, i.get("height") or 0) for i in page.get_image_info()]
    except Exception:  # noqa
        return []


def _preflight(doc, st, dump):
    res = {"fonts_not_embedded": [], "low_dpi": [], "page_sizes": {}, "scans": [], "empty": dump.get("empty_pages", []),
           "placeholders": sorted({r["page"] for r in dump.get("placeholders", [])}), "items": [], "curves": []}
    sizes = Counter()
    fonts = {}
    min_dpi = int(st.get("preflight_min_dpi") or 0)
    for pno, page in enumerate(doc, 1):
        w, h = round(page.rect.width / 72 * 25.4), round(page.rect.height / 72 * 25.4)
        sizes[(w, h)] += 1
        if st.get("preflight_fonts"):
            for f in page.get_fonts(full=True):
                name, ext = f[3], f[1]
                if not ext or ext == "n/a":
                    fonts.setdefault(name, []).append(pno)
        txt = page.get_text().strip()
        imgs = page.get_images(full=True)
        boxes = _image_boxes(page) if imgs and ((len(txt) < 20) or min_dpi) else []
        if len(txt) < 20 and imgs:
            area = page.rect.width * page.rect.height
            big = any(r.width * r.height > area * 0.6 for r, _, _ in boxes)
            if big and len(txt) < 5:
                res["scans"].append(pno)          # без текста: иллюстрация или скан — уточняет OCR
        if len(txt) < 5 and not imgs:
            try:
                curves = sum(1 for dr in page.get_drawings() if "f" in (dr.get("type") or "")
                             for it in dr["items"] if it[0] == "c")
                if curves >= 150:                 # много залитых кривых без текста — буквы переведены в кривые
                    res["curves"].append(pno)
            except Exception:  # noqa
                pass
        if min_dpi:
            for r, iw, ih in boxes:
                if r.width < 30 or r.height < 30 or not iw or not ih:
                    continue
                dpi = min(iw / (r.width / 72), ih / (r.height / 72))
                if dpi < min_dpi:
                    res["low_dpi"].append({"page": pno, "dpi": int(dpi), "px": f"{iw}×{ih}"})
    res["fonts_not_embedded"] = [{"font": k, "pages": sorted(set(v))[:20]} for k, v in fonts.items()]
    res["page_sizes"] = {f"{w}×{h} мм": n for (w, h), n in sizes.most_common()}
    # сводка строками
    it = res["items"]
    if len(sizes) > 1:
        it.append("Размеры страниц различаются: " + ", ".join(f"{k} — {v} стр." for k, v in res["page_sizes"].items()))
    for f in res["fonts_not_embedded"]:
        it.append(f"Шрифт «{f['font']}» не встроен (стр. {', '.join(map(str, f['pages']))})")
    if res["low_dpi"]:
        pages = sorted({x["page"] for x in res["low_dpi"]})
        worst = min(x["dpi"] for x in res["low_dpi"])
        it.append(f"Изображения ниже {min_dpi} dpi: стр. {', '.join(map(str, pages[:20]))} (минимум {worst} dpi)")
    if res["curves"]:
        it.append("Текст переведен в кривые (нет текстового слоя, страница не проверялась): стр. "
                  + ", ".join(map(str, res["curves"][:30])))
    if res["scans"]:
        it.append("Страницы без текстового слоя (изображение во всю страницу): " + ", ".join(map(str, res["scans"])))
    return res


def ocr_pages(pdf_path, pages, dump, log):
    """Распознает страницы-сканы Tesseract и добавляет их строки в dump."""
    if not pages:
        return []
    with pymupdf.open(pdf_path) as doc:
        return _ocr_pages(doc, pages, dump, log)


def _ocr_pages(doc, pages, dump, log):
    done = []
    for pno in pages:
        try:
            dpi = safe_dpi(doc[pno - 1], 300)
        except ValueError as e:                        # страница огромного размера — пропускается, остальные — нет
            log(f"OCR: стр. {pno} пропущена ({e})")
            continue
        try:
            tp = doc[pno - 1].get_textpage_ocr(language="rus+eng", dpi=dpi, full=True)
            d = doc[pno - 1].get_text("dict", textpage=tp)
        except Exception as e:  # noqa
            log(f"OCR недоступен ({e}); страницы-сканы не распознаны")
            return done
        lines = []
        for bi, b in enumerate(d["blocks"]):
            bl = b.get("lines", [])
            for li, ln in enumerate(bl):
                t = "".join(s["text"] for s in ln["spans"]).rstrip()
                if t.strip():
                    lines.append({"block": bi, "line": li, "y": round(ln["bbox"][1], 1), "text": t,
                                  "last": li == len(bl) - 1})
        words = [w for ln in lines for w in ln["text"].split() if len(w) >= 3 and w.isalpha()]
        if len(words) < 15:
            log(f"OCR: на стр. {pno} текста почти нет — это иллюстрация, а не скан")
            continue
        for p in dump["lines"]:
            if p["pdf_page"] == pno:
                p["lines"] = lines
                p["ocr"] = True
        done.append(pno)
    return done


# флаги текстового слоя, с которыми page.search_for ищет сам (переносы склеиваются, пробелы сохраняются)
SEARCH_FLAGS = (pymupdf.TEXT_DEHYPHENATE | pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES
                | pymupdf.TEXT_MEDIABOX_CLIP)


def annotate(pdf_path, findings, out_path):
    """Копия PDF с желтой подсветкой ошибок и всплывающими примечаниями."""
    with pymupdf.open(pdf_path) as doc:
        return _annotate(doc, findings, out_path)


def _annotate(doc, findings, out_path):
    placed = 0
    cur = {"no": None, "page": None, "tp": None}      # текстовый слой — один на страницу и только текущей страницы
    for i, f in enumerate(findings, 1):               # (замечания отсортированы по страницам)
        if f.get("status") == "rejected":
            continue
        if not 0 < f["page"] <= doc.page_count:
            continue
        if cur["no"] != f["page"]:
            pg = doc[f["page"] - 1]
            cur.update(no=f["page"], page=pg, tp=pg.get_textpage(flags=SEARCH_FLAGS))
        page, tp = cur["page"], cur["tp"]
        needles = []
        for part in [p.strip() for p in str(f.get("bad", "")).split(" … ")]:
            if part and part not in {"—", "-", "–"}:
                needles.append(part)
        needles.append(f["src"])
        rects = []
        for nd in needles:
            rects = page.search_for(nd.replace("\xa0", " "), quads=False, textpage=tp)
            if not rects and len(nd) > 40:
                rects = page.search_for(nd[:40], textpage=tp)
            if rects:
                break
        text = (f"№ {i}. {f['type']}\nКак правильно: {f['fix']}\n{f['note']}").replace(" ", " ")
        if rects:
            an = page.add_highlight_annot(rects[:4])
            an.set_colors(stroke=(1, 0.85, 0) if f["type"] != "Требует проверки автором" else (0.55, 0.8, 1))
            an.set_info(title=f"Корректор, № {i}", content=text)
            an.update()
            placed += 1
        else:
            an = page.add_text_annot(pymupdf.Point(20, 20 + (i % 30) * 14), text)
            an.set_info(title=f"Корректор, № {i}")
            an.update()
    doc.save(out_path, garbage=3, deflate=True)
    return placed
