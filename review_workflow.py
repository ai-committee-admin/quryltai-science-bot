"""Administrator-only review fields with durable edit state and audit history."""
import re
import time
from datetime import datetime

FIELDS = {
    'topic': ('Тема', 200),
    'assignee': ('Ответственный эксперт', 200),
    'deadline': ('Срок', 10),
    'comment': ('Комментарий эксперта (внесён администратором)', 4000),
    'outcome': ('Результат рассмотрения', 4000),
}


def initialize_review(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(proposals)')}
    for field in FIELDS:
        if field not in columns:
            db.execute(f"ALTER TABLE proposals ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    db.executescript('''
        CREATE TABLE IF NOT EXISTS review_edits (
            user_id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL REFERENCES proposals(id),
            field TEXT NOT NULL, old_value TEXT NOT NULL, expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS review_history (
            id INTEGER PRIMARY KEY, proposal_id INTEGER NOT NULL REFERENCES proposals(id),
            actor INTEGER NOT NULL, field TEXT NOT NULL, old_value TEXT NOT NULL,
            new_value TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    ''')


def formatted(field, value):
    if not value:
        return 'не указано'
    if field == 'deadline':
        return datetime.strptime(value, '%Y-%m-%d').strftime('%d.%m.%Y')
    return value


def review_card(db, uid, row, say, inline):
    say(db, uid, 'Рассмотрение заявки №' + str(row['id']) + '\n' + '\n'.join(
        f'{label}: {formatted(field, row[field])}' for field, (label, _) in FIELDS.items()))
    n = row['id']
    inline(db, uid, 'Изменить сведения о рассмотрении', [
        [('Тема', f'/edit {n} topic'), ('Ответственный', f'/edit {n} assignee')],
        [('Срок', f'/edit {n} deadline'), ('Комментарий', f'/edit {n} comment')],
        [('Результат', f'/edit {n} outcome'), ('История', f'/history {n}')]])


def review_input(db, uid, admins, text, cmd, draft, message, say, inline):
    pending = db.execute('SELECT * FROM review_edits WHERE user_id=?', (uid,)).fetchone()
    if uid not in admins:
        if pending:
            db.execute('DELETE FROM review_edits WHERE user_id=?', (uid,))
        if cmd in ('/edit', '/history'):
            say(db, uid, 'Команда доступна только администратору.')
            return True
        return False
    if cmd in ('/edit', '/history'):
        db.execute('DELETE FROM review_edits WHERE user_id=?', (uid,))
        args = text.split()
        valid = len(args) == (3 if cmd == '/edit' else 2) and re.fullmatch(r'[1-9][0-9]{0,12}', args[1])
        if not valid or (cmd == '/edit' and args[2] not in FIELDS):
            say(db, uid, 'Откройте заявку и нажмите нужную кнопку. Команда: /edit НОМЕР topic|assignee|deadline|comment|outcome или /history НОМЕР.')
            return True
        row = db.execute("SELECT * FROM proposals WHERE id=? AND status NOT IN ('draft','archived')", (int(args[1]),)).fetchone()
        if not row:
            say(db, uid, 'Зарегистрированная заявка не найдена.')
            return True
        if cmd == '/history':
            records = db.execute('SELECT * FROM review_history WHERE proposal_id=? ORDER BY id DESC LIMIT 20', (row['id'],)).fetchall()
            say(db, uid, f'История заявки №{row["id"]}: последние 20 изменений. Время UTC.\n' + ('\n\n'.join(
                f'{r["created"]} · администратор {r["actor"]}\n{FIELDS.get(r["field"], (r["field"], 0))[0]}\nБыло: {r["old_value"] or "—"}\nСтало: {r["new_value"] or "—"}' for r in records) or 'Изменений пока нет.'))
            return True
        if draft:
            say(db, uid, 'Сначала завершите свой черновик командой /done или удалите его через /cancel. Затем измените карточку.')
            return True
        field = args[2]
        db.execute('INSERT INTO review_edits VALUES (?,?,?,?,?)', (uid, row['id'], field, row[field], time.time()+1800))
        hint = {'topic': 'Например: финансирование, оборудование, кадры, внедрение разработок или другая тема.',
                'assignee': 'Введите ФИО эксперта. Это учётная запись в карточке; уведомление эксперту пока не отправляется.',
                'deadline': 'Введите дату в формате ДД.ММ.ГГГГ.',
                'comment': 'Введите позицию эксперта; при необходимости укажите автора и дату комментария.',
                'outcome': 'Введите итог рассмотрения. Запись результата сама по себе не меняет статус заявки.'}[field]
        say(db, uid, f'Заявка №{row["id"]}. Поле: {FIELDS[field][0]}.\nСейчас: {formatted(field, row[field])}\n{hint}\nПришлите одно текстовое сообщение (до {FIELDS[field][1]} символов). «-» — очистить поле. /cancel — отменить. Ожидание 30 минут.')
        return True
    if not pending:
        return False
    if cmd == '/cancel':
        db.execute('DELETE FROM review_edits WHERE user_id=?', (uid,))
        say(db, uid, 'Изменение карточки отменено. Сведения сохранены без изменений.')
        return True
    if cmd.startswith('/') or text in ('Подать предложение', 'Мои предложения', 'О совете', 'Мой ID'):
        db.execute('DELETE FROM review_edits WHERE user_id=?', (uid,))
        say(db, uid, 'Ввод в карточку отменён при переходе к другой команде.')
        return False
    if time.time() > pending['expires']:
        db.execute('DELETE FROM review_edits WHERE user_id=?', (uid,))
        say(db, uid, 'Время ввода истекло. Нажмите кнопку поля ещё раз; сообщение не сохранено.')
        return True
    field = pending['field']
    if not text or message.get('document') or len(text) > FIELDS[field][1]:
        say(db, uid, f'Нужно одно текстовое сообщение до {FIELDS[field][1]} символов, без вложений. /cancel — отменить.')
        return True
    value = '' if text == '-' else text
    if field == 'deadline' and value:
        try:
            if not re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', value):
                raise ValueError()
            date = datetime.strptime(value, '%d.%m.%Y')
            if not 2000 <= date.year <= 2100:
                raise ValueError()
            value = date.strftime('%Y-%m-%d')
        except ValueError:
            say(db, uid, 'Дата некорректна. Укажите ДД.ММ.ГГГГ (2000–2100), например 20.11.2026, или /cancel.')
            return True
    row = db.execute('SELECT * FROM proposals WHERE id=?', (pending['proposal_id'],)).fetchone()
    db.execute('DELETE FROM review_edits WHERE user_id=?', (uid,))
    if not row or row[field] != pending['old_value']:
        say(db, uid, 'Поле уже изменил другой администратор. Откройте карточку заново; ваше сообщение не сохранено.')
        return True
    if value != row[field]:
        db.execute(f'UPDATE proposals SET {field}=? WHERE id=?', (value, row['id']))
        db.execute('INSERT INTO review_history(proposal_id,actor,field,old_value,new_value) VALUES(?,?,?,?,?)', (row['id'], uid, field, row[field], value))
    say(db, uid, f'Заявка №{row["id"]}: {FIELDS[field][0]} — {formatted(field, value)}.')
    updated = db.execute('SELECT * FROM proposals WHERE id=?', (row['id'],)).fetchone()
    review_card(db, uid, updated, say, inline)
    return True
