"""Выгрузка результатов в .docx: тест, разбор или ответ можно распечатать или выложить в Сферум.

Документ оформлен в стиле «Вектор Знаний»: эмблема и название в верхнем колонтитуле,
ссылка на бота и номера страниц в нижнем. Тест вёрстается как бланк для ученика
(строка для имени, вопросы, варианты), ключ ответов — таблицей на отдельной странице.
Ответ нейросети приходит в Markdown: заголовки, **жирный** и списки переносятся в Word.
"""
import datetime
import io
import os
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.png")
BRAND = RGBColor(0x3B, 0x5B, 0xDB)
GREY = RGBColor(0x86, 0x8E, 0x96)
DARK = RGBColor(0x21, 0x25, 0x29)
BOT_LINK = "max.ru/ideya_fiks_24_bot"

QUESTION = re.compile(r"^\s*(?:\*\*)?(?:Вопрос\s*)?(\d+)[.):]\s*(?:\*\*)?\s*(.+)$")
OPTION = re.compile(r"^\s*[-•*]?\s*([а-гА-Гa-dA-D])\)\s*(.+)$")
KEY_TITLE = re.compile(r"^\s*(?:#+\s*)?(?:\*\*)?\s*Ключ(?:и)? ответ", re.I)
KEY_LINE = re.compile(r"^\s*(\d+)\s*[—–-]\s*([а-гА-Гa-dA-D])\)?\s*[:.]?\s*(.*)$")
HEADING = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")
BULLET = re.compile(r"^\s*[-*•+]\s+(.+)$")
BOLD = re.compile(r"\*\*(.+?)\*\*")


def _font(run, size=None, bold=None, color=None):
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def _add_rich(paragraph, text, size=None, color=None):
    """Текст с **жирными** фрагментами."""
    pos = 0
    for m in BOLD.finditer(text):
        if m.start() > pos:
            _font(paragraph.add_run(text[pos:m.start()]), size, None, color)
        _font(paragraph.add_run(m.group(1)), size, True, color)
        pos = m.end()
    if pos < len(text):
        _font(paragraph.add_run(text[pos:]), size, None, color)


def _page_field(paragraph, code):
    """Поле Word (номер страницы, число страниц) — пересчитывается при открытии файла.
    Каждая часть поля — отдельный фрагмент со своим шрифтом, как это делает сам Word."""
    for kind in ("begin", "instr", "separate", "result", "end"):
        run = paragraph.add_run()
        _font(run, 8, None, GREY)
        if kind == "instr":
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = f" {code} "
        elif kind == "result":
            el = OxmlElement("w:t")
            el.text = "1"
        else:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), kind)
        run._r.append(el)


def _setup(doc):
    section = doc.sections[0]
    section.top_margin, section.bottom_margin = Cm(2), Cm(1.8)
    section.left_margin, section.right_margin = Cm(2.2), Cm(1.8)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    normal.font.size = Pt(12)
    normal.paragraph_format.space_after = Pt(4)

    header = section.header.paragraphs[0]
    if os.path.exists(LOGO):
        header.add_run().add_picture(LOGO, height=Cm(0.9))
    _font(header.add_run("  Вектор Знаний"), 14, True, BRAND)
    _font(header.add_run("   ИИ-ассистент для учителя и ученика в МАКС"), 9, None, GREY)
    _bottom_border(header)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(footer.add_run(f"Подготовлено в боте «Вектор Знаний» · {BOT_LINK} · стр. "), 8, None, GREY)
    _page_field(footer, "PAGE")
    _font(footer.add_run(" из "), 8, None, GREY)
    _page_field(footer, "NUMPAGES")


def _bottom_border(paragraph):
    p_pr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("val", "single"), ("sz", "8"), ("space", "4"), ("color", "3B5BDB")):
        bottom.set(qn(f"w:{key}"), value)
    borders.append(bottom)
    p_pr.append(borders)


def _title(doc, title):
    title = re.sub(r"^[^\w«(]+", "", title).strip()  # эмодзи в начале — для чата, не для печати
    p = doc.add_paragraph()
    _font(p.add_run(title), 18, True, DARK)
    p.paragraph_format.space_after = Pt(2)
    date = doc.add_paragraph()
    _font(date.add_run(datetime.date.today().strftime("%d.%m.%Y")), 9, None, GREY)


