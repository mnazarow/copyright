# -*- coding: utf-8 -*-
"""Правила: превращают текст страниц и кандидатов extract.py в готовые замечания.

Каждое правило выдает «попадания» (hit): страница, строка, ошибочный фрагмент и исправление.
Функция build_findings() собирает из попаданий строки отчета: режет цитату 3–15 слов,
объединяет повторы в системные строки (п. 5.4) и следит, чтобы одно место не попало дважды.
"""
import re
from collections import defaultdict

from .common import (AUTHOR, CYR2LAT, LAT2CYR, kind, norm, page_list, plural, window, yo)

NB = " "
STD = re.compile(r"(ГОСТ|СП|СНиП|ТУ|ФЗ|ISO|IEC|МЭК|EN|DIN|№|ТР)\s*[\w.]*$")


class Hit(dict):
    pass


def hit(rule, page, line, bad, good, type_, note, group=None, token=None, zone="body", clause=None):
    return Hit(rule=rule, page=page, line=line, bad=bad, good=good, type=type_, note=note,
               group=group if group is not None else rule, token=token or bad, zone=zone, clause=clause)


def each_line(idx):
    for page, p in idx.pages.items():
        for zone, _, ln in p["items"]:
            yield page, zone, ln["text"]


# ------------------------------------------------------------------ типографика
def rule_regex(idx, rule, rx, repl, type_, note, cond=None, group_by_bad=False):
    out = []
    for page, zone, text in each_line(idx):
        for m in re.finditer(rx, text):
            if cond and not cond(m, text):
                continue
            bad = m.group(0)
            good = m.expand(repl) if isinstance(repl, str) else repl(m)
            if good == bad:
                continue
            out.append(hit(rule, page, text, bad, good, type_, note, zone=zone,
                           group=(rule, bad) if group_by_bad else rule))
    return out


def range_cond(m, text):
    a, b = (float(x.replace(",", ".")) for x in m.groups())
    before = text[max(0, m.start() - 14):m.start()]
    return a < b and not STD.search(before) and not re.search(r"[A-Za-z]\s*$", before)


