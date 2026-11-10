"""Confirmed, scoped conversational recommendation creation.

The model cannot persist recommendations.  A user message creates a short-lived
pending proposal; only the same administrator can confirm it in the same chat.
"""
import re
import secrets
from datetime import datetime, timezone, timedelta

ADD_RE = re.compile(r'\b(?:добав(?:ь|ить|им|ляем|ляй)|созда(?:й|ть)|зарегистрир(?:уй|овать)|внес(?:и|ти)|запиш(?:и|и)|постав(?:ь|ить)\s+на\s+контроль|қос|тірке)\w*',re.I)
REC_RE = re.compile(r'(?:рекомендац|предложени[ея]|ұсыным|ұсыныс)',re.I)
CASE_RE = re.compile(r'\b(?:кейс(?:у|а|е|ом)?|іс)\s*(?:№\s*|#\s*)?(\d{1,10})\b',re.I)
INTRO_RE = re.compile(r'^\s*(?:жанель|жәнел|жанел|zhanel|janel)[\s,!:;.—-]*',re.I)


def is_add_request(s):
    return bool(ADD_RE.search(s) and REC_RE.search(s))


def extract(s):
    """Extract recommendation body after case number and separator, never synthesize content."""
    match=CASE_RE.search(s)
    tid=int(match.group(1)) if match else None
    if match:
        rest=s[match.end():].strip(' \t:—–-.,;')
    else:
        rest=re.sub(r'^.*?(?:рекомендац\w*|предложени\w*|ұсыным\w*|ұсыныс\w*)', '',s, count=1, flags=re.I).strip(' \t:—–-.,;')
    rest=re.sub(r'^(?:о\s+том\s*,?\s*что|чтобы|следующ(?:ее|ую)|текст\s*[:—-])\s*', '',rest,flags=re.I)
    return tid,rest.strip(' \t\n.,;')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS zhanel_rec_pending(
         chat INTEGER NOT NULL, uid INTEGER NOT NULL, task INTEGER NOT NULL,
         body TEXT NOT NULL, nonce INTEGER NOT NULL, created_at TEXT NOT NULL,
         PRIMARY KEY(chat,uid))''')


def _pending(db,chat,uid):
    row=db.execute('SELECT * FROM zhanel_rec_pending WHERE chat=? AND uid=?',(chat,uid)).fetchone()
    if not row:return None
    try:
        stamp=datetime.fromisoformat(row['created_at'])
        if datetime.now(timezone.utc)-stamp>timedelta(minutes=15):
            db.execute('DELETE FROM zhanel_rec_pending WHERE chat=? AND uid=?',(chat,uid))
            return None
    except (TypeError,ValueError):return None
    return row


def begin(db,queue,chat,uid,raw,emit,remember_case=None,ai_topic=None):
    tid,body=extract(raw)
    if tid is None and remember_case is not None:tid=remember_case
    if tid is None:
        emit(db,queue,chat,'К какому кейсу добавить рекомендацию? Напиши, например: «Жанель, добавь рекомендацию к кейсу №8: увеличить финансирование исследований».')
        return True
    task=db.execute('SELECT id FROM council_tasks WHERE id=? AND chat=? AND archived=0',(tid,chat)).fetchone()
    if not task:
        emit(db,queue,chat,f'Активный кейс №{tid} в этой группе не найден.')
        return True
    if not body and isinstance(ai_topic,str):body=ai_topic.strip()
    if len(body)<12:
        emit(db,queue,chat,'Напиши конкретный текст рекомендации после номера кейса. Например: «Жанель, добавь рекомендацию по кейсу №8: увеличить финансирование исследований».')
        return True
    if len(body)>1500:
        emit(db,queue,chat,'Рекомендация слишком длинная (максимум 1500 символов). Сократи текст.')
        return True
    nonce=secrets.randbelow(1000000000)+1
    db.execute('''INSERT INTO zhanel_rec_pending(chat,uid,task,body,nonce,created_at) VALUES(?,?,?,?,?,?)
      ON CONFLICT(chat,uid) DO UPDATE SET task=excluded.task,body=excluded.body,nonce=excluded.nonce,created_at=excluded.created_at''',
      (chat,uid,tid,body,nonce,datetime.now(timezone.utc).isoformat()))
    emit(db,queue,chat,f'Добавить рекомендацию к кейсу №{tid}?\n\n«{body}»',
         [[('Подтвердить',f'g:zrconfirm:{uid}:{nonce}'),('Отмена',f'g:zrcancel:{uid}:{nonce}')]])
    return True


def callback(db,queue,chat,uid,action,owner,chair,emit,nonce=None):
    if not chair or owner!=uid:return 'Подтвердить может только администратор, создавший предложение.'
    row=_pending(db,chat,uid)
    if not row or (nonce is not None and nonce!=row['nonce']):return 'Запрос устарел. Открой последнее подтверждение.'
    db.execute('DELETE FROM zhanel_rec_pending WHERE chat=? AND uid=?',(chat,uid))
    if action=='zrcancel':
        emit(db,queue,chat,'Добавление рекомендации отменено.')
        return 'Отменено.'
    from recommendations import add
    result=add(db,chat,row['task'],uid,row['body'])
    emit(db,queue,chat,result)
    return 'Готово.'


def text_confirmation(db,queue,chat,uid,raw,emit):
    if not _pending(db,chat,uid):return False
    value=raw.strip().casefold().strip(' .!')
    if value in ('подтверждаю','да, добавь','да добавь','сохранить','подтвердить'):
        callback(db,queue,chat,uid,'zrconfirm',uid,True,emit)
        return True
    if value in ('отмена','отмени','не добавляй'):
        callback(db,queue,chat,uid,'zrcancel',uid,True,emit)
        return True
    return False
