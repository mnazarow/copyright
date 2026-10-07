# extract.py — извлекает структуру PDF: страницы, пункты, строки по координатам, токены.
# Файл берется из вложения. Нумерация страниц всегда физическая: номер = порядок в файле.
# Запуск:  python extract.py "input.pdf" [--png ПАПКА]
# --png ПАПКА — сохранить изображения страниц (72 dpi) для визуальной проверки.
# Папка должна быть рабочей, а не папкой с исходными документами.
import sys, json, re, unicodedata, os, difflib
import pymupdf

ARGS = [a for a in sys.argv[1:]]
PNG = None
if "--png" in ARGS:
    i = ARGS.index("--png"); PNG = ARGS[i + 1]; del ARGS[i:i + 2]
PATH = ARGS[0] if ARGS else "input.pdf"
doc = pymupdf.open(PATH)

PREP = set("в во на за к ко с со о об обо от ото по до из изо без безо для при про над надо под подо перед передо через чрез между меж около кроме вместо сверх вдоль внутрь вроде вслед мимо ради среди сквозь согласно благодаря вопреки насчет".split())
UNION = set("и а но да или либо что чтобы как если чем то тоже также причем притом зато однако когда пока хотя будто словно точно".split())
PART = set("не ни же ли бы б ведь вот вон лишь только именно даже уж уже".split())
STOP = PREP | UNION | PART
CLAUSE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})+|\d{1,2})\s*[.\t]")

def kind(ch):
    n = unicodedata.name(ch, "")
    if "CYRILLIC" in n: return "c"
    if "LATIN" in n: return "l"
    return "."

pages, mixed, nogap, dangling, cyr_times, lat_single = [], {}, [], [], [], []
# типографские кандидаты по категориям (п. 3.4.2): разбираются по категории целиком
TYPO = {k: [] for k in ("дефис в диапазоне", "нет пробела после точки", "цифра слитно со словом",
                        "двойной пробел", "неверный знак градуса", "прямые кавычки",
                        "дюйм апострофами", "знак = без отбивки", "единица слитно с числом",
                        "× без отбивки", "запись «м вод. ст.»")}
TYPO_RX = [
    ("дефис в диапазоне", r"(?<![\w./\-№])(\d+(?:,\d+)?)-(\d+(?:,\d+)?)(?![\w./\-])"),
    ("нет пробела после точки", r"[а-я\u0451]{3,}\.[А-Я\u0401а-я\u0451]{3,}"),
    ("цифра слитно со словом", r"(?<![\w])\d+[A-Z]?[а-я\u0451]{3,}"),
    ("двойной пробел", r"[^\s=][ \u00a0]{2,}[^\s=]"),
    ("неверный знак градуса", r"[\u02da\u00ba][CС]?"),
    ("прямые кавычки", r'"[^"\n]{1,40}"'),
    ("дюйм апострофами", r"\d\s*(?:''|\u2019\u2019)"),
    ("знак = без отбивки", r"[\wА-я\u0401\u0451]=[\wА-я\u0401\u0451]|[\wА-я\u0401\u0451]= \S|\S =[\wА-я\u0401\u0451]"),
    ("единица слитно с числом", r"\d(?:л|В|Вт|кВт|Гц|об/мин)\b"),
    ("× без отбивки", r"\d[×xх]\d"),
    ("запись «м вод. ст.»", r"м\.\s?вод|вод\.ст"),
]
STD = re.compile(r"(ГОСТ|СП|СНиП|ТУ|ФЗ|ISO|IEC|МЭК|EN|DIN|№|ТР)\s*[\w.]*$")