def typography(idx, st):
    H = []
    if st.get("typo_range_dash"):
        H += rule_regex(idx, "range", r"(?<![\w./\-№])(\d+(?:,\d+)?)-(\d+(?:,\d+)?)(?![\w./\-])", r"\1–\2",
                        "Типографика", "В диапазоне чисел ставится короткое тире без пробелов, а не дефис (п. 5.2).",
                        cond=range_cond)
    if st.get("typo_space_after_dot"):
        H += rule_regex(idx, "dot", r"([а-яё]{3,})\.([А-ЯЁа-яё]{3,})", r"\1. \2", "Типографика",
                        "После точки пропущен пробел.")
    if st.get("typo_digit_word"):
        H += rule_regex(idx, "digitword", r"(?<![\w])(\d+[A-Z]?)([а-яё]{3,})", r"\1 \2", "Типографика",
                        "Пропущен пробел между обозначением (числом) и следующим словом.")
    if st.get("typo_double_space"):
        H += rule_regex(idx, "dspace", r"([^\s=])[  ]{2,}([^\s=])", r"\1 \2", "Типографика",
                        "Двойной пробел между словами.")
    if st.get("typo_degree"):
        H += rule_regex(idx, "degree", r"(\d)?(\s*)[\u02da\u00ba](?:\s*([CС])(?![\wА-яЁё]))?",
                        lambda m: ((m.group(1) + " " if m.group(1) else m.group(2)) + "\u00b0" + m.group(3)) if m.group(3)
                        else (m.group(1) or "") + m.group(2) + "\u00b0",
                        "Типографика", "Вместо знака градуса ° (U+00B0) стоит кольцо ˚ или индикатор º; "
                                       "между числом и «°С» нужен пробел (п. 5.3).")
    if st.get("typo_quotes"):
        if st.get("quote_style", "ru") == "ru":
            H += rule_regex(idx, "quotes", r'"([^"\n]{1,40})"', r"«\1»", "Типографика",
                            "Прямые кавычки вместо «елочек»; внутренние кавычки — „лапки“.")
        else:                                          # английские кавычки (для англоязычных документов)
            H += rule_regex(idx, "quotes", r'"([^"\n]{1,40})"|«([^«»\n]{1,40})»',
                            lambda m: "\u201c" + (m.group(1) or m.group(2)) + "\u201d", "Типографика",
                            "В документе приняты английские кавычки “…” (настройка «Кавычки»).")
    if st.get("typo_inch"):
        H += rule_regex(idx, "inch", r"(\d|½|¼|¾)\s*(?:''|’’)", r"\1″", "Типографика",
                        "Знак дюйма — двойной штрих ″ (U+2033), а не два апострофа.")
    if st.get("typo_equals"):
        H += rule_regex(idx, "equals", r"[\wА-яЁё]+\s?=\s?[\wА-яЁё]+",
                        lambda m: re.sub(r"\s*=\s*", " = ", m.group(0)), "Типографика",
                        "Знак «=» отбивается пробелами с обеих сторон (п. 5.7).",
                        cond=lambda m, t: " = " not in m.group(0))
    if st.get("typo_unit"):
        H += rule_regex(idx, "unit", r"(?<![\w.])(\d+(?:,\d+)?)(мм|см|мкм|МПа|бар|кг|м³|°С|°C|кВт|шт|Вт|Гц|л|В|м)(?![\wА-яЁё])",
                        r"\1 \2", "Типографика",
                        "Между числом и единицей измерения нужен неразрывный пробел (п. 5.3, ГОСТ 8.417).")
    if st.get("typo_times"):
        H += rule_regex(idx, "times", r"(\d)\s*[хx×]\s*(\d)", r"\1 × \2", "Типографика",
                        "Знак умножения — «×», отбивается пробелами; кириллическая «х» и латинская «x» "
                        "вместо него недопустимы (п. 5.7, 5.8).",
                        cond=lambda m, t: m.group(0) != m.group(1) + " × " + m.group(2))
    if st.get("typo_mwc"):
        H += rule_regex(idx, "mwc", r"м\.\s?вод\.?\s?ст\.?", "м вод. ст.", "Типографика",
                        "Единица записывается «м вод. ст.»: после «м» точка не ставится.",
                        cond=lambda m, t: m.group(0) != "м вод. ст.")
    # разряды
    th = st.get("thousands", "space5")
    if th == "space5":
        H += rule_regex(idx, "thousands", r"(?<![\d,])(?<!\d[  ])(\d)[  ](\d{3})(?![  ]?\d)(?!,\d)",
                        r"\1\2", "Единообразие",
                        "Четырехзначные числа пишутся слитно, пробелом разбиваются числа от пяти знаков "
                        "(1200, но 12 000).")
    units = r"(?=\s?(?:мм|см|м|м³|м3|км|кг|т|л|шт|руб|₽|Вт|кВт|Па|кПа|МПа|бар|ч|об/мин|°С|°C)(?![\wА-яЁё]))"
    if th == "space4":                                 # «всегда пробел»: 1 200 мм (только числа с единицами — не годы и артикулы)
        H += rule_regex(idx, "thousands", r"(?<![\d,.\w-])(\d)(\d{3})(?![\d,.])" + units, r"\1" + NB + r"\2", "Единообразие",
                        "В документе принято разбивать на разряды и четырехзначные числа (1 200), неразрывным пробелом.")
    if th in ("space4", "space5"):                     # 12000 → 12 000 (числа с единицами)
        H += rule_regex(idx, "thousands", r"(?<![\d,.\w-])(\d{1,3})((?:\d{3}){1,3})(?![\d,.])" + units,
                        lambda m: m.group(1) + "".join(NB + m.group(2)[i:i + 3] for i in range(0, len(m.group(2)), 3)),
                        "Единообразие", "Числа от пяти знаков разбиваются на разряды неразрывным пробелом (12 000).",
                        cond=lambda m, t: len(m.group(1) + m.group(2)) >= 5)
    # десятичный разделитель
    if st.get("decimal_separator", "comma") == "comma":
        H += rule_regex(idx, "decimal", r"(?<![\d.])(\d+)\.(\d{1,3})(?=\s?(?:(?:мм|см|м|кг|кВт|Вт|бар|МПа|л)(?![\wА-яЁё])|%|°))",
                        r"\1,\2", "Типографика", "В русском тексте десятичный разделитель — запятая.")
    else:                                              # точка (англоязычные документы)
        H += rule_regex(idx, "decimal", r"(?<![\d,])(\d+),(\d{1,3})(?=\s?(?:(?:mm|cm|m|kg|kW|W|bar|MPa|l|мм|см|м|кг|кВт|Вт|бар|МПа|л)(?![\wА-яЁё])|%|°))",
                        r"\1.\2", "Типографика", "В документе принята точка как десятичный разделитель (настройка).")
    # стандарты
    if st.get("check_standards"):
        H += rule_regex(idx, "std", r"\b(СП|ГОСТ|СНиП)(-|)(\d)", r"\1 \3", "Типографика",
                        "Между обозначением документа и номером ставится пробел: «СП 30», «ГОСТ 8.417».",
                        cond=lambda m, t: True)
    return H


