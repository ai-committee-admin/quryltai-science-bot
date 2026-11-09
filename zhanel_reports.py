"""Read-only, chat-scoped factual reports for Жанель.

Dates are recorded only for new records after v3.9 migration. Legacy records
have unknown creation dates and must never be presented as today's/yesterday's.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Almaty')


def initialize(db):
    # Sidecar tables keep original schemas and INSERT statements untouched.
    for table, sidecar in (('council_tasks','zhanel_task_times'),('council_materials','zhanel_material_times')):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            continue
        db.execute(f"CREATE TABLE IF NOT EXISTS {sidecar}(item INTEGER PRIMARY KEY, created_at TEXT NOT NULL)")
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS zhanel_stamp_{table}
            AFTER INSERT ON {table}
            BEGIN INSERT OR IGNORE INTO {sidecar}(item,created_at)
              VALUES(NEW.id,strftime('%Y-%m-%dT%H:%M:%SZ','now')); END;''')


def day_bounds(period, now=None):
    now = now or datetime.now(TZ)
    day = now.date() - timedelta(days=1 if period == 'yesterday' else 0)
    first = datetime(day.year, day.month, day.day, tzinfo=TZ)
    next_day = first + timedelta(days=1)
    return first.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ'), next_day.astimezone(ZoneInfo('UTC')).strftime('%Y-%m-%dT%H:%M:%SZ')


def case_list(db, chat, kind='active', now=None):
    if kind in ('today', 'yesterday'):
        start,end = day_bounds(kind,now)
        rows=db.execute('''SELECT t.id,t.body,t.closed FROM council_tasks t
          JOIN zhanel_task_times z ON z.item=t.id
          WHERE t.chat=? AND t.archived=0 AND z.created_at>=? AND z.created_at<?
          ORDER BY t.id DESC LIMIT 25''',(chat,start,end)).fetchall()
        heading='сегодня' if kind=='today' else 'вчера'
        if not rows:return f'Кейсов, созданных {heading}, не найдено. У старых кейсов дата создания не была сохранена, поэтому они не включены в выборку.',()
        caption=f'Кейсы, созданные {heading}:'
    elif kind=='latest':
        rows=db.execute('SELECT id,body,closed FROM council_tasks WHERE chat=? AND archived=0 ORDER BY id DESC LIMIT 1',(chat,)).fetchall()
        if not rows:return 'Активных кейсов нет.',()
        caption='Последний активный кейс:'
    else:
        rows=db.execute('SELECT id,body,closed FROM council_tasks WHERE chat=? AND archived=0 ORDER BY id DESC LIMIT 25',(chat,)).fetchall()
        if kind=='open':rows=[r for r in rows if not r['closed']]
        if not rows:return 'Активных кейсов по запросу не найдено.',()
        caption='Открытые кейсы:' if kind=='open' else 'Последние активные кейсы:'
    lines=[caption]
    buttons=[]
    for r in rows:
        lines.append(f'№{r["id"]} · {"Завершён" if r["closed"] else "Открыт"} · {r["body"].splitlines()[0][:125]}')
        buttons.append([(f'Открыть №{r["id"]}',f'g:card:{r["id"]}')])
    return '\n'.join(lines),buttons


def recent_discussions(db,chat,period,now=None):
    start,end=day_bounds(period,now)
    rows=db.execute('''SELECT t.id,t.body,COUNT(m.id) AS cnt
      FROM council_tasks t JOIN council_materials m ON m.task=t.id
      JOIN zhanel_material_times z ON z.item=m.id
      WHERE t.chat=? AND t.archived=0 AND m.revision>0 AND m.body IS NOT NULL
        AND z.created_at>=? AND z.created_at<?
      GROUP BY t.id ORDER BY MAX(m.id) DESC LIMIT 25''',(chat,start,end)).fetchall()
    label='сегодня' if period=='today' else 'вчера'
    if not rows:return f'Текстовых мнений за {label} с известным временем записи не найдено. Для старых сообщений время не фиксировалось.',()
    return 'Обсуждения за '+label+':\n'+'\n'.join(f'Кейс №{r["id"]}: {r["cnt"]} сообщений · {r["body"].splitlines()[0][:100]}' for r in rows), [[(f'Открыть №{r["id"]}',f'g:card:{r["id"]}')] for r in rows]


def opinions(db,chat,tid):
    task=db.execute('SELECT id FROM council_tasks WHERE chat=? AND id=? AND archived=0',(chat,tid)).fetchone()
    if not task:return 'Кейс не найден или находится в архиве.',()
    rows=db.execute('''SELECT m.uid,m.body,c.name FROM council_materials m
       LEFT JOIN council_members c ON c.task=m.task AND c.uid=m.uid
       WHERE m.task=? AND m.revision>0 AND m.body IS NOT NULL AND trim(m.body)!=''
       ORDER BY m.id DESC LIMIT 100''',(tid,)).fetchall()
    if not rows:return f'По кейсу №{tid} сохранённых текстовых мнений пока нет.',()
    names={}
    for r in rows:
        key=r['uid']; names.setdefault(key,{'name':r['name'] or f'Участник {key}','count':0})['count']+=1
    lines=[f'По кейсу №{tid} высказались {len(names)} участников, сохранено {len(rows)} текстовых сообщений:']
    lines += [f'• {v["name"]}: {v["count"]} сообщ.' for v in names.values()]
    return '\n'.join(lines),[[(f'Открыть №{tid}',f'g:card:{tid}')]]


def overdue(db,chat,now=None):
    now=now or datetime.now(TZ)
    today=now.date().isoformat()
    rows=db.execute('''SELECT r.id,r.task,r.agency,r.due,r.body FROM council_recommendations r
       JOIN council_tasks t ON t.id=r.task AND t.chat=r.chat
       WHERE r.chat=? AND t.archived=0 AND r.status NOT IN (3,4)
          AND r.due!='' AND r.due<? ORDER BY r.due ASC LIMIT 30''',(chat,today)).fetchall()
    if not rows:return 'Просроченных рекомендаций с установленным сроком нет.',()
    lines=['Просроченные рекомендации:']
    for r in rows:
        lines.append(f'№{r["id"]} · кейс №{r["task"]} · срок {r["due"]} · {r["agency"] or "адресат не указан"}\n{r["body"][:100]}')
    return '\n\n'.join(lines),[[(f'Открыть №{r["id"]}',f'g:recview:{r["id"]}')] for r in rows]
