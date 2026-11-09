"""Lightweight Russian/Kazakh natural-language entrypoint for existing council actions.

No additional model API calls, no executing arbitrary model output, and no action
without validating the originating chat, actor and target.
"""
import re

WAKE = re.compile(r'^\s*(?:жанель|жәнел|жанел|zhanel|janel)[\s,!:;.—-]*', re.I)
NUMBER = re.compile(r'\b(?:кейс(?:а|е|у|ом)?\s*(?:№|#)?\s*|№\s*|#\s*)(\d{1,10})\b', re.I)


def _intent(s):
    s=s.lower().replace('ё','е')
    if re.search(r'\b(?:удал|убер|архив|өшір|жой)\w*',s):return 'archive'
    if re.search(r'\b(?:восстанов|верни|қайтар)\w*',s):return 'restore'
    if re.search(r'\b(?:созда|открой новый|жаңа іс|кейс аш)\w*',s) and 'кейс' in s:return 'create'
    if re.search(r'(?:доработ|исправ|передел|өзгерт|түзет)\w*',s) and re.search(r'заключ|қорытын',s):return 'edit'
    if re.search(r'(?:рекомендац|ұсыным)',s) and re.search(r'(?:заключ|қорытын)',s):return 'recpreview'
    if re.search(r'(?:word|ворд|docx|скача|жүкте)',s) and re.search(r'(?:заключ|қорытын)',s):return 'word'
    if re.search(r'(?:заключ|қорытын)',s):return 'conclusion'
    if re.search(r'(?:подвед|итог|резюмир|қорытындыла)',s):return 'summary'
    if re.search(r'(?:вопрос|сұрақ)',s) and re.search(r'(?:обсужд|талқыла)',s):return 'questions'
    if re.search(r'(?:аналитик|общий отчет|сводн|талдау)',s):return 'analytics'
    if re.search(r'(?:контрол|статус|рекомендац|ұсыным)',s):return 'recommendations'
    if re.search(r'(?:найд|поиск|похож|ізде|ұқсас)',s):return 'knowledge'
    if re.search(r'(?:помощ|помог|что уме|көмек|help)',s):return 'help'
    return 'unknown'


def _case(db, chat, uid, msg, raw):
    found=NUMBER.search(raw)
    if found:
        task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(int(found.group(1)),chat)).fetchone()
        return task, True
    reply=(msg.get('reply_to_message') or {}).get('message_id')
    if reply:
        link=db.execute('SELECT task FROM council_links WHERE chat=? AND message=?',(chat,reply)).fetchone()
        if link:
            return db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(link['task'],chat)).fetchone(), True
    # If a topic is given instead of a number, use it only when there is a unique match.
    topic_match=re.search(r'\b(?:по теме|на тему|по вопросу|по)\s+(.+)$',raw,flags=re.I)
    if topic_match:
        topic=topic_match.group(1).strip(' .?!«»"').casefold()
        if len(topic)>=5 and not topic.startswith('кейсу'):
            rows=db.execute('SELECT * FROM council_tasks WHERE chat=? AND archived=0',(chat,)).fetchall()
            matches=[r for r in rows if topic in r['body'].casefold()]
            if len(matches)==1:return matches[0],True
    last=db.execute('SELECT task FROM zhanel_context WHERE chat=? AND uid=?',(chat,uid)).fetchone()
    if last:
        row=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=? AND archived=0',(last['task'],chat)).fetchone()
        if row:return row, False
    active=db.execute('SELECT * FROM council_tasks WHERE chat=? AND archived=0 ORDER BY id DESC LIMIT 2',(chat,)).fetchall()
    return (active[0],False) if len(active)==1 else (None,False)


