# -*- coding: utf-8 -*-
"""Конвейер одного задания: извлечение → правила → словарь → ИИ → findings.json → DOCX → verify."""
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

import pymupdf

from . import autofix, exports, llm, office, preflight, spell
from .common import AUTHOR, Cancelled, PageIndex, dur, norm, page_list, plural, safe_dpi, yo

_log = logging.getLogger("korrektor")

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
PY = sys.executable

TOKEN_LISTS = ("mixed_alphabet_tokens", "number_unit_no_gap", "cyrillic_times_sign", "latin_single_letters")
CAT_NAMES = {"table_issues": "таблицы", "toc_issues": "содержание", "page_refs": "ссылки «стр. N»"}


def sh(args, cwd, timeout=1800, env=None, should_stop=None, on_progress=None, beat=None):
    """Скрипт конвейера отдельным процессом. Остановка задания и тайм-аут завершают процесс; строки «@progress N/M»
    из stderr передаются в on_progress(N, M); beat(секунды) вызывается раз в 15 секунд. → (код, stdout, stderr)."""
    environ = dict(os.environ, **env) if env else dict(os.environ)
    if on_progress:
        environ["KORREKTOR_PROGRESS"] = "1"
    proc = subprocess.Popen([PY] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=environ)
    out, err = [], []

    def pump(stream, parts, prog):
        try:
            for line in stream:
                if prog and line.startswith("@progress "):
                    try:
                        a, b = line.split()[1].split("/")
                        prog(int(a), int(b))
                    except Exception:  # noqa — ход выполнения необязателен
                        pass
                    continue
                parts.append(line)
        except Exception:  # noqa
            pass

    threads = [threading.Thread(target=pump, args=(proc.stdout, out, None), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err, on_progress), daemon=True)]
    for t in threads:
        t.start()
    t0 = time.time()
    next_beat = t0 + 15
    try:
        while True:
            try:
                proc.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            if should_stop and should_stop():
                raise Cancelled()
            if time.time() - t0 > timeout:
                raise subprocess.TimeoutExpired([os.path.basename(args[0])], timeout)
            if beat and time.time() >= next_beat:
                next_beat = time.time() + 15
                try:
                    beat(time.time() - t0)
                except Exception:  # noqa
                    pass
    except BaseException:
        proc.kill()
        try:
            proc.wait(5)
        except Exception:  # noqa
            pass
        raise
    finally:
        for t in threads:
            t.join(5)
    return proc.returncode, "".join(out), "".join(err)


def render_pages(pdf, outdir, dpi, should_stop=None, on_page=None):
    os.makedirs(outdir, exist_ok=True)
    with pymupdf.open(pdf) as doc:
        n = doc.page_count
        for pno, page in enumerate(doc, 1):
            if should_stop and should_stop():
                raise Cancelled()
            fn = os.path.join(outdir, f"p{pno:03d}.png")
            if not os.path.exists(fn):
                page.get_pixmap(dpi=safe_dpi(page, dpi)).save(fn + ".tmp.png")
                os.replace(fn + ".tmp.png", fn)
            if on_page:
                on_page(pno, n)
        return n


def candidates(dump):
    c = set(dump.get("mixed_alphabet_tokens", {}).keys())
    for k in ("number_unit_no_gap", "cyrillic_times_sign", "latin_single_letters"):
        c |= {r["text"] for r in dump.get(k, [])}
    c |= {" ".join(r["tail"].split()[-2:]) for r in dump.get("dangling_prepositions", [])}
    return c


def _yo_all(res):
    """«ё» → «е» в текстах дополнительных проверок (как во всем отчете)."""
    if not res:
        return res
    for key in ("issues", "duplicates"):
        for it in res.get(key) or []:
            for k in ("text", "detail", "sample", "title"):
                if isinstance(it.get(k), str):
                    it[k] = clean(yo(it[k]))
            for pl in it.get("places") or []:
                pl["label"] = clean(yo(pl.get("label") or ""))
    return res


class Coverage:
    """«Кандидат уже в замечании или в rejected?» без пересборки всего текста на каждого кандидата
    (на 1000-страничном каталоге прежняя проверка занимала десятки минут)."""

    def __init__(self, findings, rejected):
        self.base = norm(" ".join(f["src"] for f in findings) + " " + " ".join(str(r.get("text", "")) for r in rejected))
        self.big, self.tail = "", []

    def has(self, token):
        t = norm(token)
        return t in self.base or t in self.big or any(t in x for x in self.tail)

    def add(self, text):
        self.tail.append(norm(text))
        if len(self.tail) >= 400:
            self.big += "".join(self.tail)
            self.tail = []