def percent(idx, st):
    pol = st.get("percent_space", "consistent")
    a = rule_regex(idx, "pct", r"(\d)%", r"\1 %", "Единообразие", "")
    b = rule_regex(idx, "pct", r"(\d) %", r"\1%", "Единообразие", "")
    if pol == "nospace":
        flag = b
    elif pol == "space":
        flag = a
    else:
        if not a or not b:
            return []
        flag = a if len(a) < len(b) else b
    note = "Знак % в документе записывается по-разному: слитно и через пробел; нужен один вариант (п. 5.3)."
    for h in flag:
        h["note"] = note
    return flag


# ------------------------------------------------------------------ висячие предлоги
def hanging(idx, dump, st):
    H, rej = [], []
    excl = set((st.get("hanging_exclude_abbr") or "").split())
    for d in dump.get("dangling_prepositions", []):
        page, tail = d["page"], d["tail"]
        last = tail.split()[-1]
        key2 = " ".join(tail.split()[-2:])
        if last in excl:
            rej.append({"text": key2, "reason": f"стр. {page}: «{last}» — сокращение, а не предлог или союз"})
            continue
        line = None
        for ln in idx.pages.get(page, {}).get("lines", []):
            if ln["block"] == d["block"] and ln["text"].rstrip().strip('.,;:!?»"').endswith(tail):
                line = ln
                break
        nxt = idx.next_line(page, line) if line else None
        if not line or not nxt or len(line["text"].split()) == 1:
            rej.append({"text": key2, "reason": f"стр. {page}: артефакт разбиения выключенной строки на спаны, "
                                                f"предлог стоит внутри строки"})
            continue
        words = line["text"].split()
        first = nxt["text"].split()[0] if nxt["text"].split() else ""
        lead = " ".join(words[-4:])
        src = lead + " " + first
        fix = lead + NB + first
        H.append(hit("hanging", page, src, lead + " " + first, fix, "Типографика",
                     f"Висячий {'союз' if last.lower() in ('и', 'а', 'но', 'или', 'что', 'чтобы', 'как', 'также') else 'предлог'}: "
                     f"«{last}» стоит в конце строки и оторван от следующего слова «{first.strip('.,;:')}». "
                     "Нужен неразрывный пробел.",
                     group=("hanging", page, src), token=key2))
        H[-1]["exact"] = (src, last, fix, last + NB + first)
    return H, rej


