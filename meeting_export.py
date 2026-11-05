"""Deterministic meeting pack from saved records; no model calls."""
import io
import json
import re
from docx import Document
from docx.shared import Cm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def clean(value):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', '', str(value or ''))


DRAFT_LABEL = 'ПРОЕКТ ДЛЯ ПРОВЕРКИ ЭКСПЕРТОМ'
ANALYSIS_HEADINGS = {
    'Проект справки', '1. Суть проблемы', '2. Предложение автора',
    '3. Обоснование в исходном тексте',
    '4. Недостающие сведения и вопросы автору',
    '5. Вопросы для обсуждения советом',
}


def saved_source(ai):
    """Use exact saved input, never substitute today's proposal text."""
    if (ai.get('analysis_source') or '').strip():
        return ai['analysis_source'], False
    # Legacy text-only analysis explicitly identified its input scope.
    try:
        attachments = json.loads(ai.get('attachments') or '[]')
    except (TypeError, ValueError):
        return None, False
    if (attachments == [] and (ai.get('source') or '').strip()
            and 'Источник: только текст заявки. Вложения и внешние источники не изучались.'
            in (ai.get('result') or '')):
        return ai['source'], True
    return None, False


def build_meeting(db, ids, statuses):
    if not ids or len(ids) > 10 or len(set(ids)) != len(ids):
        raise ValueError('Укажите от 1 до 10 разных номеров: /meeting 1 2')
    records = []
    for pid in ids:
        row = db.execute("SELECT * FROM proposals WHERE id=? AND status!='draft'", (pid,)).fetchone()
        if not row:
            raise ValueError(f'Заявка №{pid} не найдена или ещё не отправлена. Пакет не создан.')
        ai = db.execute("SELECT * FROM ai_jobs WHERE proposal_id=? AND status IN ('done','partial') ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        files = db.execute('SELECT name FROM files WHERE proposal_id=? ORDER BY id', (pid,)).fetchall()
        records.append((dict(row), dict(ai) if ai else None, [r['name'] for r in files]))
    if sum(len(str(r)) for r in records) > 300000:
        raise ValueError('Слишком большой объём материалов. Разделите заявки на несколько пакетов.')
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2)
    for name in ('Normal', 'Title', 'Heading 1', 'Heading 2'):
        style = doc.styles[name]
        style.font.name = 'Times New Roman'
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.size = Pt(12 if name == 'Normal' else 16 if name in ('Title','Heading 1') else 13)
        style.paragraph_format.space_after = Pt(6)
        for border in style.element.xpath('./w:pPr/w:pBdr'):
            border.getparent().remove(border)
        fonts = style.element.get_or_add_rPr().find(qn('w:rFonts'))
        if fonts is not None:
            for key in list(fonts.attrib):
                if key.endswith('Theme'):
                    del fonts.attrib[key]
    for name in ('Title', 'Heading 1', 'Heading 2'):
        doc.styles[name].paragraph_format.keep_with_next = True
        doc.styles[name].paragraph_format.keep_together = True
    doc.styles['Normal'].paragraph_format.line_spacing = 1.15
    doc.styles['Normal'].paragraph_format.widow_control = True
    def para(text):
        for line in clean(text).splitlines():
            doc.add_paragraph(line)
    doc.add_paragraph('Материалы к заседанию Экспертного совета по науке и научно-технологическому развитию', 'Title')
    para('При Комитете по цифровизации, инновациям и науке Құрылтая Республики Казахстан')
    para('ПРОЕКТ ДЛЯ РАССМОТРЕНИЯ')
    para('Дата заседания: ____________________\nМесто проведения: ____________________')
    para('Материалы подготовлены для обсуждения выбранных предложений. Повестка и ИИ-справки требуют проверки перед заседанием.')
    doc.add_heading('Проект повестки', 1)
    table = doc.add_table(rows=1, cols=4)
    table.autofit = False
    widths = [0.9, 8, 3.1, 4.5]
    for col, width in zip(table.columns, widths):
        col.width = Cm(width)
    for cell, text in zip(table.rows[0].cells, ['№', 'Вопрос', 'Основание', 'Докладчик']):
        cell.text = text
    header = OxmlElement('w:tblHeader')
    table.rows[0]._tr.get_or_add_trPr().append(header)
    for i, (row, ai, files) in enumerate(records, 1):
        title = row['topic'] or f'Рассмотрение предложения №{row["id"]}'
        for cell, value in zip(table.add_row().cells, [str(i), title, f'Заявка №{row["id"]}', 'Не определён']):
            cell.text = clean(value)
    for index, tr in enumerate(table.rows):
        for cell, width in zip(tr.cells, widths):
            cell.width = Cm(width)
            props = cell._tc.get_or_add_tcPr()
            borders = OxmlElement('w:tcBorders')
            for side in ('top','left','bottom','right'):
                edge = OxmlElement('w:' + side)
                for key, value in [('val','single'),('sz','4'),('color','D9D9D9')]:
                    edge.set(qn('w:' + key), value)
                borders.append(edge)
            props.append(borders)
            if index == 0:
                shade = OxmlElement('w:shd'); shade.set(qn('w:fill'), 'EDEDED'); props.append(shade)
            for p in cell.paragraphs:
                p.paragraph_format.space_before = Pt(5)
                p.paragraph_format.space_after = Pt(5)
    for i, (row, ai, files) in enumerate(records, 1):
        doc.add_page_break()
        doc.add_heading(f'Вопрос {i}   Предложение №{row["id"]}', 1)
        if row['topic']:
            para(row['topic'])
        para(f'Автор — имя в Telegram: {row["author"]}\nДата создания записи UTC: {row["created"]}\nСтатус: {statuses.get(row["status"], row["status"])}\nОтветственный: {row["assignee"] or "Не назначен"}\nСрок рассмотрения: {row["deadline"] or "Не установлен"}')
        doc.add_heading('Комментарий администратора', 2)
        para(row['comment'] or 'Не внесён')
        doc.add_heading('Позиция эксперта', 2)
        para('Не представлена')
        doc.add_heading('Зафиксированный итог', 2)
        para(row['outcome'] or 'Не зафиксирован')
        doc.add_heading('Исходный текст предложения', 2)
        para(row['body'])
        doc.add_heading('Приложенные файлы', 2)
        para('\n'.join(files) if files else 'Нет приложенных файлов')
        if files:
            para('В пакет включён перечень файлов. Оригиналы доступны в карточке заявки в боте.')
        doc.add_heading('Сохранённая ИИ справка', 2)
        if ai:
            para(f'ПРОЕКТ ДЛЯ ПРОВЕРКИ ЭКСПЕРТОМ\nДата анализа UTC: {ai["created"]}')
            if ai['status'] == 'partial':
                para('Анализ выполнен частично. Учитывайте ограничения, указанные в справке.')
            for line in clean(ai['result'] or 'Текст справки отсутствует').splitlines():
                line = line.strip()
                plain = re.sub(r'^#{1,6} ', '', line).replace('**', '').strip()
                if not plain or plain == DRAFT_LABEL:
                    continue
                if re.fullmatch(r'Абзацы \[А1\] и далее: /sources [0-9]+', plain):
                    continue
                if re.match(r'^#{1,6} ', line) or plain in ANALYSIS_HEADINGS:
                    doc.add_heading(plain, 2)
                else:
                    paragraph = doc.add_paragraph()
                    for n, part in enumerate(line.split('**')):
                        paragraph.add_run(part).bold = bool(n % 2)
            doc.add_heading('Источники сохранённого анализа', 2)
            source, restored = saved_source(ai)
            if restored:
                para('Источник восстановлен из сохранённого текста, переданного на этот анализ. Вложения в нём не анализировались.')
            para('\n'.join(f'[А{n}] {line}' for n, line in enumerate((p.strip() for p in source.splitlines() if p.strip()), 1)) if source else 'Сохранённый текст источников отсутствует. Ссылки в справке требуют проверки.')
        else:
            para('Анализ ещё не выполнен. Для подготовки справки используйте команду /analyze с номером заявки, затем соберите пакет заново.')
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()
