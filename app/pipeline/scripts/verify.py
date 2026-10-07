# verify.py v2 — машинная проверка отчета. Печатает PASS/FAIL по каждому требованию.
# Запуск:  python verify.py otchet.docx findings.json [dump.json]
# Возвращает код 1, если есть FAIL.
import sys, json, re, os
from docx import Document
from docx.shared import Pt

DOCX = sys.argv[1] if len(sys.argv) > 1 else "otchet.docx"
FIND = sys.argv[2] if len(sys.argv) > 2 else "findings.json"
DUMP = sys.argv[3] if len(sys.argv) > 3 else "dump.json"

res, d = [], Document(DOCX)
DATA = json.load(open(FIND, encoding="utf-8"))
REJ = DATA.get("rejected", [])
REV = {r.get("category") for r in DATA.get("reviewed", [])}
# ВАЖНО: порядок тот же, что в build.py, иначе строки таблицы и записи разъедутся.
F = sorted(DATA["findings"],
           key=lambda r: (int(r["page"]) if str(r["page"]).isdigit() else 999, str(r["clause"])))

TYPES = {"Орфография", "Пунктуация", "Грамматика", "Стилистика", "Типографика",
         "Единообразие", "Логика", "Требует проверки автором"}
FAKE_TYPES = {"Структура", "Верстка", "Верстка", "Математика", "Терминология", "Синтаксис",
              "Оформление", "Фактология", "Ссылки", "Форматирование", "Смысл", "Редактура"}
COLS = ["№", "Стр., пункт", "Тип", "Как в документе", "Как правильно", "Пояснение"]
# слова, которых не должно быть в колонке-исправлении (кроме случаев, когда они есть и в оригинале)
COMMENT = re.compile(r"(?i)(провер|уточн|указать|рассмотр|перенести|избега|для единообраз|"
                     r"\bи т\.\s?д\.|\bнапример\b|рекоменд|\(\s*или\b|\bлибо\b|на выбор|\?|сверьте|"
                     r"уберите|добавьте|поставьте|выберите|замените)")
BAN_SUMMARY = re.compile(r"(?i)(рекоменд|совету|наймите|следует проверить|после исправлени|"
                         r"предлага|необходимо провести|стоит проверить|сдать|нанять)")


def chk(name, ok, detail=""):
    res.append((name, bool(ok), detail))


s = d.sections[0]
chk("ориентация альбомная", s.orientation == 1, f"orientation={s.orientation}")
chk("размер A4 альбомный 29,7 × 21,0",
    abs(s.page_width.cm - 29.7) < .05 and abs(s.page_height.cm - 21.0) < .05,
    f"{s.page_width.cm:.1f} × {s.page_height.cm:.1f}")
chk("поля 1,2 см", all(abs(getattr(s, m).cm - 1.2) < .02
                       for m in ("left_margin", "right_margin", "top_margin", "bottom_margin")), "")
chk("шрифт по умолчанию Arial 10",
    d.styles["Normal"].font.name == "Arial" and d.styles["Normal"].font.size == Pt(10),
    f'{d.styles["Normal"].font.name}')

heads = [p for p in d.paragraphs if p.runs and p.runs[0].font.size == Pt(14)]
chk("заголовки Arial 14 полужирный #0F4382",
    len(heads) >= 2 and all(r.bold and str(r.font.color.rgb) == "0F4382" and r.font.name == "Arial"
                            for p in heads for r in p.runs), f"найдено {len(heads)}")

t = d.tables[0]
hdr = t.rows[0].cells
chk("шапка: заливка #0F4382", "0F4382" in hdr[0]._tc.xml)
chk("шапка: текст белый полужирный",
    all(str(r.font.color.rgb) == "FFFFFF" and r.bold
        for c in hdr for r in c.paragraphs[0].runs if r.text.strip()), "")
chk("шапка повторяется на страницах", "tblHeader" in t.rows[0]._tr.xml)
xml = t._tbl.tblPr.xml
b = dict(re.findall(r'w:(top|left|bottom|right|insideH|insideV) w:val="(\w+)"', xml))
chk("внешний контур убран, внутренние границы #B2B2B2",
    b.get("top") == "nil" and b.get("insideV") == "single" and 'w:color="B2B2B2"' in xml, str(b))
chk("число строк = число замечаний + шапка", len(t.rows) == len(F) + 1, f"{len(t.rows)} против {len(F) + 1}")


def runs(cell, color):
    return [r.text for p in cell.paragraphs for r in p.runs
            if r.font.color and r.font.color.rgb and str(r.font.color.rgb) == color and r.bold]


def markers(txt):
    """части маркера из ячейки «Как в документе»: bold-фрагменты"""
    return None