# ------------------------------------------------------------------ алфавиты
def convert_run(run, ctx):
    """Решает, в какой алфавит привести слово со смешением. None — не трогать."""
    c = [ch for ch in run if kind(ch) == "c"]
    l_ = [ch for ch in run if kind(ch) == "l"]
    to_cyr = all(ch in LAT2CYR for ch in l_)
    to_lat = all(ch in CYR2LAT for ch in c)
    if not to_cyr and not to_lat:
        return None
    if to_cyr and to_lat:
        if len(c) != len(l_):
            target = "c" if len(c) > len(l_) else "l"
        else:
            target = "l" if re.search(r"[\d\-*_/]", ctx) or run.isupper() else "c"
    else:
        target = "c" if to_cyr else "l"
    if target == "c":
        return "".join(LAT2CYR.get(ch, ch) for ch in run)
    return "".join(CYR2LAT.get(ch, ch) for ch in run)


def alphabets(idx, dump, st):
    H, rej = [], []
    wl = set((st.get("code_whitelist") or "").split())
    for tok, pages in dump.get("mixed_alphabet_tokens", {}).items():
        if tok in wl or any(len(w) > 1 and tok.startswith(w) for w in wl):
            rej.append({"text": tok, "reason": "разрешенный смешанный токен (настройка «Разрешенные смешанные токены»)"})
            continue
        new = tok
        changed = False
        for m in re.finditer(r"[^\W\d_]+", tok):
            run = m.group(0)
            if len({kind(ch) for ch in run} - {"."}) < 2:
                continue
            if re.fullmatch(r"[A-Z][а-яё]+", run):       # Gсет, Hдля — обозначение или слитное слово
                continue
            conv = convert_run(run, tok)
            if conv and conv != run:
                new = new.replace(run, conv, 1)
                changed = True
        if not changed:
            rej.append({"text": tok, "reason": "аббревиатура или обозначение с русской частью — смешение допустимо"})
            continue
        bad_chars = ", ".join(f"«{ch}» U+{ord(ch):04X}" for ch in tok if ch not in new)
        target = "латиницей" if any(kind(ch) == "l" for ch in new if ch not in tok) else "кириллицей"
        for page in sorted(set(pages)):
            zone, ln = idx.locate(page, tok)
            if not ln:
                continue
            H.append(hit("mixed", page, ln["text"], tok, new, "Орфография",
                         f"Смешение алфавитов: в «{tok}» символы {bad_chars} другого алфавита; слово должно быть "
                         f"набрано {target} (п. 5.11).", group=("mixed", tok), zone=zone))
    for d in dump.get("latin_single_letters", []):
        t = d["text"]
        good = re.sub(r"^[aceopxy]", lambda m: LAT2CYR[m.group(0)], t)
        good = re.sub(r"P$", "Р", good)
        zone, ln = idx.locate(d["page"], t)
        if ln and good != t:
            H.append(hit("latin", d["page"], ln["text"], t, good, "Орфография",
                         "Одиночная латинская буква в русском тексте: предлог или буква обозначения набраны латиницей.",
                         group=("latin", t), zone=zone))
    return H, rej


# ------------------------------------------------------------------ стиль компании
def parse_pairs(text):
    out = []
    for line in (text or "").splitlines():
        if ":" in line:
            right, wrong = line.split(":", 1)
            for w in wrong.split(","):
                if w.strip():
                    out.append((right.strip(), w.strip()))
    return out


def keep_case(src, good):
    if src[:1].isupper():
        return good[:1].upper() + good[1:]
    return good


