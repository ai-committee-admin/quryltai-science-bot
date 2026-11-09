"""Permission-gated semantic search using OpenAI embeddings; no generative answers.

A search is queued in SQLite, then performed outside the webhook. Case vectors
are cached per content digest; results are always filtered to one council chat.
"""
import hashlib
import json
import math
import os
import urllib.error
import urllib.request

MODEL = 'text-embedding-3-small'
MAX_CASES = 120


def initialize_semantic(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS semantic_jobs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL,
        requester INTEGER NOT NULL, query TEXT NOT NULL, exclude_task INTEGER,
        status TEXT NOT NULL DEFAULT 'pending', created TEXT DEFAULT CURRENT_TIMESTAMP);
      CREATE TABLE IF NOT EXISTS semantic_cache(
        task INTEGER PRIMARY KEY, digest TEXT NOT NULL, vector TEXT NOT NULL);
    ''')


def request(db, chat, uid, query, exclude=None):
    query=(query or '').strip()[:500]
    if len(query)<3:return 'Укажите тему для поиска (не менее 3 символов).'
    if not os.getenv('OPENAI_API_KEY','').strip():
        return 'ИИ-поиск не настроен. Нужен OPENAI_API_KEY; используйте обычный поиск /findcase ТЕМА.'
    pending=db.execute('''SELECT 1 FROM semantic_jobs WHERE chat=? AND requester=?
            AND status IN ('pending','running')''',(chat,uid)).fetchone()
    if pending:return 'Ваш предыдущий смысловой поиск ещё выполняется.'
    count=db.execute("SELECT COUNT(*) FROM semantic_jobs WHERE chat=? AND date(created)=date('now')",(chat,)).fetchone()[0]
    if count>=30:return 'На сегодня достигнут лимит 30 смысловых поисков в этом совете.'
    db.execute('INSERT INTO semantic_jobs(chat,requester,query,exclude_task) VALUES(?,?,?,?)',
               (chat,uid,query,exclude))
    return 'Ищу похожие темы по смыслу. Результат появится отдельным сообщением.'


def case_text(db, task):
    """Only persisted texts; no attachments, other groups or archived cases."""
    fragments=[task['body'][:1500]]
    opinions=db.execute('''SELECT m.body FROM council_materials m
        JOIN council_members cm ON cm.task=m.task AND cm.uid=m.uid
        WHERE m.task=? AND m.revision>0 AND m.revision=cm.revision
          AND cm.state IN ('submitted','accepted') AND m.body IS NOT NULL
        ORDER BY m.id DESC LIMIT 6''',(task['id'],)).fetchall()
    fragments.extend(r['body'][:550] for r in opinions if r['body'])
    doc=db.execute('SELECT result FROM conclusion_drafts WHERE task=? AND chat=?',
                   (task['id'],task['chat'])).fetchone()
    if doc:
        from knowledge import draft_text
        fragments.append(draft_text(doc['result'])[:1300])
    return '\n'.join(fragments)[:4500]


def _embeddings(texts):
    key=os.environ.get('OPENAI_API_KEY','').strip()
    if not key:raise ValueError('API key not configured')
    req=urllib.request.Request('https://api.openai.com/v1/embeddings',
       data=json.dumps({'model':MODEL,'input':texts}).encode('utf-8'),
       headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=60) as response: data=json.load(response)
    vectors=[None]*len(texts)
    for item in data['data']:
        idx=item['index']; vec=item['embedding']
        if not isinstance(idx,int) or not 0<=idx<len(texts) or not vec:raise ValueError('Invalid embedding index')
        vectors[idx]=vec
    if any(v is None for v in vectors):raise ValueError('Missing embedding')
    return vectors


def _similarity(a,b):
    if len(a)!=len(b):return 0.0
    dot=sum(x*y for x,y in zip(a,b))
    n1=math.sqrt(sum(x*x for x in a));n2=math.sqrt(sum(y*y for y in b))
    return dot/(n1*n2) if n1 and n2 else 0.0


def run_once(path, admins):
    from app import connect,queue
    from group_tasks import is_chair,emit
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        job=db.execute("SELECT * FROM semantic_jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if not job:return False
        group=db.execute('SELECT enabled FROM council_groups WHERE chat=?',(job['chat'],)).fetchone()
        if not group or not group[0] or not is_chair(db,job['chat'],job['requester'],admins):
            db.execute("UPDATE semantic_jobs SET status='failed' WHERE id=?",(job['id'],))
            return True
        db.execute("UPDATE semantic_jobs SET status='running' WHERE id=?",(job['id'],))
    try:
        with connect(path) as db:
            tasks=db.execute('''SELECT id,chat,body FROM council_tasks
                 WHERE chat=? AND archived=0 ORDER BY id DESC LIMIT ?''',
                 (job['chat'],MAX_CASES)).fetchall()
            docs=[(t,case_text(db,t)) for t in tasks if t['id']!=job['exclude_task']]
            missing=[];vectors={}
            for t,body in docs:
                digest=hashlib.sha256((MODEL+'\n'+body).encode('utf-8')).hexdigest()
                cached=db.execute('SELECT digest,vector FROM semantic_cache WHERE task=?',(t['id'],)).fetchone()
                if cached and cached['digest']==digest:
                    try:vectors[t['id']]=json.loads(cached['vector']);continue
                    except (ValueError,TypeError):pass
                missing.append((t['id'],digest,body))
        query_vec=_embeddings([job['query']])[0]
        # Batch requests: cap batch size to keep API calls predictable.
        for pos in range(0,len(missing),24):
            batch=missing[pos:pos+24]
            result=_embeddings([r[2] for r in batch])
            with connect(path) as db:
                for (tid,digest,_),vec in zip(batch,result):
                    db.execute('''INSERT INTO semantic_cache(task,digest,vector) VALUES(?,?,?)
                        ON CONFLICT(task) DO UPDATE SET digest=excluded.digest,vector=excluded.vector''',
                        (tid,digest,json.dumps(vec,separators=(',',':'))))
                    vectors[tid]=vec
        scored=[]
        for t,body in docs:
            score=_similarity(query_vec,vectors[t['id']])
            if score>=0.31:scored.append((score,t,body))
        scored.sort(key=lambda r:r[0],reverse=True)
        with connect(path) as db:
            # Recheck permissions and current archive state before displaying results.
            group=db.execute('SELECT enabled FROM council_groups WHERE chat=?',(job['chat'],)).fetchone()
            if not group or not group[0] or not is_chair(db,job['chat'],job['requester'],admins):
                db.execute("UPDATE semantic_jobs SET status='failed' WHERE id=?",(job['id'],))
                return True
            lines=['Смысловой поиск: «'+job['query'][:100]+'»'];buttons=[]
            for score,t,body in scored[:5]:
                current=db.execute('SELECT id,body FROM council_tasks WHERE id=? AND chat=? AND archived=0',
                                   (t['id'],job['chat'])).fetchone()
                if not current:continue
                lines.append(f'\nКейс №{t["id"]}: {current["body"].replace(chr(10)," ")[:110]}')
                buttons.append([(f'Открыть кейс №{t["id"]}',f'g:card:{t["id"]}')])
            if not buttons:lines.append('\nДостаточно близких материалов не найдено.')
            lines.append('\nСовпадение по смыслу — подсказка, а не подтверждение фактов. Проверяйте оригинальные мнения и заключения в карточке кейса. Вложения не проанализированы.')
            emit(db,queue,job['chat'],'\n'.join(lines),buttons)
            db.execute("UPDATE semantic_jobs SET status='done' WHERE id=?",(job['id'],))
    except Exception:
        with connect(path) as db:
            db.execute("UPDATE semantic_jobs SET status='failed' WHERE id=?",(job['id'],))
            emit(db,queue,job['chat'],'Не удалось выполнить смысловой поиск. Попробуйте обычный /findcase или повторите позже.')
    return True


def recover(db):
    db.execute("UPDATE semantic_jobs SET status='failed' WHERE status='running'")