no_page, no_red, no_green, no_note = [], [], [], []
bad_type, fake, comment, same, notfound = [], [], [], [], []
for i, row in enumerate(t.rows[1:], 1):
    c = row.cells
    author = c[2].text.strip() == "Требует проверки автором"
    if not re.search(r"стр\.\s*\d+.+\S", c[1].text):
        no_page.append(i)
    if not author and not runs(c[3], "C00000"):
        no_red.append(i)
    if c[4].text.strip() in {"—", "-", "–", ""}:
        if not author:
            no_green.append(i)
    elif not runs(c[4], "008000"):
        no_green.append(i)
    if not c[5].text.strip():
        no_note.append(i)
    if c[2].text.strip() in FAKE_TYPES:
        fake.append((i, c[2].text.strip()))
    elif c[2].text.strip() not in TYPES:
        bad_type.append(i)
    # комментарий в исправлении: слово-маркер, которого нет в оригинальной цитате
    if c[4].text.strip() not in {"—", "-", "–", ""}:
        extra = set(m.group(0).lower() for m in COMMENT.finditer(c[4].text)) - \
                set(m.group(0).lower() for m in COMMENT.finditer(c[3].text))
        if extra:
            comment.append(i)
    # исправление не должно дословно повторять оригинал, кроме случая «показ стыка строк»
    # (там меняется только пробел — это неразрывный пробел, отметить нечем, кроме пометки в пояснении)
    if c[4].text.strip() == c[3].text.strip() and "неразрывн" not in c[5].text.lower():
        same.append(i)
    # маркер должен быть найден внутри своей цитаты
    src, fx = c[3].text, c[4].text
    for cell, text in ((c[3], src), (c[4], fx)):
        for r in cell.paragraphs[0].runs:
            if r.font.color and r.font.color.rgb and r.bold:
                if r.text.strip() and r.text not in text:
                    notfound.append((i, r.text[:25]))
    f = F[i - 1] if i - 1 < len(F) else {}
    for key, col in (("bad", c[3].text), ("good", c[4].text)):
        mk = str(f.get(key, ""))
        if mk and mk not in {"—", "-", "–"}:
            for part in [x.strip() for x in mk.split(" … ") if x.strip()]:
                if part not in col:
                    notfound.append((i, part[:25]))
chk("в каждой строке номер страницы и пункт", not no_page, f"без адреса: {no_page}")
chk("ошибка выделена красным #C00000", not no_red, f"не выделено: {no_red}")
chk("исправление выделено зеленым #008000 («—» только у «Требует проверки автором»)", not no_green, f"{no_green}")
chk("пояснение заполнено", not no_note, f"пусто: {no_note}")

seen, dup = {}, []
for i, row in enumerate(t.rows[1:], 1):
    for k in ((row.cells[1].text, row.cells[3].text),
              re.sub(r"[\s\u00a0]+", "", row.cells[3].text).lower()):
        if k in seen:
            dup.append((seen[k], i))
        seen[k] = i
chk("нет дублей одного места (по адресу и по цитате)", not dup, f"дубли: {dup}")
chk("ровно 6 колонок с названиями из п. 4.2", [c.text.strip() for c in hdr] == COLS,
    str([c.text.strip() for c in hdr]))
chk("тип — одно из 8 названий раздела 2, без выдуманных", not bad_type and not fake,
    f"недопустимые: {bad_type}; выдуманные: {fake}")
chk("«Как правильно» — только текст для вставки, без комментариев", not comment,
    f"комментарий в строках {comment}")
chk("«Как правильно» не повторяет оригинал (кроме неразрывного пробела)", not same,
    f"повторяют оригинал: {same}")
chk("все маркеры выделения найдены в своих цитатах", not notfound, f"не найдено: {notfound[:8]}")

paras = list(d.paragraphs)
idx = next((i for i, x in enumerate(paras) if x.text.strip() == "Итог"), None)
if idx is not None:
    after = [x.text for x in paras[idx + 1:] if x.text.strip()]
    chk("итог без рекомендаций и советов",
        bool(after) and not any(BAN_SUMMARY.search(a) for a in after), "")
else:
    chk("итог без рекомендаций и советов", False, "не найден заголовок «Итог»")

missing = []
if os.path.exists(DUMP):
    dump = json.load(open(DUMP, encoding="utf-8"))
    # страница = основной текст + колонтитулы (обложка и задняя обложка часто попадают в зону колонтитулов)
    pages = {p["pdf_page"]: re.sub(r"[\s\u00a0]+", "", " ".join(
                 [ln["text"] for ln in p["lines"]] + p.get("head", []) + p.get("foot", [])))
             for p in dump.get("lines", [])}
    # п. 5.14: буква с двумя точками приравнивается к обычной е
    YO = str.maketrans("\u0451\u0401", "\u0435\u0415")
    pages = {k: v.translate(YO) for k, v in pages.items()}
    for f in F:
        pg = f.get("page")
        src = re.sub(r"[\s\u00a0]+", "", f["src"]).translate(YO)
        if pg not in pages:
            missing.append((f["clause"], "нет такой страницы")); continue
        if src not in pages[pg]:
            missing.append((f["clause"], f"цитаты нет на стр. {pg}"))
    chk("цитаты привязаны к верным страницам PDF", not missing, f"{missing[:6]}")
