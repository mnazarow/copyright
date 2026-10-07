# -*- coding: utf-8 -*-
"""Конвейер одного задания: извлечение → правила → словарь → ИИ → findings.json → DOCX → verify."""
import datetime as dt
import json
import os
import re
import subprocess
import sys

import pymupdf

from . import autofix, exports, llm, office, preflight, spell
from .common import AUTHOR, PageIndex, norm, page_list, yo

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
PY = sys.executable

TOKEN_LISTS = ("mixed_alphabet_tokens", "number_unit_no_gap", "cyrillic_times_sign", "latin_single_letters")
CAT_NAMES = {"table_issues": "таблицы", "toc_issues": "содержание", "page_refs": "ссылки «стр. N»"}


def sh(args, cwd, timeout=1800):
    r = subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def render_pages(pdf, outdir, dpi):
    os.makedirs(outdir, exist_ok=True)
    doc = pymupdf.open(pdf)
    for pno, page in enumerate(doc, 1):
        fn = os.path.join(outdir, f"p{pno:03d}.png")
        if not os.path.exists(fn):
            page.get_pixmap(dpi=dpi).save(fn)


def candidates(dump):
    c = set(dump.get("mixed_alphabet_tokens", {}).keys())
    for k in ("number_unit_no_gap", "cyrillic_times_sign", "latin_single_letters"):
        c |= {r["text"] for r in dump.get(k, [])}
    c |= {" ".join(r["tail"].split()[-2:]) for r in dump.get("dangling_prepositions", [])}
    return c


def covered(token, findings, rejected):
    hay = norm(" ".join(f["src"] for f in findings) + " " + " ".join(str(r.get("text", "")) for r in rejected))
    return norm(token) in hay


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


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


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
    return {"header_lines": hl, "totals": totals, "summary": make_summary(findings, n)}


def make_summary(findings, pages):
    if not findings:
        return f"Проверено {pages} страниц. Замечаний не найдено: текст и данные документа согласованы."
    a = sum(1 for f in findings if f["type"] == AUTHOR)
    sysm = sorted([f for f in findings if f.get("systemic")], key=lambda f: -f["systemic"])[:3]
    parts = [f"Проверено {pages} страниц, найдено {len(findings)} замечаний, из них {a} требуют решения автора."]
    if sysm:
        parts.append("Системные дефекты: " + "; ".join(
            f"{LABELS.get(f.get('rule'), f['type'].lower())} ({f['systemic']} мест)" for f in sysm) + ".")
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
          "pct": "запись знака процента", "forbidden": "запрещенные слова", "lt": "грамматика"}


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
    return re.sub(r'[\\/:*?"<>|]', "_", base).strip() or "отчет_корректура"