def doc_title(dump):
    p1 = dump["lines"][0] if dump.get("lines") else {}
    lines = [ln["text"].strip() for ln in p1.get("lines", [])[:6] if len(ln["text"].strip()) > 2]
    title = ""
    for t in lines:
        if re.match(r"(Для|Версия|www|http)", t) or len(title) + len(t) > 70:
            break
        title = (title + " " + t).strip()
    title = re.sub(r"\s+", " ", title)
    ver = None
    for p in dump.get("lines", [])[:2]:
        for t in [ln["text"] for ln in p["lines"]] + p.get("head", []) + p.get("foot", []):
            m = re.search(r"[Вв]ерсия[:\s]+([^\s|]+)", t)
            if m:
                ver = m.group(1)
    return title, ver


def make_meta(name, dump, findings, pf, st, ocr_done, scans):
    title, ver = doc_title(dump)
    n = dump.get("pages_count", 0)
    a = sum(1 for f in findings if f["type"] == AUTHOR)
    vol = f"Объем: {n} {plural(n, 'страница', 'страницы', 'страниц')}"
    notes = []
    if dump.get("empty_pages"):
        notes.append("пустые страницы: " + page_list(dump["empty_pages"]))
    if pf and pf.get("placeholders"):
        notes.append("серые заглушки на месте иллюстраций: стр. " + page_list(pf["placeholders"]))
    if ocr_done:
        notes.append("страницы-сканы распознаны OCR: " + page_list(ocr_done))
    elif scans:
        notes.append("страницы без текстового слоя (изображения): " + page_list(scans))
    if pf and pf.get("curves"):
        notes.append("текст в кривых, не проверялся: " + page_list(pf["curves"]))
    if notes:
        vol += " (" + "; ".join(notes) + ")"
    hl = [f"Документ: {name}" + (f" — «{title}»" if title else "") + (f", версия {ver}" if ver else ""),
          vol, f"Дата вычитки: {dt.date.today().strftime('%d.%m.%Y')}",
          "Нумерация: физические страницы PDF, отсчет от первой страницы файла",
          "Проверка верстки: выполнена по координатам строк" +
          (f"; на страницах {page_list(ocr_done)} (OCR) — по распознанному тексту" if ocr_done else "")]
    if pf and pf.get("items"):
        hl.append("Технический контроль PDF: " + "; ".join(pf["items"])[:600])
    hl += [x for x in (st.get("report_extra_header") or "").splitlines() if x.strip()]
    totals = f"Всего замечаний: {len(findings)} (из них ошибок — {len(findings) - a}, требует проверки автором — {a})"
    # шапка отчета: без «ё» (требование мастер-промпта, проверка 25) и без управляющих знаков (ломают DOCX)
    hl = [clean(yo(x)) for x in hl]
    return {"header_lines": hl, "totals": totals, "summary": clean(yo(make_summary(findings, n, (pf or {}).get("curves"))))}


CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")


def clean(s):
    return CTRL.sub("", s) if isinstance(s, str) else s


def make_summary(findings, pages, curves=None):
    if not findings and curves:
        return (f"Проверено {pages} {plural(pages, 'страница', 'страницы', 'страниц')}. Текстовый слой отсутствует "
                f"(текст переведен в кривые) на страницах {page_list(curves)} — их текст не вычитывался.")
    if not findings:
        return f"Проверено {pages} {plural(pages, 'страница', 'страницы', 'страниц')}. Замечаний не найдено: текст и данные документа согласованы."
    a = sum(1 for f in findings if f["type"] == AUTHOR)
    sysm = sorted([f for f in findings if f.get("systemic")], key=lambda f: -f["systemic"])[:3]
    n = len(findings)
    parts = [f"Проверено {pages} {plural(pages, 'страница', 'страницы', 'страниц')}, найдено {n} "
             f"{plural(n, 'замечание', 'замечания', 'замечаний')}, из них {a} {plural(a, 'требует', 'требуют', 'требуют')} "
             "решения автора."]
    if sysm:
        parts.append("Системные дефекты: " + "; ".join(
            f"{LABELS.get(f.get('rule'), f['type'].lower())} ({f['systemic']} {plural(f['systemic'], 'место', 'места', 'мест')})"
            for f in sysm) + ".")
    key = [f for f in findings if f.get("rule") in ("table", "toc", "ref", "requisites", "mixed", "latin")
           or (f.get("rule") == "ai" and f["type"] in ("Логика", AUTHOR))][:4]
    if key:
        parts.append("Самые важные находки: " + "; ".join(
            f"стр. {f['page']} — {LABELS.get(f.get('rule'), 'расхождение данных')} («{f['src'][:40]}»)" for f in key) + ".")
    types = {}
    for f in findings:
        types[f["type"]] = types.get(f["type"], 0) + 1
    main = max(types, key=types.get)
    parts.append(f"Больше всего замечаний относится к категории «{main}».")
    s = " ".join(parts[:4])
    return re.sub(r"(?i)рекоменд\w*|следует проверить|стоит проверить", "", s)