def house(idx, st):
    H = []
    for right, wrong in parse_pairs(st.get("glossary")):
        rx = re.compile(r"(?<![\wА-яЁё-])" + re.escape(wrong) + r"(?![\wА-яЁё])", re.I)
        for page, zone, text in each_line(idx):
            for m in rx.finditer(text):
                H.append(hit("glossary", page, text, m.group(0), keep_case(m.group(0), right), "Единообразие",
                             f"По глоссарию компании пишется «{right}», вариант «{wrong}» не используется.",
                             group=("glossary", wrong.lower()), zone=zone))
    for line in (st.get("forbidden_words") or "").splitlines():
        if not line.strip():
            continue
        word, _, repl = line.partition(":")
        word, repl = word.strip(), repl.strip()
        rx = re.compile(r"(?<![\wА-яЁё])" + re.escape(word) + r"(?![\wА-яЁё])", re.I)
        for page, zone, text in each_line(idx):
            for m in rx.finditer(text):
                if repl:
                    H.append(hit("forbidden", page, text, m.group(0), keep_case(m.group(0), repl), "Стилистика",
                                 f"«{word}» — запрещенное в документах компании слово; принято «{repl}».",
                                 group=("forbidden", word.lower()), zone=zone))
                else:
                    H.append(hit("forbidden", page, text, m.group(0), None, AUTHOR,
                                 f"«{word}» — запрещенное в документах компании слово.",
                                 group=("forbidden", word.lower()), zone=zone))
    for brand in (st.get("brand_case") or "").split():
        rx = re.compile(r"(?<![\wА-яЁё])" + re.escape(brand) + r"(?![\wА-яЁё])", re.I)
        for page, zone, text in each_line(idx):
            if text.upper() == text and len(text) > 12:       # заголовок прописными — не отмечаем
                continue
            for m in rx.finditer(text):
                if m.group(0) != brand:
                    H.append(hit("brand", page, text, m.group(0), brand, "Единообразие",
                                 f"Название пишется «{brand}» (эталон написания брендов и серий).",
                                 group=("brand", m.group(0)), zone=zone))
    return H


def nearest_address(lines, i):
    """Ближайший адрес выше строки с телефоном; поиск останавливается на другом контакте."""
    for j in range(i, max(-1, i - 5), -1):
        if j != i and (PHONE.search(lines[j]) or "@" in lines[j]):
            return None
        if ADDR.search(lines[j]):
            return lines[j]
    return None


PHONE = re.compile(r"\+7[\s ]*\(?\d{3}\)?[\s ]*\d{3}[\s -]?\d{2}[\s -]?\d{2}")
ADDR = re.compile(r"(ул\.|пр-т|пр-кт|проспект|пер\.|ш\.|наб\.|мкр|вл\.|д\.\s*\d)")


def phones(idx, st):
    if not st.get("check_phone_format"):
        return []
    found = []
    for page, zone, text in each_line(idx):
        for m in PHONE.finditer(text):
            found.append((page, zone, text, m.group(0)))
    br = [f for f in found if "(" in f[3]]
    nb = [f for f in found if "(" not in f[3]]
    if not br or not nb:
        return []
    minority = br if len(br) < len(nb) else nb
    H = []
    for page, zone, text, ph in minority:
        digits = re.sub(r"\D", "", ph)
        good = (f"+7 {digits[1:4]} {digits[4:7]} {digits[7:9]} {digits[9:11]}" if minority is br
                else f"+7 ({digits[1:4]}) {digits[4:7]} {digits[7:9]} {digits[9:11]}")
        H.append(hit("phone", page, text, ph, good, "Единообразие",
                     "Телефоны в документе записаны по-разному: с кодом в скобках и без; нужен один формат.",
                     group=("phone", ph), zone=zone))
    return H


def requisites(idx, st):
    """Эталонные реквизиты: телефон или e-mail рядом с другим адресом."""
    ref = []
    for line in (st.get("requisites") or "").splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) >= 2:
            ref.append((name.strip(), parts[0], [p for p in parts[1:] if p]))
    H = []
    for name, addr, keys in ref:
        for page, p in idx.pages.items():
            lines = [ln["text"] for ln in p["lines"]]
            for i, t in enumerate(lines):
                if not any(norm(k) and norm(k) in norm(t) for k in keys) or "доб." in t:
                    continue                      # добавочный номер — другое подразделение (склад)
                near = nearest_address(lines, i)
                if near and norm(addr) not in norm(near) and norm(near) not in norm(addr):
                    H.append(hit("requisites", page, near, near, None, AUTHOR,
                                 f"Эталонные реквизиты ({name}): рядом с «{keys[0]}» должен стоять адрес «{addr}», "
                                 f"в документе — «{near.strip()}» (п. 5.12).", group=("req", page, near)))
    return H