def merge_baseline(bl):
    """Склейка кусков одной строки. PDF из Word/LibreOffice при подмене шрифта дробит строку на части
    («Диапазон давления» / « 10-20 » / «бар» / «.»), и PyMuPDF отдает их отдельными строками в порядке шрифтов.
    Части с общей базовой линией, стоящие рядом (зазор меньше кегля), собираются в одну строку слева направо."""
    if len(bl) < 2:
        return bl
    rows = []
    for ln in bl:
        size = max([s.get("size", 10) for s in ln["spans"]] or [10])
        for r in rows:
            ry0, ry1 = r["bbox"][1], r["bbox"][3]
            ov = min(ry1, ln["bbox"][3]) - max(ry0, ln["bbox"][1])
            if ov > 0.6 * min(ry1 - ry0, ln["bbox"][3] - ln["bbox"][1]) and abs(r["bbox"][3] - ln["bbox"][3]) < 0.4 * size:
                r["parts"].append(ln)
                r["bbox"] = (min(r["bbox"][0], ln["bbox"][0]), min(ry0, ln["bbox"][1]),
                             max(r["bbox"][2], ln["bbox"][2]), max(ry1, ln["bbox"][3]))
                break
        else:
            rows.append({"parts": [ln], "bbox": tuple(ln["bbox"])})
    if all(len(r["parts"]) == 1 for r in rows):
        return bl                                      # обычный PDF: порядок и строки без изменений
    out = []
    for r in rows:
        parts = sorted(r["parts"], key=lambda l: l["bbox"][0])
        size = max(s.get("size", 10) for p in parts for s in p["spans"]) if parts[0]["spans"] else 10
        chunks, right = [parts[0]], parts[0]["bbox"][2]
        for p in parts[1:]:
            if p["bbox"][0] - right > 0.15 * size:    # не вплотную: другая колонка или ячейка, не склеиваем
                out.append(_join(chunks)); chunks = [p]; right = p["bbox"][2]
            else:
                chunks.append(p); right = max(right, p["bbox"][2])
        out.append(_join(chunks))
    order = {id(l): i for i, l in enumerate(bl)}
    out.sort(key=lambda l: min([order.get(id(p), 10 ** 6) for p in l.get("_src", [l])]))
    return [{k: v for k, v in l.items() if k != "_src"} for l in out]


def _join(parts):
    """Строка из частей по символам: сортировка по x, пробел-заглушка под чужим символом удаляется."""
    if len(parts) == 1:
        return parts[0]
    src = parts
    chars = []
    for p in parts:
        for sp in p["spans"]:
            for c in sp.get("chars", []):
                chars.append((c["bbox"][0], c["bbox"][2], c["c"], sp.get("size", 10)))
    if not chars:                                      # нет посимвольных данных — простое соединение
        txt = "".join(s["text"] for p in parts for s in p["spans"])
        chars_txt = txt
    else:
        chars.sort(key=lambda c: c[0])
        solid = [c for c in chars if not c[2].isspace()]
        keep = []
        for c in chars:
            if c[2].isspace() and any(o[0] < c[1] - 0.3 and o[1] > c[0] + 0.3 for o in solid):
                continue
            if c[2].isspace() and keep and keep[-1][2].isspace() and c[0] < keep[-1][1] + 0.5:
                continue                               # два пробела-заглушки в одном месте
            w = max(0.1, c[1] - c[0])
            if any(k[2] == c[2] and min(k[1], c[1]) - max(k[0], c[0]) > 0.5 * w for k in keep[-4:]):
                continue                               # тот же символ поверх (имитация жирного, тень)
            keep.append(c)
        out, prev = [], None
        for c in keep:
            if prev is not None and not c[2].isspace() and not prev[2].isspace() and c[0] - prev[1] > 0.25 * c[3]:
                out.append(" ")
            out.append(c[2])
            prev = c
        chars_txt = "".join(out)
    x0 = min(p["bbox"][0] for p in parts); x1 = max(p["bbox"][2] for p in parts)
    y0 = min(p["bbox"][1] for p in parts); y1 = max(p["bbox"][3] for p in parts)
    size = max([s.get("size", 10) for p in parts for s in p["spans"]] or [10])
    return {"bbox": (x0, y0, x1, y1), "spans": [{"text": chars_txt, "size": size}], "_src": src}