LABELS = {"range": "дефис вместо тире в диапазонах", "times": "знак умножения", "thousands": "разряды в числах",
          "unit": "пробел между числом и единицей", "inch": "знак дюйма", "equals": "отбивка знака «=»",
          "dspace": "двойные пробелы", "dot": "пробел после точки", "digitword": "цифра слитно со словом",
          "degree": "знак градуса", "quotes": "прямые кавычки", "mwc": "запись «м вод. ст.»", "decimal": "десятичный разделитель",
          "std": "запись обозначений стандартов", "mixed": "смешение кириллицы и латиницы", "latin": "латинская буква в русском слове",
          "glossary": "термины вне глоссария", "brand": "написание брендов", "phone": "формат телефонов",
          "table": "расхождение в таблице", "toc": "номер страницы в содержании", "ref": "ссылка на несуществующую страницу",
          "requisites": "реквизиты не совпадают с эталоном", "spell": "слово вне словаря", "hanging": "висячие предлоги",
          "pct": "запись знака процента", "forbidden": "запрещенные слова", "lt": "грамматика",
          "figures": "нумерация рисунков и таблиц"}


def parse_verify(out):
    checks = []
    for line in out.splitlines():
        m = re.match(r"^(PASS|FAIL)\s+(.*?)(?:\s{3}\[(.*)\])?$", line)
        if m:
            checks.append({"ok": m.group(1) == "PASS", "name": m.group(2).strip(), "detail": m.group(3) or ""})
    m = re.search(r"ПРОВЕРОК: (\d+)/(\d+)", out)
    return {"checks": checks, "passed": int(m.group(1)) if m else 0, "total": int(m.group(2)) if m else 0,
            "ok": "ВСЕ ТРЕБОВАНИЯ ВЫПОЛНЕНЫ" in out}


def report_name(st, name, job_id):
    tpl = st.get("report_filename") or "{name}_корректура"
    try:
        base = tpl.format(name=name, date=dt.date.today().isoformat(), job=job_id)
    except (KeyError, IndexError, ValueError, AttributeError):
        base = f"{name}_корректура"                       # ошибка в шаблоне имени — имя по умолчанию
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip() or "отчет_корректура"
    while len(base.encode("utf-8")) > 180:              # + «_пометки.pdf» и т. п. — в пределах 255 байт имени файла
        base = base[:-1]
    return base.rstrip(" .") or "отчет_корректура"


def report_findings(state, st):
    """Замечания, которые попадают в отчет, в порядке его таблицы (номер = позиция + 1)."""
    fl = [f for f in state["findings"] if f.get("status") != "rejected"]
    if st.get("report_reviewed_only"):
        fl = [f for f in fl if f.get("status") == "accepted"]
    if not st.get("report_include_author", True):
        fl = [f for f in fl if f["type"] != AUTHOR]
    return sorted(fl, key=lambda f: (f["page"], str(f["clause"])))