# ------------------------------------------------------------------ данные
def data_checks(idx, dump, st):
    H = []
    if st.get("check_tables"):
        for r in dump.get("table_issues", []):
            if r["kind"].startswith("параметр"):
                q = r["text"]
                note = (f"Параметр «{q}» приведен в таблице дважды с разными значениями; одна из строк, "
                        "вероятно, должна называться иначе или значения перепутаны.")
            else:
                m = re.search(r"ряд (\d+):.*→ ряд (\d+)", r["text"])
                q = f"{m.group(2)} ряд" if m else r["text"]
                note = (f"Диапазоны соседних рядов не стыкуются: {r['text']}. Конец одного ряда должен "
                        "совпадать с началом следующего; ряды повторяются или между ними пропуск.")
            if idx.has(r["page"], q):
                H.append(hit("table", r["page"], q, q, None, AUTHOR, note, group=("table", r["page"], q),
                             clause="таблица"))
    if st.get("check_toc"):
        for r in dump.get("toc_issues", []):
            q = r["text"]
            if idx.has(r["page"], q):
                where = page_list(r.get("found") or [])
                note = (f"В содержании «{q}» указано на стр. {r['printed']}, " +
                        (f"а заголовок стоит на стр. {where}." if where else "такой страницы в файле нет."))
                H.append(hit("toc", r["page"], q, q, None, AUTHOR, note, group=("toc", q), clause="содержание"))
    if st.get("check_refs"):
        for r in dump.get("page_refs", []):
            if not r.get("exists"):
                H.append(hit("ref", r["page"], r["text"], r["text"], None, "Логика",
                             f"Ссылка «{r['text']}» ведет на страницу, которой нет в документе "
                             f"(всего {idx.count()} стр.).", group=("ref", r["page"], r["text"])))
    return H


# ------------------------------------------------------------------ рисунки и таблицы
CAPTION = re.compile(r"^\s*(Рис(?:\.|унок)|Табл(?:\.|ица))\s*(\d{1,3}(?:\.\d{1,3})?)(?=$|[\s.:—–-])")
FIGREF = re.compile(r"(?<![\w])(рис(?:\.|ун(?:ок|ке|ка|ку|ком))|табл(?:\.|иц(?:а|е|у|ы|ей)))\s*(\d{1,3}(?:\.\d{1,3})?)(?![\d]|\.\d)",
                    re.I)
FIG_KIND = {"р": ("fig", "рисунок", "рисунка"), "т": ("tab", "таблица", "таблицы")}


def _num_key(n):
    a, _, b = n.partition(".")
    return (int(a), int(b)) if b else (0, int(a))


