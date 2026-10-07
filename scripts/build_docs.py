#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сборка документации одним файлом Word: docs/Корректор_документация.docx.

    python3 scripts/build_docs.py            # нужен pandoc (apt install pandoc) и python-docx

Порядок разделов — как в меню «Документация» (DOC_PAGES в app/main.py). Схемы и снимки экрана берутся
из docs/images. PDF-версия собирается печатью страницы /docs/all (см. docs/administration.md, п. 8).
"""
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(DOCS, "Корректор_документация.docx")
NAVY = "0F4382"
PAGES = ["index", "methodology", "usage", "analytics", "trends", "users", "integrations", "standards", "settings", "installation",
         "administration", "api", "faq"]


def slug(text):
    from markdown.extensions.toc import slugify_unicode
    return slugify_unicode(text, "-")


def prepare(name, text):
    """Markdown сервиса → Markdown для pandoc: врезки, ссылки между разделами, пути картинок."""
    # врезки «!!! note "Заголовок"» → цитата с жирным заголовком
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        m = re.match(r'^!!!\s+(\w+)(?:\s+"(.*)")?\s*$', lines[i])
        if m:
            title = m.group(2) or {"note": "Примечание", "warning": "Внимание", "tip": "Совет"}.get(m.group(1), "Примечание")
            body = []
            i += 1
            while i < len(lines) and (lines[i].startswith("    ") or not lines[i].strip()):
                if not lines[i].strip() and (i + 1 >= len(lines) or not lines[i + 1].startswith("    ")):
                    break
                body.append(lines[i][4:])
                i += 1
            out.append(f"> **{title}.** " + " ".join(b.strip() for b in body if b.strip()))
            out.append("")
            continue
        out.append(lines[i])
        i += 1
    text = "\n".join(out)
    # ссылки на другие разделы → внутренние якоря документа
    text = re.sub(r"\]\((\w+)\.md#([^)]+)\)", lambda m: f"](#{m.group(2)})", text)
    text = re.sub(r"\]\((\w+)\.md\)", lambda m: f"](#раздел-{m.group(1)})", text)
    text = re.sub(r"\]\(images/", "](" + os.path.join(DOCS, "images") + "/", text)
    # якорь раздела на заголовке первого уровня
    text = re.sub(r"^# (.+)$", lambda m: f"# {m.group(1)} {{#раздел-{name}}}", text, count=1, flags=re.M)
    # якоря заголовков как в веб-версии (для ссылок вида раздел.md#3-показатели)
    text = re.sub(r"^(#{2,4}) (.+?)\s*$", lambda m: f"{m.group(1)} {m.group(2)} {{#{slug(m.group(2))}}}"
                  if "{#" not in m.group(2) else m.group(0), text, flags=re.M)
    return text


def main():
    version = open(os.path.join(ROOT, "VERSION")).read().strip()
    parts = []
    for n in PAGES:
        parts.append(prepare(n, open(os.path.join(DOCS, n + ".md"), encoding="utf-8").read()))
    br = '\n\n```{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n```\n\n'
    md = br.join(parts)
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "all.md")
    open(src, "w", encoding="utf-8").write(md)
    ref = os.path.join(tmp, "ref.docx")
    subprocess.run(["pandoc", "-o", ref, "--print-default-data-file", "reference.docx"], check=True)
    style_reference(ref)
    raw = os.path.join(tmp, "raw.docx")
    subprocess.run(["pandoc", src, "-f", "markdown+pipe_tables+fenced_code_blocks+header_attributes-implicit_figures",
                    "-t", "docx", "--reference-doc", ref, "--resource-path", DOCS, "-o", raw,
                    "--metadata", "lang=ru-RU"], check=True)
    finish(raw, OUT, version)
    print("Готово:", OUT)


def style_reference(path):
    """Стили документа: Arial, темно-синие заголовки как в отчете и на сайте, поля 2 см."""
    from docx import Document
    from docx.enum.text import WD_LINE_SPACING
    from docx.shared import Cm, Pt, RGBColor
    d = Document(path)
    navy = RGBColor.from_string(NAVY)
    for st in d.styles:
        try:
            f = st.font
        except AttributeError:
            continue
        if st.name in ("Normal", "Body Text", "First Paragraph", "Compact", "Block Text"):
            f.name, f.size = "Arial", Pt(10.5)
            f.color.rgb = RGBColor(0x1D, 0x24, 0x33)
            pf = st.paragraph_format
            pf.space_after = Pt(5)
            pf.line_spacing_rule = WD_LINE_SPACING.MULTIPLE
            pf.line_spacing = 1.12
        sizes = {"Heading 1": 22, "Heading 2": 15, "Heading 3": 12.5, "Heading 4": 11, "Title": 30}
        if st.name in sizes:
            f.name, f.size, f.bold, f.color.rgb = "Arial", Pt(sizes[st.name]), st.name != "Heading 1", navy
            st.paragraph_format.space_before = Pt(16 if st.name != "Heading 4" else 10)
            st.paragraph_format.space_after = Pt(6)
            st.paragraph_format.keep_with_next = True
        if st.name in ("Source Code", "Verbatim Char"):
            f.name, f.size = "Consolas", Pt(9)
        if st.name == "Hyperlink":
            f.color.rgb = RGBColor(0x2B, 0x57, 0x97)
    for s in d.sections:
        s.page_width, s.page_height = Cm(21), Cm(29.7)
        s.left_margin = s.right_margin = Cm(2)
        s.top_margin = s.bottom_margin = Cm(1.8)
    d.save(path)


def finish(src, dst, version):
    """Титул, оглавление, оформление таблиц и картинок, нумерация страниц."""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor
    d = Document(src)
    navy = RGBColor.from_string(NAVY)
    body = d.element.body
    first = body[0]

    def para_before(text="", size=11, bold=False, color=None, space=6, align=None):
        p = d.add_paragraph()
        body.remove(p._p)
        first.addprevious(p._p)
        if text:
            r = p.add_run(text)
            r.font.size, r.font.bold, r.font.name = Pt(size), bold, "Arial"
            if color:
                r.font.color.rgb = color
        p.paragraph_format.space_after = Pt(space)
        if align:
            p.alignment = align
        return p

    # титул
    bar = para_before(space=40)
    pPr = bar._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    top = OxmlElement("w:top")
    for k, v in (("w:val", "single"), ("w:sz", "48"), ("w:space", "1"), ("w:color", NAVY)):
        top.set(qn(k), v)
    bdr.append(top)
    pPr.append(bdr)
    lp = para_before(space=24)
    lp.add_run().add_picture(os.path.join(DOCS, "images", "logo.png"), height=Cm(2.6))
    para_before("КОРРЕКТОР", 44, False, navy, 12)
    para_before("Веб-сервис корректорской вычитки PDF-документов", 18, False, None, 18)
    para_before("Полная документация: методология, работа с сервисом, аналитика и тренды, справочник настроек, "
                "установка, администрирование, REST API, вопросы и неполадки", 12, False, RGBColor(0x6B, 0x72, 0x80), 200)
    para_before(f"Версия {version} · {dt.date.today():%d.%m.%Y} · мастер-промпт корректора v5.7", 11, False,
                RGBColor(0x6B, 0x72, 0x80), 0)
    p = para_before()
    p.add_run().add_break(WD_BREAK.PAGE)
    # оглавление: ссылки на разделы (H1) и подразделы (H2) — работает в любом редакторе без обновления полей
    para_before("Содержание", 22, False, navy, 10)
    heads = []
    for hp in d.paragraphs:
        lvl = {"Heading 1": 1, "Heading 2": 2}.get(hp.style.name)
        bm = hp._p.find(".//" + qn("w:bookmarkStart"))
        prev = hp._p.getprevious()
        while bm is None and prev is not None and prev.tag in (qn("w:bookmarkStart"), qn("w:bookmarkEnd")):
            bm = prev if prev.tag == qn("w:bookmarkStart") else None
            prev = prev.getprevious()
        if lvl and bm is not None and hp.text.strip():
            heads.append((lvl, hp.text.strip(), bm.get(qn("w:name"))))
    for lvl, text, anchor in heads:
        tp = para_before(space=1 if lvl == 2 else 3)
        tp.paragraph_format.left_indent = Cm(0 if lvl == 1 else 0.8)
        if lvl == 1:
            tp.paragraph_format.space_before = Pt(7)
        hl = OxmlElement("w:hyperlink")
        hl.set(qn("w:anchor"), anchor)
        r = OxmlElement("w:r")
        rpr = OxmlElement("w:rPr")
        for tag, val in (("w:rFonts", None), ("w:color", NAVY if lvl == 1 else "1D2433"), ("w:sz", "21" if lvl == 1 else "18")):
            e = OxmlElement(tag)
            if tag == "w:rFonts":
                e.set(qn("w:ascii"), "Arial")
                e.set(qn("w:hAnsi"), "Arial")
            else:
                e.set(qn("w:val"), val)
            rpr.append(e)
        if lvl == 1:
            rpr.append(OxmlElement("w:b"))
        r.append(rpr)
        t = OxmlElement("w:t")
        t.text = text
        t.set(qn("xml:space"), "preserve")
        r.append(t)
        hl.append(r)
        tp._p.append(hl)
    p = para_before()
    p.add_run().add_break(WD_BREAK.PAGE)

    # таблицы: шапка темно-синяя, тонкие серые линии, перенос шапки
    for t in d.tables:
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        tblPr = t._tbl.tblPr
        borders = OxmlElement("w:tblBorders")
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            e = OxmlElement(f"w:{side}")
            e.set(qn("w:val"), "single")
            e.set(qn("w:sz"), "4")
            e.set(qn("w:color"), "C9D1DB")
            borders.append(e)
        for old in tblPr.findall(qn("w:tblBorders")):
            tblPr.remove(old)
        tblPr.append(borders)
        w = OxmlElement("w:tblW")
        w.set(qn("w:w"), "5000")
        w.set(qn("w:type"), "pct")
        for old in tblPr.findall(qn("w:tblW")):
            tblPr.remove(old)
        tblPr.append(w)
        for ri, row in enumerate(t.rows):
            if ri == 0:
                trPr = row._tr.get_or_add_trPr()
                h = OxmlElement("w:tblHeader")
                trPr.append(h)
            for cell in row.cells:
                if ri == 0:
                    tcPr = cell._tc.get_or_add_tcPr()
                    shd = OxmlElement("w:shd")
                    shd.set(qn("w:val"), "clear")
                    shd.set(qn("w:color"), "auto")
                    shd.set(qn("w:fill"), NAVY)
                    tcPr.append(shd)
                for cp in cell.paragraphs:
                    cp.paragraph_format.space_after = Pt(2)
                    for run in cp.runs:
                        run.font.size = Pt(9)
                        if ri == 0:
                            run.font.bold = True
                            run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)

    # картинки: не шире поля набора, по центру, с подписью из alt-текста
    from docx.shared import Emu
    maxw = Cm(17)
    for shape in d.inline_shapes:
        if shape.width > maxw:
            ratio = maxw / shape.width
            shape.width = Emu(int(shape.width * ratio))
            shape.height = Emu(int(shape.height * ratio))
        if shape.height > Cm(21):
            ratio = Cm(21) / shape.height
            shape.width = Emu(int(shape.width * ratio))
            shape.height = Emu(int(shape.height * ratio))
    for p in d.paragraphs:
        if p._p.findall(".//" + qn("w:drawing")):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.keep_with_next = True
            docpr = p._p.find(".//" + qn("wp:docPr"))
            alt = docpr.get("descr") if docpr is not None else ""
            if alt:
                cap = d.add_paragraph()
                p._p.addnext(cap._p)
                r = cap.add_run(alt)
                r.font.size, r.font.italic, r.font.name = Pt(9), True, "Arial"
                r.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
                cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                cap.paragraph_format.space_after = Pt(10)

    # колонтитул: название и номер страницы
    for s in d.sections:
        fp = s.footer.paragraphs[0] if s.footer.paragraphs else s.footer.add_paragraph()
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = fp.add_run(f"Корректор {version} — документация · стр. ")
        r.font.size, r.font.name = Pt(8), "Arial"
        r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        r2 = fp.add_run()
        r2.font.size = Pt(8)
        for tag, txt in (("begin", None), (None, "PAGE"), ("end", None)):
            if tag:
                fc = OxmlElement("w:fldChar")
                fc.set(qn("w:fldCharType"), tag)
                r2._r.append(fc)
            else:
                it = OxmlElement("w:instrText")
                it.text = txt
                r2._r.append(it)
    d.core_properties.title = "Корректор — документация"
    d.core_properties.subject = f"Версия {version}"
    d.core_properties.author = "Корректор"
    d.save(dst)


if __name__ == "__main__":
    sys.exit(main())
