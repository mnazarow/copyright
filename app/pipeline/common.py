# -*- coding: utf-8 -*-
"""Общие функции конвейера: тексты страниц, поиск цитат, нормализация."""
import re
import unicodedata

WS = re.compile(r"[\s ]+")
YO = str.maketrans("ёЁ", "еЕ")
AUTHOR = "Требует проверки автором"
TYPES = ["Орфография", "Пунктуация", "Грамматика", "Стилистика", "Типографика",
         "Единообразие", "Логика", AUTHOR]

LAT2CYR = dict(zip("ABCEHKMOPTXYaceopxy", "АВСЕНКМОРТХУасеорху"))
CYR2LAT = {v: k for k, v in LAT2CYR.items()}


def norm(s):
    """Нормализация для сравнения цитат так же, как это делает verify.py."""
    return WS.sub("", s or "").translate(YO)


def yo(s):
    return (s or "").translate(YO)


def kind(ch):
    n = unicodedata.name(ch, "")
    if "CYRILLIC" in n:
        return "c"
    if "LATIN" in n:
        return "l"
    return "."


class PageIndex:
    """Тексты страниц из dump.json: строки (с колонтитулами) и нормализованный текст."""

    def __init__(self, dump):
        self.dump = dump
        self.pages = {}
        for p in dump.get("lines", []):
            body = [ln["text"] for ln in p["lines"]]
            items = [("body", i, ln) for i, ln in enumerate(p["lines"])]
            items += [("head", i, {"text": t}) for i, t in enumerate(p.get("head", []))]
            items += [("foot", i, {"text": t}) for i, t in enumerate(p.get("foot", []))]
            self.pages[p["pdf_page"]] = {
                "lines": p["lines"], "items": items,
                "norm": norm(" ".join(body + p.get("head", []) + p.get("foot", []))),
            }

    def count(self):
        return len(self.pages)

    def has(self, page, quote):
        p = self.pages.get(page)
        return bool(p) and norm(quote) in p["norm"]

    def find_pages(self, quote):
        q = norm(quote)
        return [n for n, p in self.pages.items() if q and q in p["norm"]]

    def locate(self, page, text):
        """Строка страницы, в которой есть text: (зона, строка) или (None, None)."""
        p = self.pages.get(page)
        if not p:
            return None, None
        for zone, _, ln in p["items"]:
            if text in ln["text"]:
                return zone, ln
        nt = norm(text)
        for zone, _, ln in p["items"]:
            if nt and nt in norm(ln["text"]):
                return zone, ln
        return None, None

    def next_line(self, page, line):
        """Следующая строка того же блока (для висячих предлогов)."""
        lines = self.pages.get(page, {}).get("lines", [])
        for i, ln in enumerate(lines):
            if ln is line and i + 1 < len(lines) and lines[i + 1]["block"] == ln["block"]:
                return lines[i + 1]
        return None


def window(text, match, max_words=15, min_words=3):
    """Цитата 3–15 слов вокруг фрагмента match в строке text (дословно из строки)."""
    text = text.strip()
    i = text.find(match)
    if i < 0:
        return text
    words = list(re.finditer(r"\S+", text))
    if len(words) <= max_words:
        return text
    # индексы слов, пересекающих фрагмент
    a = next((k for k, w in enumerate(words) if w.end() > i), 0)
    b = next((k for k, w in enumerate(words) if w.start() >= i + len(match)), len(words)) - 1
    lo = max(0, a - 4)
    hi = min(len(words) - 1, max(b + 4, lo + min_words - 1))
    while hi - lo + 1 > max_words and hi > b:
        hi -= 1
    while hi - lo + 1 > max_words and lo < a:
        lo += 1
    return text[words[lo].start():words[hi].end()]


def page_list(pages, limit=12):
    ps = sorted(set(pages))
    s = ", ".join(str(p) for p in ps[:limit])
    if len(ps) > limit:
        s += f" и еще {len(ps) - limit}"
    return s


MAX_PIXELS = 36_000_000          # ≈ 6000×6000: изображение страницы не должно съесть память сервиса


def safe_dpi(page, dpi):
    """Разрешение, при котором картинка страницы не больше MAX_PIXELS (огромные страницы — до 5 м)."""
    w, h = page.rect.width / 72, page.rect.height / 72
    if w <= 0 or h <= 0:
        return dpi
    lim = (MAX_PIXELS / (w * h)) ** 0.5
    return max(1, int(min(dpi, lim)))