def figures(idx, st):
    """Нумерация рисунков и таблиц: повторы, пропуски, нарушенный порядок, ссылки на несуществующие номера."""
    if not st.get("check_figures", True):
        return []
    caps = {"fig": [], "tab": []}
    refs = []
    for page in sorted(idx.pages):
        for zone, _, ln in idx.pages[page]["items"]:
            if zone != "body":
                continue
            text = ln["text"]
            m = CAPTION.match(text)
            cap_end = -1
            if m and not re.search(r"(?i)продолж|окончан", text):
                k = FIG_KIND[m.group(1)[0].lower()][0]
                caps[k].append({"num": m.group(2), "page": page, "line": text, "label": m.group(0).strip()})
                cap_end = m.end()
            for r in FIGREF.finditer(text):
                if r.start() < cap_end or (r.start() == 0 and m):
                    continue
                refs.append({"kind": FIG_KIND[r.group(1)[0].lower()][0], "num": r.group(2), "page": page, "line": text,
                             "label": r.group(0)})
    H = []
    for k, items in caps.items():
        if not items:
            continue
        name, gen = {"fig": ("рисунок", "рисунка"), "tab": ("таблица", "таблицы")}[k]
        seen = {}
        for c in items:                               # повторы номера
            if c["num"] in seen:
                p0 = seen[c["num"]]["page"]
                H.append(hit("figures", c["page"], c["line"], c["label"], None, AUTHOR,
                             f"Номер {gen} {c['num']} уже использован на стр. {p0}: у каждого {gen} должен быть свой номер.",
                             group=("figures", k, "dup", c["num"], c["page"]), clause="нумерация"))
            else:
                seen[c["num"]] = c
        groups = defaultdict(list)                    # пропуски и порядок — внутри раздела (2.1, 2.2 …)
        for c in seen.values():
            groups[c["num"].partition(".")[0] if "." in c["num"] else ""].append(c)
        for g, lst in groups.items():
            present = {_num_key(c["num"])[1] for c in lst}
            prev = None
            for c in lst:                             # в порядке появления в документе
                key = _num_key(c["num"])
                if prev is not None and key < _num_key(prev["num"]):
                    H.append(hit("figures", c["page"], c["line"], c["label"], None, AUTHOR,
                                 f"Нарушен порядок нумерации: {name} {c['num']} стоит после {gen} {prev['num']} "
                                 f"(стр. {prev['page']}).", group=("figures", k, "ord", c["num"]), clause="нумерация"))
                elif prev is not None and key[1] - _num_key(prev["num"])[1] > 1 and key[0] == _num_key(prev["num"])[0] \
                        and [x for x in range(_num_key(prev["num"])[1] + 1, key[1]) if x not in present]:
                    miss = [x for x in range(_num_key(prev["num"])[1] + 1, key[1]) if x not in present]
                    nums = ", ".join((f"{g}." if g else "") + str(x) for x in miss[:5])
                    H.append(hit("figures", c["page"], c["line"], c["label"], None, AUTHOR,
                                 f"После {gen} {prev['num']} (стр. {prev['page']}) идет {name} {c['num']} — "
                                 f"пропущен номер {nums}.", group=("figures", k, "gap", c["num"]), clause="нумерация"))
                prev = c
        have = set(seen)
        for r in refs:                                # ссылки на несуществующие номера
            if r["kind"] != k or r["num"] in have:
                continue
            nums = sorted(have, key=_num_key)
            H.append(hit("figures", r["page"], r["line"], r["label"], None, AUTHOR,
                         f"Ссылка «{r['label']}», а {gen} с таким номером в документе нет "
                         f"(есть: {', '.join(nums[:8])}{' …' if len(nums) > 8 else ''}).",
                         group=("figures", k, "ref", r["num"], r["page"]), clause="ссылка"))
    return H


# ------------------------------------------------------------------ сборка замечаний
def to_finding(h):
    """Попадание → строка отчета (цитата дословно из строки, исправление — та же цитата)."""
    if h.get("exact"):
        src, bad, fix, good = (yo(x) for x in h["exact"])
        return dict(page=h["page"], clause=h.get("clause") or "текст", type=h["type"], src=src, bad=bad,
                    fix=fix, good=good, note=yo(h["note"]), rule=h["rule"])
    line = yo(h["line"])
    bad = yo(h["bad"])
    src = window(line, bad)
    if bad not in src:
        src = bad
    h["note"] = yo(h["note"])
    if h["good"] is None or h["type"] == AUTHOR:
        return dict(page=h["page"], clause=h.get("clause") or ("колонтитул" if h["zone"] != "body" else "текст"),
                    type=AUTHOR, src=src, bad=bad, fix="—", good="—", note=h["note"], rule=h["rule"])
    good = yo(h["good"])
    h["note"] = yo(h["note"])
    fix = src.replace(bad, good, 1)
    if fix == src:
        return None
    # маркеры не должны состоять только из тире (build.py считает их пустыми)
    if bad.strip() in {"-", "–", "—"} or good.strip() in {"-", "–", "—"}:
        bad, good = src, fix
    return dict(page=h["page"], clause=h.get("clause") or ("колонтитул" if h["zone"] != "body" else "текст"),
                type=h["type"], src=src, bad=bad, fix=fix, good=good, note=h["note"], rule=h["rule"])