def _student_line(doc):
    p = doc.add_paragraph()
    _font(p.add_run("Фамилия, имя: ______________________________   Класс: ______   Дата: __________"), 11, None, DARK)
    p.paragraph_format.space_after = Pt(12)


def _split_key(lines):
    for i, line in enumerate(lines):
        if KEY_TITLE.match(line):
            return lines[:i], lines[i + 1:]
    return lines, []


def _key_rows(key):
    """Ключ из строки «1 — б (абзац 3); 2 — а» или из строк «1 — а: пояснение»."""
    rows = []
    for part in re.split(r";\s*|\n", key or ""):
        m = KEY_LINE.match(part.replace("**", ""))
        if m:
            rows.append((m.group(1), m.group(2), m.group(3).strip(" ()")))
    return rows


def _key_page(doc, key_text, extra_lines=()):
    p = doc.add_paragraph()
    p.add_run().add_break(WD_BREAK.PAGE)
    _font(p.add_run("Ключ ответов"), 16, True, BRAND)
    rows = _key_rows(key_text)
    if rows:
        table = doc.add_table(rows=1, cols=3)
        table.style = "Light Grid Accent 1"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for cell, text in zip(table.rows[0].cells, ("№", "Ответ", "Пояснение")):
            cell.text = text
        for number, letter, note in rows:
            cells = table.add_row().cells
            cells[0].text, cells[1].text, cells[2].text = number, letter, note
        table.autofit = False
        widths = (Cm(1.3), Cm(2), Cm(13.6))
        for column, width in zip(table.columns, widths):
            column.width = width
        for row in table.rows:
            for cell, width in zip(row.cells, widths):
                cell.width = width
    else:
        for line in (key_text or "").split("\n"):
            if line.strip():
                _add_rich(doc.add_paragraph(), line.strip())
    for line in extra_lines:
        if line.strip():
            _add_rich(doc.add_paragraph(), line.strip())


def _is_test(lines):
    return sum(bool(QUESTION.match(l)) for l in lines) >= 2 and sum(bool(OPTION.match(l)) for l in lines) >= 4


def _test_body(doc, lines):
    # варианты, записанные в одну строку («а) x   б) y»), раскладываем по строкам
    lines = [part for line in lines for part in re.split(r"\s{2,}(?=[а-гa-d]\))", line) if part.strip()]
    for i, line in enumerate(lines):
        q, opt = QUESTION.match(line), OPTION.match(line)
        if opt:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.8)
            p.paragraph_format.space_after = Pt(1)
            # вопрос и его варианты не разрываются между страницами
            p.paragraph_format.keep_with_next = i + 1 < len(lines) and bool(OPTION.match(lines[i + 1]))
            _add_rich(p, f"{opt.group(1).lower()}) {opt.group(2).strip()}")
        elif q:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.keep_with_next = True
            _font(p.add_run(f"{q.group(1)}. "), None, True, BRAND)
            _add_rich(p, q.group(2).replace("**", "").strip())
            for run in p.runs[1:]:
                run.bold = True
        else:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.8)
            _add_rich(p, line.strip())


def _markdown_body(doc, lines):
    for line in lines:
        if not line.strip() or re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            continue
        heading, bullet = HEADING.match(line), BULLET.match(line)
        if heading:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            _font(p.add_run(heading.group(1).replace("**", "")), 13, True, BRAND)
        elif bullet:
            _add_rich(doc.add_paragraph(style="List Bullet"), bullet.group(1))
        else:
            _add_rich(doc.add_paragraph(), line.strip())


def to_docx(title, body, answer_key=None):
    doc = Document()
    props = doc.core_properties
    props.author = props.last_modified_by = "Вектор Знаний"
    props.title = re.sub(r"^[^\w«(]+", "", title).strip()
    props.comments = props.subject = ""
    props.keywords = "Вектор Знаний"
    _setup(doc)
    _title(doc, title)
    lines = (body or "").split("\n")
    main, key_lines = _split_key(lines)
    if _is_test(main):
        _student_line(doc)
        _test_body(doc, main)
        key_text = answer_key or "\n".join(key_lines)
        if key_text.strip():
            _key_page(doc, key_text)
    else:
        _markdown_body(doc, main)
        if answer_key or key_lines:
            _key_page(doc, answer_key or "\n".join(key_lines))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def filename(title):
    name = re.sub(r"[^\w\- ]+", "", title).strip().replace(" ", "_")[:60]
    return f"{name or 'vektor_znaniy'}.docx"
