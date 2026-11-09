"""Tracking council recommendations with auditable changes; no automatic agency messaging."""
from datetime import datetime, timedelta

STATUSES = ('Подготовлена', 'Направлена', 'На рассмотрении', 'Реализована', 'Отклонена')
FINAL = (3, 4)


def initialize(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS council_recommendations (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat INTEGER NOT NULL, task INTEGER NOT NULL REFERENCES council_tasks(id),
      body TEXT NOT NULL, status INTEGER NOT NULL DEFAULT 0 CHECK(status BETWEEN 0 AND 4),
      agency TEXT NOT NULL DEFAULT '', due TEXT NOT NULL DEFAULT '',
      created_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS rec_chat_task ON council_recommendations(chat,task);
    CREATE TABLE IF NOT EXISTS council_recommendation_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT, recommendation INTEGER NOT NULL REFERENCES council_recommendations(id),
      actor INTEGER NOT NULL, field TEXT NOT NULL, old TEXT NOT NULL DEFAULT '',
      new TEXT NOT NULL DEFAULT '', changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS council_recommendation_reminders (
      recommendation INTEGER NOT NULL REFERENCES council_recommendations(id),
      event TEXT NOT NULL, due TEXT NOT NULL, PRIMARY KEY(recommendation,event,due)
    );
    """)


def get(db, chat, rid):
    return db.execute('''SELECT r.* FROM council_recommendations r
       JOIN council_tasks t ON t.id=r.task AND t.chat=r.chat
       WHERE r.id=? AND r.chat=? AND t.archived=0''',(rid,chat)).fetchone()


def record(db, rid, uid, field, old, new):
    db.execute('INSERT INTO council_recommendation_history(recommendation,actor,field,old,new) VALUES(?,?,?,?,?)',
               (rid,uid,field,str(old),str(new)))


def add(db, chat, task, uid, body):
    body=body.strip()
    if not 12<=len(body)<=1500:return 'Текст рекомендации должен содержать 12–1500 символов.'
    exists=db.execute('SELECT id FROM council_tasks WHERE id=? AND chat=? AND archived=0',(task,chat)).fetchone()
    if not exists:return 'Активный кейс не найден.'
    rid=db.execute('INSERT INTO council_recommendations(chat,task,body,created_by) VALUES(?,?,?,?)',
                   (chat,task,body,uid)).lastrowid
    record(db,rid,uid,'created','',body)
    return f'Рекомендация №{rid} добавлена к кейсу №{task}. Открыть: /rec {rid}'


def update_field(db, chat, rid, uid, field, value):
    row=get(db,chat,rid)
    if row is None:return 'Рекомендация не найдена.'
    if field=='status':
        try:value=int(value)
        except (ValueError, TypeError):return 'Недопустимый статус.'
        if not 0<=value<len(STATUSES):return 'Недопустимый статус.'
        if value==row['status']:return 'Статус не изменился.'
    elif field=='agency':
        value=value.strip()
        if not 2<=len(value)<=200:return 'Укажи организацию (2–200 символов).'
    elif field=='due':
        try:
            value=datetime.strptime(value.strip(),'%d.%m.%Y').date().isoformat()
        except ValueError:return 'Дата в формате ДД.ММ.ГГГГ, например 30.11.2026.'
    elif field=='note':
        value=value.strip()
        if not 3<=len(value)<=1500:return 'Комментарий должен содержать 3–1500 символов.'
        record(db,rid,uid,field,'',value)
        return f'Комментарий по рекомендации №{rid} сохранён в истории.'
    else:return 'Неизвестное действие.'
    old=row[field]
    if old==value:return 'Данные не изменились.'
    db.execute(f'UPDATE council_recommendations SET {field}=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND chat=?', (value,rid,chat))
    record(db,rid,uid,field,old,value)
    return f'Рекомендация №{rid} обновлена.'


def show(db, chat, rid):
    row=get(db,chat,rid)
    if row is None:return None, ()
    buttons=[[(name,f'g:recstatus:{rid}:{i}') for i,name in enumerate(STATUSES[:2])],
             [(name,f'g:recstatus:{rid}:{i}') for i,name in enumerate(STATUSES[2:4])],
             [(STATUSES[4],f'g:recstatus:{rid}:4')]]
    text=(f'Рекомендация №{rid} · кейс №{row["task"]}\n{row["body"]}\n\n'
          f'Статус: {STATUSES[row["status"]]}\nАдресат: {row["agency"] or "не указан"}\n'
          f'Срок: {row["due"] or "не указан"}\n\n'
          f'/recorg {rid} Министерство...\n/recdate {rid} 30.11.2026\n'
          f'/recnote {rid} Получен ответ...\n/rechistory {rid} — история')
    return text, buttons


def list_items(db, chat, task=None):
    if task is None:
        rows=db.execute('''SELECT r.* FROM council_recommendations r
             JOIN council_tasks t ON t.id=r.task AND t.chat=r.chat
             WHERE r.chat=? AND t.archived=0 ORDER BY r.id DESC LIMIT 25''',(chat,)).fetchall()
    else:
        rows=db.execute('''SELECT r.* FROM council_recommendations r
             JOIN council_tasks t ON t.id=r.task AND t.chat=r.chat
             WHERE r.chat=? AND r.task=? AND t.archived=0 ORDER BY r.id DESC LIMIT 25''',(chat,task)).fetchall()
    if not rows:return 'Рекомендаций пока нет. Добавить: /recadd НОМЕР_КЕЙСА текст рекомендации',()
    text='Рекомендации экспертного совета:\n'+ '\n'.join(
         f'№{r["id"]} · кейс №{r["task"]} · {STATUSES[r["status"]]} · {r["body"][:65]}' for r in rows)
    return text,[[(f'Открыть №{r["id"]}',f'g:recview:{r["id"]}')] for r in rows]


def history(db,chat,rid):
    if get(db,chat,rid) is None:return 'Рекомендация не найдена.'
    rows=db.execute('SELECT * FROM council_recommendation_history WHERE recommendation=? ORDER BY id DESC LIMIT 15',(rid,)).fetchall()
    names={'created':'Создана','status':'Статус','agency':'Адресат','due':'Срок','note':'Комментарий'}
    return 'История рекомендации №'+str(rid)+':\n'+ '\n'.join(
        f'{r["changed_at"]} · {names.get(r["field"],r["field"])} · {r["new"][:180]}' for r in rows)


def reminders(db,queue,now,emit):
    # once on due date or 3 days before; no notifications for completed/rejected recommendations.
    day=now.date()
    rows=db.execute('''SELECT r.* FROM council_recommendations r
      JOIN council_tasks t ON t.id=r.task AND t.chat=r.chat
      JOIN council_groups g ON g.chat=r.chat
      WHERE g.enabled=1 AND t.archived=0 AND r.status NOT IN (3,4) AND r.due!='' ''').fetchall()
    for r in rows:
        due=datetime.strptime(r['due'],'%Y-%m-%d').date()
        event='due' if day>=due else 'soon' if day==due-timedelta(days=3) else None
        if event is None:continue
        # Single overdue notification on due date; next time only if due changes.
        if db.execute('INSERT OR IGNORE INTO council_recommendation_reminders VALUES(?,?,?)',
                      (r['id'],event,r['due'])).rowcount:
            emit(db,queue,r['chat'],f'Контроль рекомендации №{r["id"]} · кейс №{r["task"]}\n'
                 f'{"Срок сегодня или уже прошёл" if event=="due" else "До срока 3 дня"}: {r["due"]}\n'
                 f'Адресат: {r["agency"] or "не указан"}. /rec {r["id"]}')
