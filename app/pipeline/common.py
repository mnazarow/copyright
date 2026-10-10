# -*- coding: utf-8 -*-
"""Общие функции конвейера: тексты страниц, поиск цитат, нормализация."""
import collections
import contextlib
import re
import threading
import time
import unicodedata

WS = re.compile(r"[\s ]+")
YO = str.maketrans("ёЁ", "еЕ")
AUTHOR = "Требует проверки автором"
TYPES = ["Орфография", "Пунктуация", "Грамматика", "Стилистика", "Типографика",
         "Единообразие", "Логика", AUTHOR]

class Cancelled(Exception):
    """Задание остановлено пользователем (кнопка «Остановить»)."""


# PyMuPDF (MuPDF) не поддерживает работу из нескольких потоков одновременно — документация прямо предупреждает
# о возможном аварийном завершении интерпретатора. Вызовы из потоков веб-сервера (миниатюры, страницы, сравнение
# версий, отчет для руководства, PDF с пометками при пересборке) выполняются по одному под этой блокировкой.
# Обработка заданий идет в отдельных процессах (app.worker), у каждого — своя копия библиотеки.
class FairLock:
    """Повторно входимая блокировка «в порядке очереди». Обычная блокировка не соблюдает очередь: под нагрузкой
    поток, только что отпустивший ее, тут же забирает ее снова, а другие ждут минутами (миниатюры получали отказ)."""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._queue = collections.deque()
        self._owner, self._count = None, 0

    def acquire(self, blocking=True, timeout=-1):
        me = threading.get_ident()
        with self._cond:
            if self._owner == me:
                self._count += 1
                return True
            if not blocking and (self._owner is not None or self._queue):
                return False
            ticket = object()
            self._queue.append(ticket)
            end = None if timeout is None or timeout < 0 else time.monotonic() + timeout
            while self._owner is not None or self._queue[0] is not ticket:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    self._queue.remove(ticket)
                    self._cond.notify_all()
                    return False
                self._cond.wait(left)
            self._queue.popleft()
            self._owner, self._count = me, 1
            return True

    def release(self):
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError("блокировка PDF освобождается не владельцем")
            self._count -= 1
            if not self._count:
                self._owner = None
                self._cond.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


PDF_LOCK = FairLock()
_pdf_used = {"t": 0.0}


def release_pdf_memory():
    """Кеш MuPDF (до 256 МБ шрифтов и картинок) и освобожденная память кучи — системе. Вызывается в веб-сервере
    раз в минуту, если страницы рисовались: иначе процесс «раздувался» от просмотра страниц и миниатюр."""
    if not _pdf_used["t"] or not PDF_LOCK.acquire(timeout=5):
        return False
    try:
        import pymupdf
        pymupdf.TOOLS.store_shrink(100)
        _pdf_used["t"] = 0.0
    except Exception:  # noqa
        pass
    finally:
        PDF_LOCK.release()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa
        pass
    return True


class PairCache:
    """Небольшой кеш результатов сравнения двух заданий (версии, языки): страница, выгрузка и REST API считают
    одно и то же сравнение один раз, пока не изменились файлы заданий."""

    def __init__(self, size=8):
        self.size, self.items, self.lock = size, collections.OrderedDict(), threading.Lock()

    @staticmethod
    def stamp(job_dir, files=("state.json", "dump.json")):
        import os
        out = []
        for fn in files:
            try:
                out.append(os.path.getmtime(os.path.join(job_dir, fn)))
            except OSError:
                out.append(None)
        return tuple(out)

    def get(self, key, compute):
        with self.lock:
            if key in self.items:
                self.items.move_to_end(key)
                return self.items[key]
        val = compute()
        if val is not None:
            with self.lock:
                self.items[key] = val
                while len(self.items) > self.size:
                    self.items.popitem(last=False)
        return val


@contextlib.contextmanager
def pdf_lock(timeout=60):
    """PDF_LOCK с ограничением ожидания: запрос из браузера не висит бесконечно за долгой операцией другого
    запроса — TimeoutError (отвечаем «сервер занят, повторите»)."""
    if not PDF_LOCK.acquire(timeout=timeout):
        raise TimeoutError("обработка PDF занята другими запросами")
    try:
        yield
    finally:
        _pdf_used["t"] = time.time()
        PDF_LOCK.release()


LAT2CYR = dict(zip("ABCEHKMOPTXYaceopxy", "АВСЕНКМОРТХУасеорху"))
CYR2LAT = {v: k for k, v in LAT2CYR.items()}


def norm(s):
    """Нормализация для сравнения цитат так же, как это делает verify.py."""
    return WS.sub("", s or "").translate(YO)


def plural(n, one, few, many):
    """Русское окончание по числу: 1 замечание, 2 замечания, 5 замечаний."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def yo(s):
    return (s or "").translate(YO)


def dur(sec):
    """Длительность для журнала: «0,4 с», «12 с», «3 мин 05 с», «1 ч 02 мин»."""
    sec = max(0.0, float(sec))
    if sec < 10:
        return f"{sec:.1f}".replace(".", ",") + " с"
    if sec < 60:
        return f"{int(sec)} с"
    if sec < 3600:
        return f"{int(sec // 60)} мин {int(sec % 60):02d} с"
    return f"{int(sec // 3600)} ч {int(sec % 3600 // 60):02d} мин"


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
    if lim < 1:                                       # даже 1 dpi больше предела: страница в сотни метров (испорченный
        raise ValueError(f"страница {round(w * 25.4 / 1000)}×{round(h * 25.4 / 1000)} м слишком велика для изображения")
    return int(min(dpi, lim))                         # размер в PDF) — картинка заняла бы гигабайты памяти
