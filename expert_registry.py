"""Private application registry. Never post candidate contact details into a group.

Candidates are *applications*, not approved experts. CSV import via administrator DM.
"""
import csv
import io
import re
import secrets
import unicodedata
from datetime import datetime

FIELDS = ('application_id','submitted','status','name','councils','organization','position','region','phone','email','topics','experience','education','profile_url','publication','decision','comment')
SCIENCE = 'Экспертный совет по науке и научно-технологическому развитию'
STOP = set('наука науки научный научные научных научно эксперт экспертный совет казахстан области вопрос вопросы проект проекты исследование исследования управление разработка развитие система системы работа деятельность опыт года при для или это есть эти тот как кто где что про по над под между также их они которое'.split())


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS expert_applications(
      application_id TEXT PRIMARY KEY,submitted TEXT,status TEXT,name TEXT NOT NULL,
      councils TEXT,organization TEXT,position TEXT,region TEXT,phone TEXT,email TEXT,
      topics TEXT,experience TEXT,education TEXT,profile_url TEXT,publication TEXT,
      decision TEXT,comment TEXT,imported TEXT DEFAULT CURRENT_TIMESTAMP,
      promoted_profile INTEGER);
    CREATE TABLE IF NOT EXISTS expert_csv_upload(uid INTEGER PRIMARY KEY,created TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS expert_app_name ON expert_applications(name);
    ''')


def _tokens(s):
    words = re.findall(r'[^\W_]+',unicodedata.normalize('NFKC',str(s or '')).casefold().replace('ё','е'))
    return set(x for x in words if len(x)>=5 and x not in STOP)


def import_csv(db, raw):
    if len(raw)>4_000_000:raise ValueError('CSV exceeds 4 MB')
    content=raw.decode('utf-8-sig')
    reader=csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or set(FIELDS)-set(reader.fieldnames):raise ValueError('Неверные колонки CSV')
    count=updated=skipped=0
    for row in reader:
        aid=(row.get('application_id') or '').strip()
        if not re.fullmatch(r'EXP-\d{4}-\d{6}',aid):skipped+=1;continue
        if SCIENCE.casefold() not in (row.get('councils') or '').casefold():skipped+=1;continue
        if not (row.get('name') or '').strip():skipped+=1;continue
        values=[(row.get(k) or '').strip()[:5000] for k in FIELDS]
        existing=db.execute('SELECT 1 FROM expert_applications WHERE application_id=?',(aid,)).fetchone()
        if existing:updated+=1
        else:count+=1
        db.execute('''INSERT INTO expert_applications('''+','.join(FIELDS)+''') VALUES('''+','.join('?' for _ in FIELDS)+''')
           ON CONFLICT(application_id) DO UPDATE SET '''+','.join(f'{k}=excluded.{k}' for k in FIELDS[1:]), values)
    return count,updated,skipped


def rank(db, query, limit=8):
    q=_tokens(query)
    if not q:return []
    results=[]
    for p in db.execute('SELECT * FROM expert_applications'):
        primary=_tokens(p['topics']);secondary=_tokens((p['position'] or '')+' '+(p['organization'] or '')+' '+(p['education'] or ''))
        overlaps=q & primary;support=q & secondary
        # Word-root overlap permits Russian declensions, but never claim verified expertise.
        for word in q:
            if word not in overlaps and len(word)>=7:
                if any(w[:6]==word[:6] for w in primary):overlaps.add(word)
                elif any(w[:6]==word[:6] for w in secondary):support.add(word)
        score=3*len(overlaps)+len(support)
        if score:results.append((score,p,sorted(overlaps)[:5]))
    results.sort(key=lambda x:(-x[0],x[1]['name']))
    return results[:limit]


def _send(db,queue,uid,text):
    for offset in range(0,len(text),1600):
        queue(db,'sendMessage',{'chat_id':uid,'text':text[offset:offset+1600]})


def candidate_text(p,private=False):
    val=f"{p['application_id']} · {p['name']}\n{p['topics'] or 'Специализация не указана'}\n{p['organization'] or 'Организация не указана'} · {p['position'] or 'Должность не указана'}\nСтатус: {p['status'] or 'Не указан'} (не подтверждено членство в совете)"
    if private:val+=f"\nГород: {p['region'] or '—'}\nКонтакты: {p['phone'] or '—'}, {p['email'] or '—'}\nПрофиль: {p['profile_url'] or '—'}"
    return val


def _find(db,query):
    p=db.execute('SELECT * FROM expert_applications WHERE application_id=?',(query.strip().upper(),)).fetchone()
    if p:return [p]
    pattern='%'+query[:70].replace('%','').replace('_','')+'%'
    return db.execute('SELECT * FROM expert_applications WHERE name LIKE ? LIMIT 8',(pattern,)).fetchall()


def handle_private(db, admins, update, queue):
    m=update.get('message') or {};chat=m.get('chat') or {};actor=m.get('from') or {};uid=actor.get('id')
    if chat.get('type')!='private' or uid!=chat.get('id') or uid not in admins:return False
    text=(m.get('text') or '').strip();parts=text.split(maxsplit=1);cmd=parts[0].split('@')[0] if parts else ''
    if cmd=='/expert_import':
        db.execute('INSERT OR REPLACE INTO expert_csv_upload(uid) VALUES(?)',(uid,))
        _send(db,queue,uid,'Отправь подготовленный CSV файлом в этот личный чат. Данные остаются в базе бота; они не публикуются в группе. /expert_cancel — отменить.')
        return True
    if cmd=='/expert_cancel':
        db.execute('DELETE FROM expert_csv_upload WHERE uid=?',(uid,))
        _send(db,queue,uid,'Импорт отменён.');return True
    if cmd=='/expert_count':
        n=db.execute('SELECT COUNT(*) FROM expert_applications').fetchone()[0]
        _send(db,queue,uid,f'В базе {n} заявок экспертов. Это кандидаты, не утверждённые члены совета.')
        return True
    if cmd in ('/candidate','/candidate_find'):
        if len(parts)<2:_send(db,queue,uid,'Пример: /candidate гидробиология или /candidate EXP-2026-000194');return True
        query=parts[1].strip()
        if cmd=='/candidate':
            matches=_find(db,query)
            _send(db,queue,uid,'\n\n'.join(candidate_text(p,True) for p in matches) if matches else 'Кандидат не найден.')
        else:
            matches=rank(db,query)
            _send(db,queue,uid,'\n\n'.join(candidate_text(p) for _,p,_ in matches) if matches else 'Совпадений по заявленным специализациям нет.')
        return True
    if cmd=='/candidate_promote':
        if len(parts)<2:_send(db,queue,uid,'Пример: /candidate_promote EXP-2026-000194');return True
        p=db.execute('SELECT * FROM expert_applications WHERE application_id=?',(parts[1].upper(),)).fetchone()
        if not p:_send(db,queue,uid,'Заявка не найдена.');return True
        if p['promoted_profile']:
            _send(db,queue,uid,f'Профиль №{p["promoted_profile"]} уже создан.');return True
        role=' · '.join(x for x in (p['organization'],p['position']) if x) or 'Не указано'
        created=db.execute('INSERT INTO expert_profiles(name,role,topics,experience,education) VALUES(?,?,?,?,?)',
            (p['name'][:1500],role[:1500],(p['topics'] or 'Не указано')[:1500],(p['experience'] or 'Не указано')[:1500],(p['education'] or 'Не указано')[:1500])).lastrowid
        db.execute('UPDATE expert_applications SET promoted_profile=? WHERE application_id=?',(created,p['application_id']))
        _send(db,queue,uid,f'Черновик профиля №{created} создан, но НЕ опубликован. Проще всего: когда эксперт напишет в группе, ответь на его сообщение командой /profile_link и выбери профиль №{created}. Альтернатива: /expert_invite {created} TELEGRAM_ID CHAT_ID.')
        return True
    pending=db.execute('SELECT 1 FROM expert_csv_upload WHERE uid=?',(uid,)).fetchone()
    if pending and m.get('document'):
        doc=m['document']
        if not (doc.get('file_name') or '').lower().endswith('.csv'):
            _send(db,queue,uid,'Ожидаю файл CSV, не Word/Excel.');return True
        if int(doc.get('file_size') or 0)>4_000_000:
            _send(db,queue,uid,'CSV слишком большой, максимум 4 МБ.');return True
        # Telegram file is downloaded *outside* SQLite transaction by the existing app's safe URL machinery.
        # Keep the file ID in a job; a worker will download/import it safely.
        db.execute('CREATE TABLE IF NOT EXISTS expert_import_jobs(id INTEGER PRIMARY KEY,uid INTEGER,file_id TEXT,state TEXT DEFAULT "pending")')
        db.execute('INSERT INTO expert_import_jobs(uid,file_id) VALUES(?,?)',(uid,doc['file_id']))
        db.execute('DELETE FROM expert_csv_upload WHERE uid=?',(uid,))
        _send(db,queue,uid,'Файл принят. Начинаю загрузку кандидатов в базу.')
        return True
    return False


def handle_group(db,admins,update,queue):
    m=update.get('message') or {};ch=m.get('chat') or {};who=m.get('from') or {}
    cid=ch.get('id');uid=who.get('id')
    if ch.get('type') not in ('group','supergroup') or not isinstance(uid,int):return False
    text=(m.get('text') or '').strip()
    match=re.search(r'(?:^/candidate_match\s+|^\s*(?:жанель|жәнел)[,\s]+.*?(?:подбер\w*|найд\w*|подскаж\w*)\s+(?:подходящ\w*\s+)?эксперт\w*.*?(?:кейс\w*\s*(?:№\s*)?))(\d+)',text,re.I)
    if not match:return False
    from group_tasks import is_chair
    if not is_chair(db,cid,uid,admins):
        _send(db,queue,cid,'Подбор кандидатов доступен ведущему и администратору.');return True
    group=db.execute('SELECT enabled FROM council_groups WHERE chat=?',(cid,)).fetchone()
    if not group or not group[0]:return False
    task=db.execute('SELECT body FROM council_tasks WHERE chat=? AND id=? AND archived=0',(cid,int(match.group(1)))).fetchone()
    if not task:_send(db,queue,cid,'Кейс не найден в этом чате.');return True
    matches=rank(db,task['body'],6)
    if matches:
        lines=[f"{p['application_id']} · {p['name']}\n{p['topics'][:250]}\nСовпадение: {', '.join(why) if why else 'по профилю'}" for _,p,why in matches]
        _send(db,queue,uid,'Кандидаты для кейса №'+match.group(1)+' (по заявкам, квалификация не проверена):\n\n'+'\n\n'.join(lines))
        _send(db,queue,cid,'Список подходящих кандидатов отправлен администратору в личные сообщения. Это не утверждённые участники совета.')
    else:_send(db,queue,cid,'В заявках не нашлось кандидатов с прямыми совпадениями специализации. Нужен более широкий поиск.')
    return True


def import_pending_once(path,admins,bot_token):
    """Run outside webhook transaction; use Telegram getFile/file download."""
    import json,urllib.request,urllib.parse
    from app import connect,queue
    if not bot_token:return False
    with connect(path) as db:
        jobs=db.execute("SELECT * FROM expert_import_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchall() if db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='expert_import_jobs'").fetchone() else []
        if not jobs:return False
        job=jobs[0]
        if job['uid'] not in admins:
            db.execute("UPDATE expert_import_jobs SET state='failed' WHERE id=?",(job['id'],))
            return True
        db.execute("UPDATE expert_import_jobs SET state='working' WHERE id=?",(job['id'],))
    try:
        with urllib.request.urlopen('https://api.telegram.org/bot'+bot_token+'/getFile?'+urllib.parse.urlencode({'file_id':job['file_id']}),timeout=25) as r:
            meta=json.load(r)
        if not meta.get('ok'):raise ValueError('getFile failed')
        path_remote=meta['result']['file_path']
        if '..' in path_remote or path_remote.startswith('/'):raise ValueError('unsafe file path')
        with urllib.request.urlopen('https://api.telegram.org/file/bot'+bot_token+'/'+path_remote,timeout=40) as r:
            data=r.read(4_000_001)
        with connect(path) as db:
            count,updated,skipped=import_csv(db,data)
            db.execute("UPDATE expert_import_jobs SET state='done' WHERE id=?",(job['id'],))
            queue(db,'sendMessage',{'chat_id':job['uid'],'text':f'Импорт завершён: новых {count}, обновлено {updated}, пропущено {skipped}. /expert_count — проверить.'})
    except Exception as exc:
        with connect(path) as db:
            db.execute("UPDATE expert_import_jobs SET state='failed' WHERE id=?",(job['id'],))
            queue(db,'sendMessage',{'chat_id':job['uid'],'text':'Ошибка импорта: '+str(exc)[:180]})
    return True
