# -*- coding: utf-8 -*-
"""Соответствие корпоративному стандарту оформления: шрифты, кегли, цвета текста, поля страницы.

Проверка идет по самому PDF (как в программах допечатной подготовки): каждый фрагмент текста — шрифт, кегль,
цвет и положение на странице. Результат — отдельный блок «Стандарт оформления» в задании и лист в выгрузке;
на 25 проверок отчета он не влияет. Без заданного стандарта собирается только профиль оформления документа
(какие шрифты, кегли и цвета в нем используются) — его удобно взять за основу стандарта.
"""
import re
from collections import Counter, defaultdict

import pymupdf

SUFFIXES = re.compile(r"(MT|PS|PSMT|Std|Pro|LT|OT|TT)$")


def family(font):
    """«ABCDEF+Arial-BoldItalicMT» → «arial»; «PTSans-Regular» → «ptsans»; «Times New Roman,Bold» → «timesnewroman»."""
    f = (font or "").split("+", 1)[-1]
    f = re.split(r"[-,]", f, maxsplit=1)[0]
    f = SUFFIXES.sub("", f.replace(" ", ""))
    return f.lower()


def _allowed_fonts(st):
    return [family(x.strip()) for x in re.split(r"[,;\n]", st.get("style_fonts") or "") if x.strip()]


def _hex(c):
    return f"#{c:06x}"


def _rgb(h):
    h = h.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return None
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _allowed_colors(st):
    return [c for c in (_rgb(x) for x in re.split(r"[,;\s]+", st.get("style_colors") or "") if x.strip()) if c]


def _sizes(st):
    out = []
    for x in re.split(r"[,;\s]+", st.get("style_sizes") or ""):
        try:
            out.append(float(x.replace(",", ".")))
        except ValueError:
            pass
    return out


def _g(x):
    return f"{x:g}".replace(".", ",")


def configured(st):
    return bool(_allowed_fonts(st) or _allowed_colors(st) or _sizes(st) or float(st.get("style_min_size") or 0)
                or float(st.get("style_margin_mm") or 0) or int(st.get("style_max_fonts") or 0))


def check(pdf_path, st):
    allowed_f, allowed_c, sizes = _allowed_fonts(st), _allowed_colors(st), _sizes(st)
    min_size = float(st.get("style_min_size") or 0)
    margin = float(st.get("style_margin_mm") or 0) * 72 / 25.4
    tol_c = float(st.get("style_color_tolerance") if st.get("style_color_tolerance") is not None else 40)
    max_fonts = int(st.get("style_max_fonts") or 0)
    fonts, colors, ksizes = Counter(), Counter(), Counter()
    font_names = defaultdict(set)
    issues = {}                                        # (вид, ключ) → {pages, chars, sample}

    def hit(kind, key, pno, text, title, detail):
        it = issues.setdefault((kind, key), {"kind": kind, "key": key, "title": title, "detail": detail,
                                             "pages": set(), "chars": 0, "sample": ""})
        it["pages"].add(pno)
        it["chars"] += len(text)
        if not it["sample"]:
            it["sample"] = text.strip()[:60]

    with pymupdf.open(pdf_path) as doc:
        for pno, page in enumerate(doc, 1):
            W, H = page.rect.width, page.rect.height
            for b in page.get_text("dict", flags=0)["blocks"]:
                for ln in b.get("lines", []):
                    for sp in ln.get("spans", []):
                        text = sp.get("text") or ""
                        if not text.strip() or sp.get("size", 0) < 1:
                            continue
                        n = len(text.strip())
                        fam, size, col = family(sp["font"]), round(sp["size"] * 2) / 2, sp.get("color", 0)
                        fonts[fam] += n
                        font_names[fam].add(sp["font"].split("+", 1)[-1])
                        ksizes[size] += n
                        colors[_hex(col)] += n
                        if allowed_f and not any(fam == a or fam.startswith(a) for a in allowed_f):
                            hit("font", fam, pno, text, f"Шрифт вне стандарта: {sp['font'].split('+', 1)[-1]}",
                                "Разрешены: " + (st.get("style_fonts") or "").strip())
                        if min_size and sp["size"] < min_size - 0.05:
                            hit("min_size", size, pno, text, f"Кегль {_g(size)} пт меньше минимального {_g(min_size)} пт",
                                "Мелкий текст плохо читается в печати")
                        elif sizes and not any(abs(sp["size"] - s) <= 0.3 for s in sizes):
                            hit("size", size, pno, text, f"Кегль {_g(size)} пт вне шкалы стандарта",
                                "Разрешенные кегли: " + "; ".join(_g(s) for s in sizes))
                        if allowed_c:
                            rgb = ((col >> 16) & 255, (col >> 8) & 255, col & 255)
                            if min(sum((a - b) ** 2 for a, b in zip(rgb, c)) ** 0.5 for c in allowed_c) > tol_c:
                                hit("color", _hex(col), pno, text, f"Цвет текста {_hex(col)} вне фирменной палитры",
                                    "Разрешены: " + (st.get("style_colors") or "").strip())
                        if margin:
                            x0, y0, x1, y1 = sp["bbox"]
                            if x0 < margin - 1 or y0 < margin - 1 or x1 > W - margin + 1 or y1 > H - margin + 1:
                                hit("margin", "поля", pno, text,
                                    f"Текст заходит на поля страницы (меньше {st.get('style_margin_mm')} мм от края)",
                                    "Текст за пределами поля может обрезаться при печати")
    if max_fonts and len(fonts) > max_fonts:
        issues[("max_fonts", "")] = {"kind": "max_fonts", "key": "", "pages": set(), "chars": 0,
                                     "title": f"В документе {len(fonts)} гарнитур — больше {max_fonts}",
                                     "detail": ", ".join(f for f, _ in fonts.most_common()), "sample": ""}
    total = sum(fonts.values()) or 1
    out = []
    for it in sorted(issues.values(), key=lambda x: (-x["chars"], x["title"])):
        it["pages"] = sorted(it["pages"])
        it["share"] = round(100 * it["chars"] / total, 2)
        it["key"] = str(it["key"])
        out.append(it)
    return {
        "configured": configured(st), "ok": not out, "issues": out,
        "profile": {
            "fonts": [{"family": f, "names": sorted(font_names[f])[:4], "share": round(100 * n / total, 1)}
                      for f, n in fonts.most_common(10)],
            "sizes": [{"size": s, "share": round(100 * n / total, 1)} for s, n in ksizes.most_common(10)],
            "colors": [{"color": c, "share": round(100 * n / total, 1)} for c, n in colors.most_common(8)],
        },
    }


def summary(res):
    if not res:
        return ""
    if not res.get("configured"):
        return "стандарт не задан — собран профиль оформления"
    return "соответствует стандарту" if res.get("ok") else f"нарушений: {len(res['issues'])}"
