# -*- coding: utf-8 -*-
"""Отчет для руководства в PDF: ключевые показатели за период с изменением к прошлому периоду, график плотности
ошибок по неделям с целью, частые ошибки, отделы, документы с превышением цели и рекомендации.

Строится средствами PyMuPDF (без внешних программ); шрифт DejaVu Sans лежит в app/static/fonts.
Рассылка — по расписанию (раз в неделю или раз в месяц) письмом с вложением.
"""
import datetime as dt
import os
import threading
import time

import pymupdf

from . import analytics, core

FONT_DIR = os.path.join(core.APP_DIR, "static", "fonts")
REG, BOLD = os.path.join(FONT_DIR, "DejaVuSans.ttf"), os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
LOGO = os.path.join(core.APP_DIR, "static", "img", "logo.svg")
W, H, M = 595, 842, 40
INK, MUTED, GRID, BRAND, GOAL = (0.11, 0.14, 0.2), (0.42, 0.45, 0.52), (0.88, 0.9, 0.93), (0.06, 0.26, 0.51), (0.79, 0.52, 0.0)
GOOD, BAD = (0.06, 0.54, 0.29), (0.75, 0.15, 0.2)


class Doc:
    def __init__(self):
        self.doc = pymupdf.open()
        self.page = None
        self.y = 0
        self.n = 0

    def new_page(self):
        self.page = self.doc.new_page(width=W, height=H)
        self.page.insert_font(fontname="reg", fontfile=REG)
        self.page.insert_font(fontname="bold", fontfile=BOLD)
        self.n += 1
        self.y = M
        self.page.insert_text((M, H - 22), f"Корректор · отчет для руководства · стр. {self.n}", fontname="reg",
                              fontsize=7.5, color=MUTED)
        return self.page

    def need(self, h):
        if self.page is None or self.y + h > H - M:
            self.new_page()

    def text(self, x, y, s, size=9.5, bold=False, color=INK):
        self.page.insert_text((x, y), str(s), fontname="bold" if bold else "reg", fontsize=size, color=color)

    def width(self, s, size=9.5, bold=False):
        return _font(bold).text_length(str(s), fontsize=size)

    def box(self, x, y, w, h, s, size=9.5, bold=False, color=INK, align=0):
        rc = self.page.insert_textbox(pymupdf.Rect(x, y, x + w, y + h), str(s), fontname="bold" if bold else "reg",
                                      fontsize=size, color=color, align=align)
        return rc

    def para(self, s, size=9.5, bold=False, color=INK, gap=4):
        lines = _wrap(s, W - 2 * M, size, bold)
        lh = size * 1.35
        self.need(lh * len(lines) + gap)
        for ln in lines:
            self.y += lh
            self.text(M, self.y, ln, size, bold, color)
        self.y += gap

    def h2(self, s):
        self.need(80)                                    # заголовок не остается внизу страницы без строк
        self.y += 18
        self.text(M, self.y, s, 13, True, BRAND)
        self.y += 8


_fonts = {}


def _font(bold):
    k = "b" if bold else "r"
    if k not in _fonts:
        _fonts[k] = pymupdf.Font(fontfile=BOLD if bold else REG)
    return _fonts[k]


def _wrap(s, width, size, bold=False):
    out = []
    for para in str(s).split("\n"):
        line = ""
        for w in para.split(" "):
            t = (line + " " + w).strip()
            if _font(bold).text_length(t, fontsize=size) <= width or not line:
                line = t
            else:
                out.append(line)
                line = w
        out.append(line)
    return out