for pno, page in enumerate(doc, 1):
    h, w = page.rect.height, page.rect.width
    d = page.get_text("rawdict")
    for _b in d["blocks"]:                             # rawdict: текст span собирается из символов
        for _l in _b.get("lines", []):
            for _s in _l["spans"]:
                _s["text"] = "".join(c["c"] for c in _s.get("chars", []))
    head, foot, lines = [], [], []
    for bi, b in enumerate(d["blocks"]):
        if b.get("type", 0) != 0: continue
        bl = merge_baseline(b.get("lines", []))
        for li, ln in enumerate(bl):
            x0, y0, x1, y1 = ln["bbox"]
            txt = "".join(s["text"] for s in ln["spans"]).rstrip()
            if not txt.strip(): continue
            if y1 < h * 0.08: head.append(txt); continue
            if y0 > h * 0.92: foot.append(txt); continue
            lines.append({"block": bi, "line": li, "y": round(y0, 1), "text": txt,
                          "last": li == len(bl) - 1})
    # Нумерация всегда физическая: номер = порядковый номер страницы в файле.
    # Печатные номера из колонтитулов сохраняем отдельно и используем только для сверки.
    printed = None
    for t in head + foot:
        m = re.fullmatch(r"\s*(\d{1,3})\s*", t)
        if m: printed = int(m.group(1)); break
    clause = None
    for ln in lines:
        m = CLAUSE.match(ln["text"])
        if m: clause = m.group(1); break
    pages.append({"pdf_page": pno, "page": pno, "printed": printed, "clause_from": clause,
                  "head": head, "foot": foot, "lines": lines})

    text = page.get_text()
    for t in re.findall(r"\S+", text):
        core = t.strip('.,;:()[]«»""\u201c\u201d\u2033\u2032')
        if not core: continue
        kk = set(kind(c) for c in core)
        if "c" in kk and "l" in kk:
            mixed.setdefault(core, []).append(pno)
    for m in re.finditer(r"\d[\d.,]*[ \u00a0]?(?:мм|см|мкм|МПа|бар|кг|м\u00b3|°С|°C|кВт|шт)\b|\d[\d.,]*[ \u00a0]?м\b", text):
        if not re.match(r"^\d[\d.,]*[ \u00a0]", m.group(0)):
            nogap.append({"page": pno, "text": m.group(0),
                          "ctx": text[max(0, m.start()-22):m.end()+12].replace("\n", " ")})
    for m in re.finditer(r"\d\s*[\u0445\u0425]\s*\d", text):
        cyr_times.append({"page": pno, "text": m.group(0)})
    # одиночная латинская буква в русском тексте: предлог «c» латиницей, «ГОСТ P»
    for m in re.finditer(r"(?<![\w-])[aceopxy]\s+[\u0430-\u044f\u0451]{2,}", text):
        lat_single.append({"page": pno, "text": m.group(0)})
    for m in re.finditer(r"\b(?:ГОСТ|ОСТ)\s+P\b", text):
        lat_single.append({"page": pno, "text": m.group(0)})
    for cat, rx in TYPO_RX:
        for m in re.finditer(rx, text):
            if cat == "дефис в диапазоне":
                before = text[max(0, m.start() - 14):m.start()]
                a, b = (float(x.replace(",", ".")) for x in m.groups())
                if a >= b or STD.search(before) or re.search(r"[A-Za-z]\s*$", before):
                    continue                     # номер стандарта, год, код изделия
            TYPO[cat].append({"page": pno, "text": m.group(0).strip()})

for p in pages:
    blocks = {}
    for ln in p["lines"]:
        blocks.setdefault(ln["block"], []).append(ln)
    for bi, rows in blocks.items():
        if len(rows) < 2: continue
        for ln in rows:
            if ln["last"] or ln["text"].rstrip().endswith("-"): continue
            w = ln["text"].rstrip().strip('.,;:!?»""\u201c\u201d').split()
            if not w: continue
            if w[-1].lower() in STOP:
                dangling.append({"page": p["pdf_page"], "block": bi, "tail": " ".join(w[-4:])})

# --- таблицы: дубли параметров и разрывы диапазонов (п. 3.4.2) ---
# Строки таблицы собираются из слов с одинаковой вертикальной координатой.
table_issues = []
NUM = re.compile(r"^\d{1,3}(?:[ \u00a0]?\d{3})*(?:,\d+)?$|^\d+(?:,\d+)?$")
def tofloat(x):
    return float(x.replace(" ", "").replace("\u00a0", "").replace(",", "."))