def build_report(job_dir, state, st, log):
    """Пишет findings.json из состояния задания, собирает DOCX, проверяет, делает доп. форматы."""
    fl = report_findings(state, st)
    meta = dict(state["meta"])
    a = sum(1 for f in fl if f["type"] == AUTHOR)
    meta["totals"] = f"Всего замечаний: {len(fl)} (из них ошибок — {len(fl) - a}, требует проверки автором — {a})"
    if st.get("report_summary_auto", True) and not state.get("summary_edited"):
        meta["summary"] = yo(make_summary(fl, state.get("pages", 0), (state.get("preflight") or {}).get("curves")))
        state["meta"]["summary"] = meta["summary"]     # страница задания показывает тот же итог, что в отчете
    meta = {k: ([clean(x) for x in v] if isinstance(v, list) else clean(v)) for k, v in meta.items()}
    rejected = list(state.get("rejected", []))
    # убранные из отчета замечания не должны оставлять кандидатов неразобранными
    for f in state["findings"]:
        if f not in fl:
            rejected.append({"text": f["src"], "reason": "замечание отклонено корректором или исключено настройками"})
    data = {"document": meta,
            "findings": [{k: clean(f[k]) for k in ("page", "clause", "type", "src", "bad", "fix", "good", "note")} for f in fl],
            "rejected": rejected, "reviewed": state.get("reviewed", [])}
    json.dump(data, open(os.path.join(job_dir, "findings.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    base = state["report_base"]
    docx = base + ".docx"
    code, out, err = sh([os.path.join(SCRIPTS, "build.py"), "findings.json", docx], job_dir)
    if code:
        log("Сборка DOCX упала: " + err[-800:])
        return {"ok": False, "passed": 0, "total": 25, "checks": [], "count": len(fl),
                "error": "отчет DOCX не собран: " + ((err.strip().splitlines() or ["ошибка сборки"])[-1])[:300]}
    code, out, err = sh([os.path.join(SCRIPTS, "verify.py"), docx, "findings.json", "dump.json"], job_dir)
    ver = parse_verify(out)
    open(os.path.join(job_dir, "verify.log"), "w", encoding="utf-8").write(out + err)
    log(f"Машинная проверка оформления: {ver['passed']}/{ver['total']} " + ("PASS" if ver["ok"] else "— есть нарушения"))
    if not ver["total"]:
        log("⚠ машинная проверка не выдала результата: " + ((err or out).strip().splitlines() or ["нет вывода"])[-1][:300])
    for c in [c for c in ver["checks"] if not c["ok"]][:10]:
        log(f"  ✗ не пройдено: {c['name']}" + (f" — {c['detail']}" if c["detail"] else ""))
    fm = st.get("report_formats") or ["docx"]
    files = {"docx": docx}
    if "xlsx" in fm:
        exports.to_xlsx(fl, meta, os.path.join(job_dir, base + ".xlsx")); files["xlsx"] = base + ".xlsx"
    if "csv" in fm:
        exports.to_csv(fl, os.path.join(job_dir, base + ".csv")); files["csv"] = base + ".csv"
    if "json" in fm:
        files["json"] = "findings.json"
    if "pdf_annot" in fm:
        try:
            n = preflight.annotate(os.path.join(job_dir, "source.pdf"), fl, os.path.join(job_dir, base + "_пометки.pdf"))
            files["pdf_annot"] = base + "_пометки.pdf"
            log(f"PDF с пометками: подсвечено {n} из {len(fl)} замечаний")
        except Exception as e:  # noqa
            log(f"PDF с пометками не создан: {e}")
    src = office.find_source(job_dir)
    if src and "docx_review" in fm:
        try:
            from . import docx_review
            work = src
            if not src.lower().endswith(".docx"):
                work = os.path.join(job_dir, "source_converted.docx")
                if not os.path.exists(work):
                    office.to_docx(src, work)
            name = base + "_рецензия.docx"
            nc, nch, miss = docx_review.review(work, fl, os.path.join(job_dir, name),
                                               author=st.get("review_author") or "Корректор")
            files["docx_review"] = name
            log(f"Правки в исходном Word: примечаний {nc}, исправлений в режиме рецензирования {nch}, "
                f"не найдено в тексте {miss}")
        except Exception as e:  # noqa
            log(f"Правки в исходном Word не созданы: {e}")
    ver["files"] = files
    ver["count"] = len(fl)
    return ver


def _num(x):
    return f"{int(x):,}".replace(",", " ")


def _mb(size):
    return f"{(size or 0) / 2 ** 20:.1f}".replace(".", ",")


class _Stage:
    """Необязательная проверка: ошибка в ней не роняет задание — она пропускается, а причина пишется в журнал,
    на страницу задания и в шапку отчета (warnings)."""

    def __init__(self, log, warnings, should_stop):
        self.log, self.warnings, self.should_stop = log, warnings, should_stop

    def __call__(self, title, fn, *a, empty=None, quiet=False, **kw):
        if self.should_stop():
            raise Cancelled()
        t = time.time()
        try:
            res = fn(*a, **kw)
        except Cancelled:
            raise
        except MemoryError:
            raise
        except Exception as e:  # noqa
            _log.exception("проверка «%s»", title)
            self.log(f"  ⚠ «{title}» пропущено из-за ошибки: {e.__class__.__name__}: {str(e)[:300]}")
            self.warnings.append(f"{title} (ошибка: {str(e)[:120] or e.__class__.__name__})")
            return empty
        n = None
        if quiet:
            pass
        elif isinstance(res, tuple) and res and isinstance(res[0], list):
            n = len(res[0])
        elif isinstance(res, list):
            n = len(res)
        if n is not None:
            self.log(f"  · {title}: {n} {plural(n, 'находка', 'находки', 'находок')} за {dur(time.time() - t)}")
        return res


def process(job_dir, name, st, log, progress, job_id=0, status=None, should_stop=None):
    status = status or (lambda m: None)
    should_stop = should_stop or (lambda: False)
    warnings = []
    stage = _Stage(log, warnings, should_stop)
    pdf = os.path.join(job_dir, "source.pdf")
    if not os.path.exists(pdf):
        src = office.find_source(job_dir)
        if not src:
            raise RuntimeError("Исходный файл не найден")
        progress(0.02, "Преобразование документа в PDF (LibreOffice)")
        t = time.time()
        office.to_pdf(src, pdf)
        log(f"Документ {os.path.basename(src)} преобразован в PDF за {dur(time.time() - t)}")
    try:
        with pymupdf.open(pdf) as _d:
            if _d.needs_pass and not _d.authenticate(""):
                raise RuntimeError("PDF защищен паролем на открытие — снимите защиту (сохраните без пароля) и загрузите снова")
            if _d.page_count == 0:
                raise RuntimeError("В PDF нет страниц")
            meta = _d.metadata or {}
            info = [f"PDF: {_d.page_count} {plural(_d.page_count, 'страница', 'страницы', 'страниц')}, "
                    f"{_mb(os.path.getsize(pdf))} МБ"]
            if meta.get("format"):
                info.append(meta["format"])
            if meta.get("creator") or meta.get("producer"):
                info.append("создан: " + " / ".join(x for x in (meta.get("creator"), meta.get("producer")) if x)[:120])
            if _d.is_encrypted:
                info.append("с ограничениями прав (печать, копирование)")
            log(clean(", ".join(info)))
    except RuntimeError:
        raise
    except Exception as e:  # noqa
        raise RuntimeError(f"Файл не открывается как PDF: {e}")

    step = "Извлечение текста и координат строк"
    progress(0.03, step)
    seen = {"t": 0.0}

    def extract_prog(i, n):
        now = time.time()
        if now - seen["t"] >= 1 or i == n:
            seen["t"] = now
            try:
                progress(0.03 + 0.08 * i / max(1, n), step)
            except Cancelled:
                return
            status(f"Извлечение текста: страница {i} из {n}")

    code, out, err = sh([os.path.join(SCRIPTS, "extract.py"), pdf], job_dir,
                        env={"KORREKTOR_HANGING_WORDS": st.get("hanging_words") or ""}, should_stop=should_stop,
                        on_progress=extract_prog)
    open(os.path.join(job_dir, "extract.log"), "w", encoding="utf-8").write(out + err)
    if code:
        tail = (err.strip().splitlines() or ["нет текстового слоя"])[-1]
        log("Разбор PDF завершился с ошибкой. Последние строки вывода:\n" + "\n".join(err.strip().splitlines()[-12:]))
        raise RuntimeError("Не удалось разобрать PDF: " + tail[:300])
    dump = json.load(open(os.path.join(job_dir, "dump.json"), encoding="utf-8"))
    npages = dump.get("pages_count", 0)
    if npages > int(st.get("max_pages") or 10 ** 6):
        raise RuntimeError(f"Документ длиннее {st.get('max_pages')} страниц (настройка «Максимум страниц»)")
    nlines = sum(len(p.get("lines") or []) for p in dump.get("lines", []))
    nchars = sum(len(ln.get("text") or "") for p in dump.get("lines", []) for ln in p.get("lines") or [])
    log(f"Текст извлечен: {npages} стр., {_num(nlines)} {plural(nlines, 'строка', 'строки', 'строк')}, {_num(nchars)} "
        f"{plural(nchars, 'знак', 'знака', 'знаков')}"
        + f"; кандидаты: смешение алфавитов {len(dump.get('mixed_alphabet_tokens') or {})}, число слитно с единицей "
          f"{len(dump.get('number_unit_no_gap') or [])}, висячие предлоги {len(dump.get('dangling_prepositions') or [])}, "
          f"таблицы {len(dump.get('table_issues') or [])}, содержание {len(dump.get('toc_issues') or [])}, ссылки «стр. N» "
          f"{len(dump.get('page_refs') or [])}"
        + (f"; пустые страницы: {page_list(dump['empty_pages'])}" if dump.get("empty_pages") else ""))
    if nchars == 0:
        log("⚠ в документе нет текстового слоя: текст переведен в кривые или страницы — сканы. Включите OCR "
            "(«Технический контроль PDF» → «Распознавать страницы-сканы»), иначе проверять нечего")

    step = "Изображения страниц"
    progress(0.12, step)
    dpi = int(st.get("page_images_dpi") or 72)
    t = time.time()

    def page_done(i, n):
        if i == n or i % 10 == 0:
            status(f"Изображения страниц: {i} из {n}")
    try:
        render_pages(pdf, os.path.join(job_dir, "pages"), dpi, should_stop, page_done)
        log(f"Изображения страниц ({dpi} dpi) готовы за {dur(time.time() - t)}")
    except Cancelled:
        raise
    except MemoryError:
        raise
    except Exception as e:  # noqa — без картинок страниц отчет все равно собирается
        log(f"⚠ изображения страниц не созданы ({e.__class__.__name__}: {e}); просмотр страниц будет недоступен")
        warnings.append("изображения страниц для просмотра")

    pf = None
    scans, ocr_done = [], []
    if st.get("preflight_enabled", True):
        progress(0.18, "Технический контроль PDF")
        pf = stage("Технический контроль PDF", preflight.preflight, pdf, st, dump)
        if pf:
            scans = pf["scans"]
            log("Технический контроль: " + ("; ".join(pf["items"])[:800] if pf["items"] else "замечаний нет"))
            if scans and st.get("ocr_enabled"):
                progress(0.2, "Распознавание страниц-сканов (OCR)")
                log(f"OCR: распознаются страницы {page_list(scans)}")
                ocr_done = stage("Распознавание страниц-сканов (OCR)", preflight.ocr_pages, pdf, scans, dump, log,
                                 empty=[], quiet=True) or []
                if ocr_done:
                    log(f"OCR: распознаны страницы {page_list(ocr_done)}")
                    json.dump(dump, open(os.path.join(job_dir, "dump.json"), "w", encoding="utf-8"), ensure_ascii=False)
            elif scans:
                log(f"⚠ страницы без текстового слоя: {page_list(scans)} — их текст не проверяется (OCR выключен)")
    style_res = None
    if st.get("style_enabled", True):
        progress(0.22, "Стандарт оформления: шрифты, кегли, цвета, поля")
        from . import style
        style_res = stage("Стандарт оформления", style.check, pdf, st)
        if style_res:
            log(f"Стандарт оформления: {style.summary(style_res)}")
    products_res = None
    ref = None
    if st.get("products_enabled", True):
        from . import products
        ref = stage("Справочник продукции (загрузка)", products.load, products.PATH())
        if ref:
            progress(0.24, "Сверка со справочником продукции")
            products_res = stage("Сверка со справочником продукции", products.check, dump, ref, st, pdf)
            if products_res:
                log(f"Справочник продукции ({products_res['loaded']} позиций): найдено артикулов "
                    f"{products_res['found']}, замечаний {len(products_res['issues'])}")
    images_res = None
    if st.get("images_duplicates", True):
        progress(0.245, "Изображения: одинаковые картинки у разных изделий")
        from . import images
        images_res = stage("Одинаковые изображения", images.check, pdf, st,
                           [it["article"] for it in (ref or {}).get("items", {}).values()] or None)
        if images_res:
            log(f"Изображения: проверено {images_res.get('images', 0)}, одинаковых у разных изделий — "
                f"{len(images_res.get('duplicates') or [])}")
    idx = PageIndex(dump)
    passes = set(st.get("passes") or [])
    hits, rejected = [], []
    progress(0.25, "Правила: типографика, алфавиты, висячие предлоги")
    log(f"Правила (проходы: {', '.join(sorted(passes)) or 'нет'}):")
    if "text" in passes:
        hits += stage("типографика", autofix.typography, idx, st, empty=[]) or []
        hits += stage("знак процента", autofix.percent, idx, st, empty=[]) or []
    if "layout" in passes and st.get("hanging_check", True):
        h, r = stage("висячие предлоги", autofix.hanging, idx, dump, st, empty=([], [])) or ([], [])
        hits += h
        rejected += r
    if "codes" in passes and st.get("check_codes", True):
        h, r = stage("смешение алфавитов в кодах и словах", autofix.alphabets, idx, dump, st, empty=([], [])) or ([], [])
        hits += h
        rejected += r
    if "terms" in passes:
        hits += stage("глоссарий, бренды, сокращения", autofix.house, idx, st, empty=[]) or []
        hits += stage("формат телефонов", autofix.phones, idx, st, empty=[]) or []
    if "data" in passes:
        hits += stage("таблицы, содержание, ссылки, стандарты", autofix.data_checks, idx, dump, st, empty=[]) or []
        hits += stage("нумерация рисунков и таблиц", autofix.figures, idx, st, empty=[]) or []
        hits += stage("реквизиты", autofix.requisites, idx, st, empty=[]) or []
    if "text" in passes:
        progress(0.35, "Словарная проверка")
        hits += stage("словарь (Hunspell)", spell.hunspell, idx, st, log, empty=[]) or []
        hits += stage("грамматика (LanguageTool)", spell.languagetool, idx, st, log, should_stop, empty=[]) or []
    log(f"Правила и словарь: {len(hits)} находок")
    findings, rej2 = autofix.build_findings(hits, int(st.get("systemic_threshold") or 5))
    rejected += rej2
    log(f"После объединения повторов и системных дефектов: {len(findings)} "
        f"{plural(len(findings), 'замечание', 'замечания', 'замечаний')}")

    mode = st.get("mode", "auto")
    use_ai = mode == "rules_ai" or (mode == "auto" and st.get("ai_provider") not in (None, "", "none"))
    ai_report = None
    if use_ai or os.environ.get("KORREKTOR_AI_MOCK"):
        progress(0.45, "ИИ: вычитка смысла по мастер-промпту")
        extra = "\n".join(x for x in [st.get("ai_extra_instructions") or "",
                                      ("Сокращения компании:\n" + st["abbreviations"]) if st.get("abbreviations") else "",
                                      ("Глоссарий компании (правильно: неправильно):\n" + st["glossary"]) if st.get("glossary") else ""] if x)
        ai_report = {}
        try:
            ai = llm.run(dump, idx, st, extra, log,
                         lambda x: progress(0.45 + 0.35 * x, "ИИ: вычитка смысла по мастер-промпту"),
                         status=status, should_stop=should_stop, report=ai_report)
        except Cancelled:
            raise
        except Exception as e:  # noqa — задание завершается с результатами правил
            _log.exception("проход ИИ")
            log(f"ИИ ✗ проход прерван внутренней ошибкой ({e.__class__.__name__}: {e}) — отчет собирается по правилам")
            ai = []
            ai_report.setdefault("failed", []).append({"pages": "весь документ", "reason": f"внутренняя ошибка: {e}",
                                                       "kind": "other"})
        rule_norms = {norm(f["src"]) for f in findings}
        added = 0
        for f in ai:
            if norm(f["src"]) in rule_norms:
                continue
            if f["type"] == "Типографика" and any(norm(f["src"]) in r or r in norm(f["src"]) for r in rule_norms):
                continue
            findings.append(f)
            added += 1
        if ai:
            log(f"ИИ: добавлено замечаний {added} (совпадающих с правилами пропущено {len(ai) - added})")
    elif mode == "rules_ai":
        log("ИИ не подключен — выполнена только проверка правилами")

    findings = autofix.dedupe(findings)
    if not st.get("author_review_on_doubt", True):
        findings = [f for f in findings if f["type"] != AUTHOR or f.get("rule") in ("table", "toc", "requisites")]

    # кандидаты extract.py: каждый — в замечании или в rejected (п. 0.17)
    if should_stop():
        raise Cancelled()
    t = time.time()
    cov = Coverage(findings, rejected)
    n_author = n_rej = 0
    for tok in sorted(candidates(dump)):
        if cov.has(tok):
            continue
        pages = []
        for k in TOKEN_LISTS:
            v = dump.get(k)
            if isinstance(v, dict) and tok in v:
                pages = v[tok]
            elif isinstance(v, list):
                pages += [r["page"] for r in v if r.get("text") == tok]
        pages += [r["page"] for r in dump.get("dangling_prepositions", []) if " ".join(r["tail"].split()[-2:]) == tok]
        page = pages[0] if pages else 1
        zone, ln = idx.locate(page, tok)
        src = autofix.window(yo(ln["text"]), yo(tok)) if ln else ""
        if st.get("unresolved_candidates", "author") == "author" and ln and yo(tok) in src:
            findings.append(dict(page=page, clause="текст", type=AUTHOR, src=src, bad=yo(tok), fix="—", good="—",
                                 note=f"Кандидат машинной проверки «{yo(tok)}» не подтвержден автоматически; "
                                      "место требует решения корректора или автора.", rule="candidate"))
            cov.add(src)
            n_author += 1
        else:                                          # нет дословного совпадения (пробелы) — в rejected, иначе проверка 20
            rejected.append({"text": tok, "reason": "не подтверждено автоматически — требует ручного просмотра"})
            cov.add(tok)
            n_rej += 1
    findings = autofix.dedupe(findings)
    if n_author or n_rej:
        log(f"Неподтвержденные кандидаты машинной проверки: {n_author} — «Требует проверки автором», {n_rej} — в список "
            f"отклоненных ({dur(time.time() - t)})")

    # категории п. 3.4.2 → reviewed
    reviewed = []
    cats = {k: v for k, v in dump.get("typography", {}).items() if v}
    for key, nm in CAT_NAMES.items():
        if dump.get(key):
            cats[nm] = dump[key]
    for nm, items in cats.items():
        reviewed.append({"category": nm, "result": f"Проверено автоматически: {len(items)} кандидатов; дефекты вынесены "
                                                  "в замечания по правилам сервиса, остальное — не дефекты."})
    thr = int(st.get("learn_reject_threshold") or 0)
    if thr:
        try:
            from .. import analytics
            learned = analytics.learned_rejections(thr, exclude_job=job_id)
            hidden = 0
            for f in findings:
                n = learned.get(analytics.learn_key(f.get("rule"), f.get("bad")))
                if n:
                    f["status"], f["auto"] = "rejected", "learned"
                    f["note"] = (f["note"].rstrip() + f" [Скрыто по опыту: корректор отклонял такое замечание в {n} "
                                 f"{plural(n, 'документе', 'документах', 'документах')}.]")
                    hidden += 1
            if hidden:
                log(f"Обучение на решениях корректора: скрыто {hidden} замечаний, которые раньше всегда отклонялись")
        except Exception as e:  # noqa
            log(f"⚠ обучение на решениях пропущено: {e}")
    for i, f in enumerate(findings, 1):
        f.setdefault("status", "new")
        f["id"] = i
    by_src = {}
    for f in findings:
        k = "ИИ" if f.get("rule") == "ai" else "кандидаты" if f.get("rule") == "candidate" else "правила"
        by_src[k] = by_src.get(k, 0) + 1
    log(f"Замечаний всего: {len(findings)} (" + ", ".join(f"{k} — {v}" for k, v in by_src.items()) + f"); "
        f"в списке отклоненных кандидатов: {len(rejected)}")
    meta = make_meta(name + ".pdf", dump, findings, pf, st, ocr_done, scans)
    gap = llm.not_checked(ai_report)
    if gap:
        meta["header_lines"].append(clean(yo(gap))[:600])
    if warnings:
        meta["header_lines"].append(clean(yo("Не выполнены из-за ошибки: " + "; ".join(warnings)))[:600])
    state = {"findings": findings, "rejected": rejected, "reviewed": reviewed, "meta": meta,
             "pages": dump.get("pages_count", 0), "preflight": pf, "running": dump.get("running_titles", {}) if st.get("check_running", True) else {},
             "page_refs": dump.get("page_refs", []), "report_base": report_name(st, name, job_id),
             "style": _yo_all(style_res), "products": _yo_all(products_res), "images": _yo_all(images_res),
             "warnings": warnings}
    if ai_report is not None:
        state["ai"] = ai_report
    progress(0.85, "Сборка отчета DOCX и машинная проверка (25 требований)")
    t = time.time()
    state["verify"] = build_report(job_dir, state, st, log)
    log(f"Отчет собран за {dur(time.time() - t)}; файлы: " + ", ".join(FORMAT_NAMES.get(k, k) for k in state["verify"].get("files", {})))
    tmp = os.path.join(job_dir, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(job_dir, "state.json"))
    progress(1.0, "Готово")
    return state


FORMAT_NAMES = {"docx": "DOCX", "xlsx": "XLSX", "csv": "CSV", "json": "JSON", "pdf_annot": "PDF с пометками",
                "docx_review": "правки в исходном Word"}