def build_findings(hits, threshold):
    groups = defaultdict(list)
    for h in hits:
        groups[h["group"]].append(h)
    # правило с одной группой на весь документ (типографика) группируется по правилу
    findings, rejected = [], []
    for key, hs in groups.items():
        pages = sorted({h["page"] for h in hs})
        if len(hs) >= threshold and hs[0]["rule"] not in ("hanging", "table", "toc", "ref", "requisites"):
            f = to_finding(hs[0])
            if not f:
                continue
            examples = sorted({yo(h["bad"]) for h in hs})[:4]
            f["clause"] = f"системно: стр. {page_list(pages)} ({len(hs)} {plural(len(hs), 'раз', 'раза', 'раз')})"
            f["note"] = (f["note"] + f" Системно: {len(hs)} {plural(len(hs), 'место', 'места', 'мест')}" +
                         (f", например {', '.join('«' + e + '»' for e in examples)}." if len(examples) > 1 else "."))
            f["systemic"] = len(hs)
            findings.append(f)
            for h in hs[1:]:
                rejected.append({"text": h["token"], "reason": f"учтено системной строкой, стр. {f['page']}"})
        else:
            for h in hs:
                f = to_finding(h)
                if f:
                    findings.append(f)
    return findings, rejected


def merge_same_place(g, f):
    """Два разных дефекта в одной цитате → одна строка с двумя исправлениями (п. 0.7)."""
    if g["type"] == AUTHOR or f["type"] == AUTHOR or f["fix"] == "—":
        if f["note"] not in g["note"]:
            g["note"] += " " + f["note"]
        return True
    if f["bad"] in g["fix"] and f["bad"] != f["good"]:
        g["fix"] = g["fix"].replace(f["bad"], f["good"], 1)
        g["bad"] = g["bad"] + " … " + f["bad"] if f["bad"] not in g["bad"] else g["bad"]
        g["good"] = g["good"] + " … " + f["good"] if f["good"] not in g["good"] else g["good"]
        if g["good"] not in g["fix"]:
            g["good"] = f["good"]
        if f["note"] not in g["note"]:
            g["note"] += " " + f["note"]
        return True
    return False


def dedupe(findings):
    """Одна строка на одно место: по тексту цитаты и по адресу (п. 0.7, 0.14)."""
    seen, out = {}, []
    for f in findings:
        k = norm(f["src"]).lower()
        if k in seen and seen[k]["page"] == f["page"] and (f.get("rule") != seen[k].get("rule")
                                                            or f.get("bad") not in seen[k].get("bad", "")):
            if merge_same_place(seen[k], f):           # второй дефект в той же строке (в т. ч. то же правило)
                continue
        if k in seen:
            g = seen[k]
            if f["page"] != g["page"] and "также стр." not in g["clause"] and "системно" not in g["clause"]:
                g["clause"] += f" (также стр. {f['page']})"
            elif "также стр." in g["clause"] and f"стр. {f['page']}" not in g["clause"] and f["page"] != g["page"]:
                g["clause"] = g["clause"].rstrip(")") + f", {f['page']})"
            if f["note"] not in g["note"] and f.get("rule") != g.get("rule"):
                g["note"] += " " + f["note"]
            continue
        seen[k] = f
        out.append(f)
    return out