for pno, page in enumerate(doc, 1):
    rows = {}
    for x0, y0, x1, y1, wtxt, *_ in page.get_text("words"):
        rows.setdefault(round(y0 / 3), []).append((x0, wtxt))
    lines_ = [[w for _, w in sorted(v)] for _, v in sorted(rows.items())]
    # 1) параметр повторен с другими значениями
    seen = {}
    for ws in lines_:
        k = 0
        while k < len(ws) and not re.match(r"^[\d+\-–*]", ws[k]):
            k += 1
        label, tail = " ".join(ws[:k]), ws[k:]
        if (len(label) < 10 or len(tail) < 2 or re.search(r"[,—:\ufffd]$|\ufffd", label)
                or not all(re.fullmatch(r"[+\-–*\d.,/]+", t) for t in tail)):
            continue
        if label in seen and seen[label] != tail:
            table_issues.append({"page": pno, "kind": "параметр повторен с другими значениями", "text": label})
        seen.setdefault(label, tail)
    # 2) ряды «min max»: конец одного ряда должен совпадать с началом следующего
    seq = []
    for ws in lines_:
        m = re.match(r"^(\d{1,2})$", ws[0]) if ws else None
        if m and len(ws) > 2 and ws[1] == "ряд":
            nums = []
            for a_, b_ in zip(ws[2:], ws[3:]):          # первые два соседних числа ряда
                if NUM.match(a_) and NUM.match(b_):
                    nums = [a_, b_]; break
            if nums:
                seq.append((int(ws[0]), nums))
    for (n1, a), (n2, b) in zip(seq, seq[1:]):
        try:
            a0, a1, b0, b1 = tofloat(a[0]), tofloat(a[1]), tofloat(b[0]), tofloat(b[1])
        except ValueError:
            continue
        same = a0 == b0 and a1 == b1                              # ряд повторен целиком
        gap = b1 > a1 and a1 != b0 and abs(a1 - b0) <= 0.5 * a1     # разрыв или нахлест
        if a0 < a1 and b0 < b1 and (same or gap):
            table_issues.append({"page": pno, "kind": "разрыв или повтор диапазона",
                                 "text": f"ряд {n1}: {a[0]}–{a[1]} → ряд {n2}: {b[0]}–{b[1]}"})

# --- содержание и ссылки «стр. N» (п. 3.4.2) ---
toc_issues, page_refs = [], []
heads = {p["pdf_page"]: " ".join(p["head"] + [ln["text"] for ln in p["lines"][:6]]) for p in pages}
for p in pages:
    toc = []
    for ln in p["lines"]:
        m = re.match(r"^\s*(.{4,}?)\s*[.\u2026\ufffd·_ ]{3,}\s*(\d{1,3})\s*$", ln["text"])
        if m:
            toc.append(m)
    titles = [re.sub(r"[^\w()/«» -]+", "", m.group(1)).strip(" .") for m in toc]
    for m in toc:
        if m:
            title = re.sub(r"[^\w()/«» -]+", "", m.group(1)).strip(" .")
            num = int(m.group(2))
            if titles.count(title) > 1:
                continue                      # повторяющийся подзаголовок («Состав блока») не сверяется
            where = [q for q, t in heads.items() if q != p["pdf_page"] and title.lower()[:25] in t.lower()]
            if num > doc.page_count:
                toc_issues.append({"page": p["pdf_page"], "text": title, "printed": num, "found": where,
                                   "kind": "страницы нет в файле"})
            elif where and num not in where:
                toc_issues.append({"page": p["pdf_page"], "text": title, "printed": num, "found": where,
                                   "kind": "заголовок найден на других страницах"})
    body = " ".join(ln["text"] for ln in p["lines"])
    for m in re.finditer(r"(?:см\.|на|в|по)\s+стр\.\s*(\d{1,3})", body):
        page_refs.append({"page": p["pdf_page"], "text": m.group(0), "target": int(m.group(1)),
                          "exists": int(m.group(1)) <= doc.page_count})

# --- колонтитулы: все варианты с диапазонами страниц, для просмотра глазами ---
running = {}
for p in pages:
    for t in p["head"] + p["foot"]:
        t = t.strip()
        if t and not re.fullmatch(r"\d{1,3}", t) and len(t) > 6:
            running.setdefault(t, []).append(p["pdf_page"])

# --- пустые страницы и серые заглушки вместо иллюстраций ---
empty_pages, placeholders = [], []
for pno, page in enumerate(doc, 1):
    txt = page.get_text().strip()
    imgs = page.get_images()
    if len(re.sub(r"\d", "", txt)) < 3 and not imgs and not page.get_drawings():
        empty_pages.append(pno)
    area = page.rect.width * page.rect.height
    img_rects = []
    for im in imgs:
        try:
            img_rects += page.get_image_rects(im[0])
        except Exception:
            pass
    for dr in page.get_drawings():
        f = dr.get("fill")
        r = dr.get("rect")
        if not f or not r or r.width * r.height < area * 0.08:
            continue
        if not all(it[0] == "re" for it in dr.get("items", [])):   # заглушка — простой прямоугольник
            continue
        if max(f) - min(f) < 0.03 and 0.75 < f[0] < 0.97:          # однотонный светло-серый
            words_in = [w for w in page.get_text("words") if pymupdf.Rect(w[:4]).intersects(r)]
            strokes = [x for x in page.get_drawings() if x.get("rect") and x["rect"] != r
                       and r.contains(x["rect"])]
            if not any(r.intersects(ir) for ir in img_rects) and not words_in and len(strokes) < 5:
                placeholders.append({"page": pno, "rect": [round(v) for v in r]})
