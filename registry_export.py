"""XLSX registry snapshot. User-provided values are always literal strings."""
import io
import re
from datetime import datetime, timedelta, timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment


def build_registry(db, statuses):
    rows = db.execute("SELECT * FROM proposals WHERE status!='draft' ORDER BY id").fetchall()
    if len(rows) > 5000:
        raise ValueError('В реестре больше 5000 заявок. Нужна выгрузка по периодам; файл не создан.')
    wb = Workbook()
    ws = wb.active
    ws.title = 'Реестр предложений'
    headers = ['№ заявки', 'Создана (UTC+5)', 'Имя в Telegram', 'Статус', 'Текст предложения', 'Файлов', 'Имена файлов', 'Последний ИИ-анализ', 'Дата анализа (UTC+5)', 'Тема', 'Ответственный эксперт', 'Срок', 'Комментарий эксперта (внесён администратором)', 'Результат рассмотрения']
    ws.append(headers)
    ai_states = {'pending': 'В очереди', 'running': 'Выполняется', 'done': 'Готов (проект)', 'partial': 'Частичный (проект)', 'failed': 'Ошибка'}
    def local(value):
        return (datetime.fromisoformat(value) + timedelta(hours=5)).strftime('%d.%m.%Y %H:%M') if value else ''
    for row in rows:
        files = db.execute('SELECT name FROM files WHERE proposal_id=? ORDER BY id', (row['id'],)).fetchall()
        ai = db.execute('SELECT status,created FROM ai_jobs WHERE proposal_id=? ORDER BY id DESC LIMIT 1', (row['id'],)).fetchone()
        values = [row['id'], local(row['created']), row['author'], statuses[row['status']], row['body'], len(files), '\n'.join(f['name'] for f in files), ai_states.get(ai['status'], ai['status']) if ai else 'Не запускался', local(ai['created']) if ai else '', row['topic'], row['assignee'], datetime.strptime(row['deadline'], '%Y-%m-%d').strftime('%d.%m.%Y') if row['deadline'] else '', row['comment'], row['outcome']]
        ws.append(values)
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str):
                value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', cell.value)
                cell.value = value[:32740] + ('\n[Сокращено в Excel]' if len(value) > 32740 else '')
                cell.data_type = 's'  # Never interpret =, +, -, @ as spreadsheet formulas.
            cell.alignment = Alignment(vertical='top', wrap_text=True)
        ws.row_dimensions[ws.max_row].height = 60
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='17365D')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    ws.row_dimensions[1].height = 32
    for col, width in zip('ABCDEFGHIJKLMN', [12, 22, 28, 22, 80, 10, 38, 25, 23, 25, 30, 18, 65, 65]):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    note = wb.create_sheet('Пояснения')
    for line in ['Реестр зарегистрированных предложений, без черновиков.', 'Имя в Telegram — не подтверждённые ФИО и не отдельное поле организации.', 'Даты отображаются в UTC+5. Дата анализа — время постановки запроса в очередь.', 'ИИ-анализ является проектом для проверки экспертом.', 'Telegram ID, ключи, технические идентификаторы файлов и полные ИИ-справки не выгружаются.', 'Полный текст заявки: /view НОМЕР. Исходники анализа: /sources НОМЕР.', 'Строки имеют фиксированную высоту. Для чтения длинного текста увеличьте высоту строки.', 'Сформировано: ' + (datetime.now(timezone.utc) + timedelta(hours=5)).strftime('%d.%m.%Y %H:%M') + ' UTC+5']:
        note.append([line])
    note.column_dimensions['A'].width = 110
    output = io.BytesIO()
    wb.save(output)
    if len(output.getvalue()) > 15 * 1024 * 1024:
        raise ValueError('Выгрузка больше 15 МБ. Нужна выгрузка по периодам; файл не создан.')
    return output.getvalue()