def _remember(db,chat,uid,task):
    db.execute('INSERT INTO zhanel_context(chat,uid,task) VALUES(?,?,?) ON CONFLICT(chat,uid) DO UPDATE SET task=excluded.task',(chat,uid,task))


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS zhanel_context(
        chat INTEGER NOT NULL,uid INTEGER NOT NULL,task INTEGER NOT NULL,
        PRIMARY KEY(chat,uid))''')


def handle(db,queue,chat,uid,message,admins):
    """Return False only if message is not directed to Жанель."""
    text=(message.get('text') or '').strip()
    match=WAKE.match(text)
    if not match:return False
    from group_tasks import emit,is_chair,task_card
    if not is_chair(db,chat,uid,admins):
        emit(db,queue,chat,'Жанель выполняет действия с кейсами только по запросу администратора или ведущего.')
        return True
    raw=text[match.end():].strip()
    action=_intent(raw)
    if action=='help' or not raw:
        emit(db,queue,chat,'Можешь писать обычными словами: «Жанель, подготовь заключение по кейсу №5», «Жанель, скачай заключение №5 в Word», «Жанель, подведи итоги кейса №5», «Жанель, найди похожие проблемы по грантам», «Жанель, что на контроле?». Для удаления я запрошу подтверждение.')
        return True
    if action=='unknown':
        emit(db,queue,chat,'Не поняла просьбу. Попробуй: «Жанель, подготовь заключение по кейсу №5» или «Жанель, что у нас на контроле?»')
        return True
    if action=='recommendations':
        from recommendations import list_items
        result,buttons=list_items(db,chat)
        emit(db,queue,chat,result,buttons)
        return True
    if action=='analytics':
        from science_analytics import request
        emit(db,queue,chat,request(db,chat,uid))
        return True
    if action=='knowledge':
        from semantic_knowledge import request
        query=re.sub(r'^(?:найди|найти|поищи|покажи|ізде)\s*', '',raw,flags=re.I).strip()
        if len(query)<3:
            emit(db,queue,chat,'Напиши тему: «Жанель, найди похожие проблемы по грантовому финансированию».')
        else:
            emit(db,queue,chat,request(db,chat,uid,query[:500]))
        return True
    if action=='create':
        detail=re.sub(r'^.*?\bкейс\b\s*[:№.\-]?\s*','',raw,flags=re.I).strip()
        detail=re.sub(r'^\b(?:по|о|об|на тему)\b\s*','',detail,flags=re.I).strip()
        if len(detail)<12:
            emit(db,queue,chat,'Укажи тему и проблему: «Жанель, создай кейс: задержки грантового финансирования и их влияние на исследования».')
            return True
        task_id=db.execute('INSERT INTO council_tasks(chat,creator,body) VALUES(?,?,?)',(chat,uid,detail[:5000])).lastrowid
        task=db.execute('SELECT * FROM council_tasks WHERE chat=? AND id=?',(chat,task_id)).fetchone()
        _remember(db,chat,uid,task_id)
        task_card(db,queue,task)
        return True
    task,explicit=_case(db,chat,uid,message,raw)
    if task is None:
        emit(db,queue,chat,'Уточни номер кейса, например: «Жанель, подготовь заключение по кейсу №5». Можно также ответить Жанель на карточку кейса.')
        return True
    tid=task['id']
    if task['archived'] and action!='restore':
        emit(db,queue,chat,f'Кейс №{tid} находится в архиве. Его можно восстановить через /archivedcases.')
        return True
    _remember(db,chat,uid,tid)
    if action=='archive':
        emit(db,queue,chat,f'Переместить кейс №{tid} в архив? Все материалы останутся для восстановления.',
             [[('Подтвердить',f'g:confirmarchive:{tid}'),('Отмена',f'g:card:{tid}')]])
    elif action=='restore':
        emit(db,queue,chat,'Для восстановления открой /archivedcases и нажми «Восстановить».')
    elif action=='summary' or action=='questions':
        from moderator import request_moderation
        emit(db,queue,chat,request_moderation(db,task,uid,action))
    elif action=='conclusion':
        from conclusions import request_conclusion
        emit(db,queue,chat,request_conclusion(db,task,uid,'draft'))
    elif action=='word':
        from conclusions import export_conclusion
        emit(db,queue,chat,export_conclusion(db,queue,task))
    elif action=='recpreview':
        from recommendation_import import prepare
        result,buttons=prepare(db,chat,tid)
        emit(db,queue,chat,result,buttons)
    elif action=='edit':
        from conclusions import request_conclusion
        if not db.execute('SELECT 1 FROM conclusion_drafts WHERE task=? AND chat=?',(tid,chat)).fetchone():
            emit(db,queue,chat,'Сначала подготовь заключение.')
        else:
            db.execute('INSERT INTO conclusion_edits(chat,uid,task) VALUES(?,?,?) ON CONFLICT(chat,uid) DO UPDATE SET task=excluded.task,created=CURRENT_TIMESTAMP',(chat,uid,tid))
            emit(db,queue,chat,f'Кейс №{tid}: следующим сообщением напиши, что изменить. Для отмены /cancelconclusion.')
    return True