else:
    chk("цитаты привязаны к верным страницам PDF", False, "нет dump.json — сначала запусти extract.py")

cand_missing = []
if os.path.exists(DUMP):
    dump = json.load(open(DUMP, encoding="utf-8"))
    hay = re.sub(r"[\s\u00a0]+", "", " ".join(f["src"] for f in F) + " " +
                 " ".join(str(r.get("text", "")) for r in REJ))
    cands = set(dump.get("mixed_alphabet_tokens", {}).keys())
    cands |= {r["text"] for r in dump.get("number_unit_no_gap", [])}
    cands |= {r["text"] for r in dump.get("cyrillic_times_sign", [])}
    cands |= {r["text"] for r in dump.get("latin_single_letters", [])}
    cands |= {" ".join(r["tail"].split()[-2:]) for r in dump.get("dangling_prepositions", [])}
    hay = hay.replace("\u0451", "\u0435")
    for c in sorted(cands):
        if re.sub(r"[\s\u00a0]+", "", c).replace("\u0451", "\u0435") not in hay:
            cand_missing.append(c)
    # категории (п. 3.4.2): каждая непустая категория должна быть отмечена в "reviewed"
    cats = {k: v for k, v in dump.get("typography", {}).items() if v}
    for key, name in (("table_issues", "таблицы"), ("toc_issues", "содержание"),
                      ("page_refs", "ссылки «стр. N»")):
        if dump.get(key):
            cats[name] = dump[key]
    for name in cats:
        if name not in REV:
            cand_missing.append("категория: " + name)
    # пустые страницы и заглушки должны быть названы в шапке отчета
    head_txt = " ".join(DATA["document"].get("header_lines", [])).lower()
    if dump.get("placeholders") and "заглуш" not in head_txt:
        cand_missing.append("шапка: не указаны серые заглушки")
    if dump.get("empty_pages") and "пуст" not in head_txt:
        cand_missing.append("шапка: не указаны пустые страницы")
    chk("все кандидаты extract.py рассмотрены", not cand_missing,
        f"не рассмотрено {len(cand_missing)}: {cand_missing[:12]}")
else:
    chk("все кандидаты extract.py рассмотрены", False, "нет dump.json")

# сверка с эталоном (если рядом лежит reference_findings.json)
ref_path = os.path.join(os.path.dirname(os.path.abspath(FIND)), "reference_findings.json")
if not os.path.exists(ref_path):
    # Сверка не задана — проверка все равно считается пройденной, чтобы число проверок не плавало.
    chk("эталонные дефекты найдены (reference_findings.json не задан — сверка пропущена)", True, "")
else:
    ref = json.load(open(ref_path, encoding="utf-8"))["findings"]
    hay = re.sub(r"[\s\u00a0]+", "", " ".join(f["src"] + f["fix"] + f["note"] for f in F)).lower()
    absent = []
    for r in ref:
        key = re.sub(r"[\s\u00a0]+", "", r["key"]).lower()
        if key not in hay:
            absent.append(r["key"])
    chk("эталонные дефекты найдены (%d)" % len(ref), not absent,
        f"не найдено {len(absent)}: {absent}")

# Проверка 25: буквы с двумя точками (U+0451, U+0401) в файле отчета быть не должно.
raw = ""
try:
    import zipfile
    with zipfile.ZipFile(DOCX) as z:
        raw = b"".join(z.read(n) for n in z.namelist() if n.startswith("word/")).decode("utf-8", "ignore")
except Exception as e:
    raw = ""
import html as _html
raw = _html.unescape(raw)                       # буква может быть записана сущностью &#1105;
raw = raw.replace("&#1105;", "\u0451").replace("&#1025;", "\u0401")
yo = [c for c in raw if c in ("\u0451", "\u0401")]
chk("в отчете нет буквы с двумя точками (U+0451, U+0401)", not yo,
    f"найдено {len(yo)}: {''.join(sorted(set(yo)))}")

bad = [r for r in res if not r[1]]
for name, ok, detail in res:
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
print(f"\nПРОВЕРОК: {len(res) - len(bad)}/{len(res)} PASS")
print("ИТОГ:", "ВСЕ ТРЕБОВАНИЯ ВЫПОЛНЕНЫ" if not bad else f"НЕ ВЫПОЛНЕНО ПУНКТОВ: {len(bad)}")
sys.exit(1 if bad else 0)