if PNG:
    os.makedirs(PNG, exist_ok=True)
    for pno, page in enumerate(doc, 1):
        page.get_pixmap(dpi=72).save(os.path.join(PNG, f"p{pno:03d}.png"))

out = {"file": PATH, "pages_count": doc.page_count,
       "pages_meta": [{"pdf_page": p["pdf_page"], "page": p["pdf_page"], "printed": p["printed"], "clause_from": p["clause_from"]} for p in pages],
       "lines": pages, "mixed_alphabet_tokens": mixed, "number_unit_no_gap": nogap,
       "dangling_prepositions": dangling, "cyrillic_times_sign": cyr_times,
       "latin_single_letters": lat_single, "typography": TYPO,
       "table_issues": table_issues, "toc_issues": toc_issues, "page_refs": page_refs,
       "running_titles": running, "empty_pages": empty_pages, "placeholders": placeholders}
json.dump(out, open("dump.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

print("страниц:", doc.page_count)
print("нумерация: физические страницы PDF, отсчет от первой страницы файла")
print("печатные номера в колонтитулах (только для сверки):", [p["printed"] for p in pages])
print("\nСМЕШЕНИЕ АЛФАВИТОВ (%d):" % len(mixed))
for t, ps in mixed.items():
    print("  ", repr(t), "стр.", ps, [f"{c}=U+{ord(c):04X}" for c in t if kind(c) in "cl"])
print("\nЧИСЛО БЕЗ ПРОБЕЛА ОТ ЕДИНИЦЫ (%d):" % len(nogap))
for r in nogap[:40]: print("   стр.", r["page"], repr(r["text"]))
print("\nКИРИЛЛИЧЕСКАЯ «х» КАК ЗНАК УМНОЖЕНИЯ (%d):" % len(cyr_times))
for r in cyr_times[:20]: print("   стр.", r["page"], repr(r["text"]))
print("\nОДИНОЧНАЯ ЛАТИНСКАЯ БУКВА В РУССКОМ ТЕКСТЕ (%d):" % len(lat_single))
for r in lat_single: print("   стр.", r["page"], repr(r["text"]))
print("\nВИСЯЧИЕ ПРЕДЛОГИ И СОЮЗЫ (%d):" % len(dangling))
for r in dangling: print("   стр.", r["page"], "…", repr(r["tail"]))
print("\nТИПОГРАФИКА ПО КАТЕГОРИЯМ (разбирать категорию целиком, п. 3.4.2):")
for cat, items in TYPO.items():
    if items:
        print(f"   {cat} ({len(items)}):", ", ".join(sorted({f'стр. {r["page"]} {r["text"]!r}' for r in items})[:8]))
print("\nТАБЛИЦЫ (%d):" % len(table_issues))
for r in table_issues[:30]: print("   стр.", r["page"], r["kind"] + ":", repr(r["text"]))
print("\nСОДЕРЖАНИЕ (%d):" % len(toc_issues))
for r in toc_issues: print("   стр.", r["page"], repr(r["text"]), "→ указано", r["printed"], "| найдено на", r["found"], "|", r["kind"])
print("\nССЫЛКИ «стр. N» (%d):" % len(page_refs))
for r in page_refs: print("   стр.", r["page"], repr(r["text"]), "" if r["exists"] else "← СТРАНИЦЫ НЕТ")
print("\nКОЛОНТИТУЛЫ (проверить, что соответствуют разделу):")
for t, ps in running.items(): print(f"   {ps[0]}–{ps[-1]} ({len(ps)} стр.): {t!r}")
print("\nПУСТЫЕ СТРАНИЦЫ:", empty_pages or "нет")
print("СЕРЫЕ ЗАГЛУШКИ ВМЕСТО ИЛЛЮСТРАЦИЙ:", sorted({r["page"] for r in placeholders}) or "нет")
if PNG: print("Изображения страниц:", PNG)
print("\nПолные данные: dump.json")