def _fmt(v, d=1, suf=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        s = f"{v:,.{d}f}".replace(",", " ").replace(".", ",")
        s = s.rstrip("0").rstrip(",") if "," in s else s
    else:
        s = f"{v:,}".replace(",", " ")
    return s + suf


def _short(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


# ------------------------------------------------------------------ графики
def line_chart(d, x, y, w, h, labels, values, goal=None, title=""):
    p = d.page
    if title:
        d.text(x, y - 6, title, 9, True)
    vals = [v for v in values if v is not None]
    raw = max(vals + ([goal] if goal else []) + [1]) * 1.1
    step_v = next(x for x in (0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 10 ** 9) if raw / x <= 5)
    top = step_v * 4 if raw <= step_v * 4 else step_v * 5
    ticks = int(round(top / step_v))
    left, bottom = x + 30, y + h - 18
    cw, ch = w - 34, h - 22
    for i in range(ticks + 1):                           # сетка с «круглыми» делениями
        gy = bottom - ch * i / ticks
        p.draw_line((left, gy), (left + cw, gy), color=GRID, width=0.6)
        d.text(x, gy + 3, _fmt(float(step_v * i), 1), 7, color=MUTED)
    n = len(values)
    if not n:
        return
    xs = [left + (cw * (i + 0.5) / n) for i in range(n)]
    step = max(1, n // 8)
    for i, lab in enumerate(labels):
        if i % step == 0:
            d.text(xs[i] - d.width(lab, 6.5) / 2, bottom + 11, lab, 6.5, color=MUTED)
    if goal:
        gy = bottom - ch * goal / top
        p.draw_line((left, gy), (left + cw, gy), color=GOAL, width=1.2, dashes="[4 3] 0")
        d.text(left + 3, gy - 3, f"цель {_fmt(goal)}", 7, color=INK)
    pts = [(xs[i], bottom - ch * v / top) for i, v in enumerate(values) if v is not None]
    seg = []
    for i, v in enumerate(values):                       # разрывы на неделях без документов
        if v is None:
            if len(seg) > 1:
                p.draw_polyline(seg, color=BRAND, width=2)
            seg = []
        else:
            seg.append((xs[i], bottom - ch * v / top))
    if len(seg) > 1:
        p.draw_polyline(seg, color=BRAND, width=2)
    for pt in pts:
        p.draw_circle(pt, 2.6, color=(1, 1, 1), fill=BRAND, width=1.2)
    if pts:
        lx, ly = pts[-1]
        d.text(min(lx + 5, left + cw - 22), ly - 5, _fmt(vals[-1]), 7.5, True)


def hbar_chart(d, x, y, w, rows, title="", bar_h=11, gap=5, label_w=190):
    p = d.page
    if title:
        d.text(x, y - 6, title, 9, True)
    top = max([r[1] for r in rows] + [1])
    cy = y + 2
    for lab, val in rows:
        d.text(x, cy + bar_h - 2, _short(lab, 38), 7.5)
        bw = (w - label_w - 30) * val / top
        r = pymupdf.Rect(x + label_w, cy, x + label_w + max(bw, 1.5), cy + bar_h)
        p.draw_rect(r, color=None, fill=BRAND, radius=0.15 if bw > 6 else None)
        d.text(r.x1 + 4, cy + bar_h - 2, _fmt(val), 7.5)
        cy += bar_h + gap
    return cy


def table(d, cols, rows, widths, size=8):
    lh = size * 1.6
    d.need(lh * 2)
    x = M
    d.y += lh
    for c, wd in zip(cols, widths):
        d.text(x, d.y, c, size, True, MUTED)
        x += wd
    d.page.draw_line((M, d.y + 4), (W - M, d.y + 4), color=GRID, width=0.8)
    for row in rows:
        if d.y + lh > H - M:
            d.new_page()
        d.y += lh
        x = M
        for v, wd in zip(row, widths):
            d.text(x, d.y, _short(v, int(wd / (size * 0.55))), size)
            x += wd
    d.y += 6


# ------------------------------------------------------------------ отчет
def build(q, title_extra=""):
    q = dict(q)
    q.setdefault("period", "30")
    a = analytics.analytics(q)
    fr, to = a["filters"].get("from"), a["filters"].get("to")
    # тренды — за тот же интервал, что и показатели (сравнение — с предыдущим интервалом той же длины)
    tq = dict(q, period="custom", **{"from": fr or "", "to": to or ""}) if (fr or to) else dict(q)
    span = ((dt.date.fromisoformat(to) if to else dt.date.today()) - (dt.date.fromisoformat(fr) if fr else
                                                                      dt.date.today() - dt.timedelta(days=365))).days
    gran = "day" if span <= 31 else "week" if span <= 200 else "month"
    t = analytics.trends(dict(tq, gran=gran))
    k, cmp_, g = a["kpi"], t.get("compare") or {}, a["goals"]
    st = core.load_settings()
    from .pipeline.common import pdf_lock
    with pdf_lock(None):                                  # PyMuPDF — по одному потоку (см. PDF_LOCK)
        d = Doc()
        d.new_page()
        p = d.page
        # шапка
        try:
            with pymupdf.open(LOGO) as logo, pymupdf.open("pdf", logo.convert_to_pdf()) as lp:
                p.show_pdf_page(pymupdf.Rect(M, M - 6, M + 82, M + 51), lp, 0)
        except Exception:  # noqa — без логотипа отчет все равно нужен
            pass
        f = a["filters"]
        d.text(M + 100, M + 14, "Качество документации", 18, True, BRAND)
        def _day(x):
            return f"{dt.date.fromisoformat(x):%d.%m.%Y}" if x else "…"
        per = f"{_day(f.get('from'))} — {_day(f.get('to') or dt.date.today().isoformat())}" if f.get("from") or f.get("to") else "весь период"
        d.text(M + 100, M + 32, f"Отчет для руководства за период {per}{title_extra}", 9.5, color=MUTED)
        d.text(M + 100, M + 45, f"Сформирован {dt.datetime.now():%d.%m.%Y %H:%M} сервисом «Корректор» {core.VERSION}", 8, color=MUTED)
        d.y = M + 70

        # ключевые показатели
        tiles = [("Документов проверено", k["docs"], "docs", 0, ""), ("Страниц", k["pages"], "pages", 0, ""),
                 ("Замечаний на 10 страниц", k["density"], "density", 2, ""),
                 ("Точность замечаний", k["precision"], "precision", 1, " %"),
                 ("Отчетов приняли 25/25", k["verify_rate"], "verify_rate", 1, " %"),
                 ("Документов выше цели", k.get("over_goal", 0), None, 0, "")]
        tw, th = (W - 2 * M - 20) / 3, 58
        for i, (lab, val, key, dec, suf) in enumerate(tiles):
            x0 = M + (i % 3) * (tw + 10)
            y0 = d.y + (i // 3) * (th + 10)
            p.draw_rect(pymupdf.Rect(x0, y0, x0 + tw, y0 + th), color=GRID, fill=(0.97, 0.98, 0.99), width=0.8, radius=0.08)
            d.text(x0 + 10, y0 + 15, lab, 8, color=MUTED)
            d.text(x0 + 10, y0 + 37, _fmt(float(val) if isinstance(val, float) else val, dec, suf), 17, True)
            c = cmp_.get(key) if key else None
            if c and c.get("pct") is not None:
                arrow = "▲" if (c["delta"] or 0) > 0 else "▼" if (c["delta"] or 0) < 0 else "="
                word = {"better": "лучше", "worse": "хуже"}.get(c["verdict"], "")
                col = GOOD if c["verdict"] == "better" else BAD if c["verdict"] == "worse" else MUTED
                d.text(x0 + 10, y0 + 51, f"{arrow} {_fmt(abs(c['pct']), 1)} % к прошлому периоду {('— ' + word) if word else ''}", 7,
                       color=col)
            goal = g.get(key) if key else None
            if goal and key in ("density", "precision", "verify_rate"):
                d.text(x0 + tw - 10 - d.width(f"цель {_fmt(goal)}", 7), y0 + 15, f"цель {_fmt(goal)}", 7, color=MUTED)
        d.y += 2 * th + 10 + 34

        # график плотности
        dens = t["series"]["density"]
        line_chart(d, M, d.y, W - 2 * M, 170, t["labels"], dens, g.get("density"),
                   f"Замечаний на 10 страниц по {({'day': 'дням', 'week': 'неделям'}).get(gran, 'месяцам')}")
        d.y += 170 + 30

        # частые ошибки
        rows = [(r["label"], r["n"]) for r in a["top_rules"][:10]]
        if rows:
            end = hbar_chart(d, M, d.y, W - 2 * M, rows, "Самые частые ошибки (замечаний)")
            d.y = end + 6
        d.para("Плотность ошибок — число замечаний на 10 страниц: она позволяет сравнивать документы разного объема. "
               "Точность — доля замечаний, которые корректор принял (✓), среди отмеченных. «25/25» — отчет прошел все "
               "машинные проверки оформления.", 7.5, color=MUTED)

        # отделы
        deps = [r for r in a["by_department"] if r["docs"]]
        if len(deps) > 1 or (deps and deps[0]["key"]):
            d.h2("По отделам")
            table(d, ["Отдел", "Документов", "Страниц", "На 10 стр.", "Точность, %", "25/25, %"],
                  [[r["label"], r["docs"], r["pages"], _fmt(r["density"], 2), _fmt(r["precision"]), _fmt(r["verify_rate"])]
                   for r in deps[:15]], [175, 70, 60, 70, 70, 70])
        auth = [r for r in a["by_author"] if r["docs"] and r["key"]]
        if auth:
            d.h2("Авторы с наибольшей плотностью ошибок")
            auth = sorted(auth, key=lambda r: -(r["density"] or 0))[:10]
            table(d, ["Автор", "Документов", "Страниц", "На 10 стр.", "Точность, %"],
                  [[r["label"], r["docs"], r["pages"], _fmt(r["density"], 2), _fmt(r["precision"])] for r in auth],
                  [215, 70, 70, 80, 80])
        over = [r for r in a["density"] if r.get("goal") and (r["density"] or 0) > r["goal"]]
        if over:
            d.h2(f"Документы выше цели ({len(over)})")
            table(d, ["Документ", "Страниц", "На 10 стр.", "Цель"],
                  [[r["name"], r.get("pages", "—"), _fmt(r["density"], 2), _fmt(r["goal"])]
                   for r in sorted(over, key=lambda r: -(r["density"] or 0))[:20]], [330, 60, 70, 55])
        pre = [r for r in a["preflight"] if r["docs"]]
        if pre:
            d.h2("Предпечатная проверка и стандарты")
            table(d, ["Проблема", "Документов", "Случаев"], [[r["label"], r["docs"], r["items"]] for r in pre], [330, 90, 90])
        recs = a.get("recommendations") or []
        if recs:
            d.h2("Рекомендации")
            for r in recs[:8]:
                d.para("• " + r["title"], 9, True, gap=1)
                d.para(r["text"], 8.5, color=MUTED, gap=5)
        link = (st.get("service_url") or "").rstrip("/")
        if link:
            d.para(f"Интерактивная аналитика: {link}/analytics · тренды: {link}/trends", 8, color=MUTED)
        try:
            d.doc.subset_fonts()                        # в файл попадают только использованные знаки шрифта
        except Exception:  # noqa
            pass
        try:
            return d.doc.tobytes(garbage=4, deflate=True)
        finally:
            d.doc.close()


def filename(q=None):
    return f"Корректор_отчет_для_руководства_{dt.date.today():%Y-%m-%d}.pdf"


# ------------------------------------------------------------------ рассылка
def due(st, now, last):
    """Пора ли отправлять: раз в неделю (понедельник) или раз в месяц (1-е число) после заданного часа."""
    if not st.get("mgmt_report_enabled") or now.hour < int(st.get("mgmt_report_hour", 8) or 0):
        return None
    if st.get("mgmt_report_period") == "month":
        key = now.strftime("%Y-%m")
        return key if last != key else None
    key = now.strftime("%G-%V")
    return key if last != key else None


def send(st, force=False):
    from . import notify
    month = st.get("mgmt_report_period") == "month"
    today = dt.date.today()
    if month:                                       # прошлый календарный месяц
        end = today.replace(day=1) - dt.timedelta(days=1)
        start = end.replace(day=1)
    else:                                           # прошлые 7 дней
        end, start = today - dt.timedelta(days=1), today - dt.timedelta(days=7)
    q = {"period": "custom", "from": start.isoformat(), "to": end.isoformat()}
    pdf = build(q)
    to = [x.strip() for x in (st.get("mgmt_report_to") or "").replace(";", ",").replace("\n", ",").split(",")
          if x.strip()] or notify.recipients(st)
    subj = f"Корректор: отчет о качестве документации за {'месяц' if month else 'неделю'} ({start:%d.%m}–{end:%d.%m.%Y})"
    text = (f"Во вложении — отчет о качестве документации за период {start:%d.%m.%Y}–{end:%d.%m.%Y}: ключевые "
            "показатели, динамика ошибок, частые ошибки, отделы и рекомендации.")
    return notify.send_email(st, subj, text, notify._html(text), to,
                             attachments=[(filename(), pdf, "application", "pdf")])


def loop():
    from .notify import _read_mark, _write_mark
    mark = os.path.join(core.DATA, ".mgmt_report_sent")
    sent, failures = None, {}                        # в памяти: отправленный период и число неудачных попыток
    while True:
        try:
            st = core.load_settings()
            key = due(st, dt.datetime.now(), _read_mark(mark))
            if key and key != sent and failures.get(key, 0) < 6:
                r = send(st)
                if r == "ok":
                    sent = key
                    _write_mark(mark, key)
                else:                                   # повторная попытка — через час, не больше шести за период
                    failures[key] = failures.get(key, 0) + 1
                    core.log.warning("отчет для руководства: %s", r)
                    time.sleep(2700)
        except Exception:  # noqa
            core.log.exception("отчет для руководства")
        time.sleep(900)


def start():
    threading.Thread(target=loop, daemon=True).start()