def build_report(job_dir, state, st, log):
    """Пишет findings.json из состояния задания, собирает DOCX, проверяет, делает доп. форматы."""
    fl = [f for f in state["findings"] if f.get("status") != "rejected"]
    if st.get("report_reviewed_only"):
        fl = [f for f in fl if f.get("status") == "accepted"]
    if not st.get("report_include_author", True):
        fl = [f for f in fl if f["type"] != AUTHOR]
    fl = sorted(fl, key=lambda f: (f["page"], str(f["clause"])))
    meta = dict(state["meta"])
    a = sum(1 for f in fl if f["type"] == AUTHOR)
    meta["totals"] = f"Всего замечаний: {len(fl)} (из них ошибок — {len(fl) - a}, требует проверки автором — {a})"
    if st.get("report_summary_auto", True) and not state.get("summary_edited"):
        meta["summary"] = make_summary(fl, state.get("pages", 0))
    rejected = list(state.get("rejected", []))
    # убранные из отчета замечания не должны оставлять кандидатов неразобранными
    for f in state["findings"]:
        if f not in fl:
            rejected.append({"text": f["src"], "reason": "замечание отклонено корректором или исключено настройками"})
    data = {"document": meta,
            "findings": [{k: f[k] for k in ("page", "clause", "type", "src", "bad", "fix", "good", "note")} for f in fl],
            "rejected": rejected, "reviewed": state.get("reviewed", [])}
    json.dump(data, open(os.path.join(job_dir, "findings.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    base = state["report_base"]
    docx = base + ".docx"
    code, out, err = sh([os.path.join(SCRIPTS, "build.py"), "findings.json", docx], job_dir)
    if code:
        log("Сборка DOCX упала: " + err[-800:])
        return {"ok": False, "passed": 0, "total": 25, "checks": [], "error": err[-800:]}
    code, out, err = sh([os.path.join(SCRIPTS, "verify.py"), docx, "findings.json", "dump.json"], job_dir)
    ver = parse_verify(out)
    open(os.path.join(job_dir, "verify.log"), "w", encoding="utf-8").write(out + err)
    log(f"Машинная проверка оформления: {ver['passed']}/{ver['total']} " + ("PASS" if ver["ok"] else "— есть нарушения"))
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


def process(job_dir, name, st, log, progress, job_id=0):
    pdf = os.path.join(job_dir, "source.pdf")
    if not os.path.exists(pdf):
        src = office.find_source(job_dir)
        if not src:
            raise RuntimeError("Исходный файл не найден")
        progress(0.02, "Преобразование документа в PDF (LibreOffice)")
        office.to_pdf(src, pdf)
        log(f"Документ {os.path.basename(src)} преобразован в PDF")
    progress(0.03, "Извлечение текста и координат строк")
    code, out, err = sh([os.path.join(SCRIPTS, "extract.py"), pdf], job_dir)
    open(os.path.join(job_dir, "extract.log"), "w", encoding="utf-8").write(out + err)
    if code:
        raise RuntimeError("Не удалось разобрать PDF: " + (err.strip().splitlines() or ["нет текстового слоя"])[-1])
    dump = json.load(open(os.path.join(job_dir, "dump.json"), encoding="utf-8"))
    if dump.get("pages_count", 0) > int(st.get("max_pages") or 10 ** 6):
        raise RuntimeError(f"Документ длиннее {st.get('max_pages')} страниц (настройка «Максимум страниц»)")
    progress(0.12, "Изображения страниц")
    render_pages(pdf, os.path.join(job_dir, "pages"), int(st.get("page_images_dpi") or 72))

    pf = None
    scans, ocr_done = [], []
    if st.get("preflight_enabled", True):
        progress(0.18, "Технический контроль PDF")
        pf = preflight.preflight(pdf, st, dump)
        scans = pf["scans"]
        if scans and st.get("ocr_enabled"):
            progress(0.2, "Распознавание страниц-сканов (OCR)")
            ocr_done = preflight.ocr_pages(pdf, scans, dump, log)
            if ocr_done:
                json.dump(dump, open(os.path.join(job_dir, "dump.json"), "w", encoding="utf-8"), ensure_ascii=False)
    style_res = None
    if st.get("style_enabled", True):
        progress(0.22, "Стандарт оформления: шрифты, кегли, цвета, поля")
        try:
            from . import style
            style_res = style.check(pdf, st)
            log(f"Стандарт оформления: {style.summary(style_res)}")
        except Exception as e:  # noqa
            log(f"Проверка оформления пропущена: {e}")
    products_res = None
    if st.get("products_enabled", True):
        try:
            from . import products
            ref = products.load(products.PATH())
            if ref:
                progress(0.24, "Сверка со справочником продукции")
                products_res = products.check(dump, ref, st)
                log(f"Справочник продукции ({products_res['loaded']} позиций): найдено артикулов "
                    f"{products_res['found']}, замечаний {len(products_res['issues'])}")
        except Exception as e:  # noqa
            log(f"Сверка со справочником пропущена: {e}")
    idx = PageIndex(dump)
    passes = set(st.get("passes") or [])
    hits, rejected = [], []
    progress(0.25, "Правила: типографика, алфавиты, висячие предлоги")
    if "text" in passes:
        hits += autofix.typography(idx, st)
        hits += autofix.percent(idx, st)
    if "layout" in passes and st.get("hanging_check", True):
        h, r = autofix.hanging(idx, dump, st)
        hits += h
        rejected += r
    if "codes" in passes and st.get("check_codes", True):
        h, r = autofix.alphabets(idx, dump, st)
        hits += h
        rejected += r
    if "terms" in passes:
        hits += autofix.house(idx, st)
        hits += autofix.phones(idx, st)
    if "data" in passes:
        hits += autofix.data_checks(idx, dump, st)
        hits += autofix.requisites(idx, st)
    if "text" in passes:
        progress(0.35, "Словарная проверка")
        hits += spell.hunspell(idx, st, log)
        hits += spell.languagetool(idx, st, log)
    log(f"Правила и словарь: {len(hits)} находок")
    findings, rej2 = autofix.build_findings(hits, int(st.get("systemic_threshold") or 5))
    rejected += rej2

    mode = st.get("mode", "auto")
    use_ai = mode == "rules_ai" or (mode == "auto" and st.get("ai_provider") not in (None, "", "none"))
    if use_ai or os.environ.get("KORREKTOR_AI_MOCK"):
        progress(0.45, "ИИ: вычитка смысла по мастер-промпту")
        extra = "\n".join(x for x in [st.get("ai_extra_instructions") or "",
                                      ("Сокращения компании:\n" + st["abbreviations"]) if st.get("abbreviations") else "",
                                      ("Глоссарий компании (правильно: неправильно):\n" + st["glossary"]) if st.get("glossary") else ""] if x)
        ai = llm.run(dump, idx, st, extra, log, lambda x: progress(0.45 + 0.35 * x, "ИИ: вычитка смысла по мастер-промпту"))
        rule_norms = {norm(f["src"]) for f in findings}
        for f in ai:
            if norm(f["src"]) in rule_norms:
                continue
            if f["type"] == "Типографика" and any(norm(f["src"]) in r or r in norm(f["src"]) for r in rule_norms):
                continue
            findings.append(f)
    elif mode == "rules_ai":
        log("ИИ не подключен — выполнена только проверка правилами")

    findings = autofix.dedupe(findings)
    if not st.get("author_review_on_doubt", True):
        findings = [f for f in findings if f["type"] != AUTHOR or f.get("rule") in ("table", "toc", "requisites")]

    # кандидаты extract.py: каждый — в замечании или в rejected (п. 0.17)
    for tok in sorted(candidates(dump)):
        if covered(tok, findings, rejected):
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
        if st.get("unresolved_candidates", "author") == "author" and ln:
            src = autofix.window(yo(ln["text"]), yo(tok))
            findings.append(dict(page=page, clause="текст", type=AUTHOR, src=src, bad=yo(tok), fix="—", good="—",
                                 note=f"Кандидат машинной проверки «{yo(tok)}» не подтвержден автоматически; "
                                      "место требует решения корректора или автора.", rule="candidate"))
        else:
            rejected.append({"text": tok, "reason": "не подтверждено автоматически — требует ручного просмотра"})
    findings = autofix.dedupe(findings)

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
                    f["note"] = f["note"].rstrip() + f" [Скрыто по опыту: корректор отклонял такое замечание в {n} документах.]"
                    hidden += 1
            if hidden:
                log(f"Обучение на решениях корректора: скрыто {hidden} замечаний, которые раньше всегда отклонялись")
        except Exception as e:  # noqa
            log(f"Обучение на решениях пропущено: {e}")
    for i, f in enumerate(findings, 1):
        f.setdefault("status", "new")
        f["id"] = i
    meta = make_meta(name + ".pdf", dump, findings, pf, st, ocr_done, scans)
    state = {"findings": findings, "rejected": rejected, "reviewed": reviewed, "meta": meta,
             "pages": dump.get("pages_count", 0), "preflight": pf, "running": dump.get("running_titles", {}),
             "page_refs": dump.get("page_refs", []), "report_base": report_name(st, name, job_id),
             "style": style_res, "products": products_res}
    progress(0.85, "Сборка отчета DOCX и машинная проверка (25 требований)")
    state["verify"] = build_report(job_dir, state, st, log)
    tmp = os.path.join(job_dir, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(job_dir, "state.json"))
    progress(1.0, "Готово")
    return state
