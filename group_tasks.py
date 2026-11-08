"""Opt-in council group workflow. No model calls; original files stay in Telegram."""
import json
import re
from datetime import datetime, timezone, timedelta

STATES = {'working':'Участвует', 'submitted':'Мнение представлено', 'accepted':'Учтено', 'revision':'Ожидаем уточнение'}


def initialize_groups(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS council_groups(chat INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS council_chairs(chat INTEGER, uid INTEGER, PRIMARY KEY(chat,uid));
    CREATE TABLE IF NOT EXISTS council_tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL, creator INTEGER NOT NULL, body TEXT NOT NULL, deadline TEXT NOT NULL DEFAULT '', closed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS council_members(task INTEGER REFERENCES council_tasks(id), uid INTEGER, name TEXT, state TEXT NOT NULL DEFAULT 'working', revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(task,uid));
    CREATE TABLE IF NOT EXISTS council_materials(id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER REFERENCES council_tasks(id), uid INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, body TEXT, file_id TEXT, name TEXT, message INTEGER NOT NULL, UNIQUE(task,message));
    CREATE TABLE IF NOT EXISTS council_links(chat INTEGER, message INTEGER, task INTEGER, PRIMARY KEY(chat,message));
    CREATE TABLE IF NOT EXISTS council_pending(id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, uid INTEGER, message INTEGER, payload TEXT, used INTEGER NOT NULL DEFAULT 0, UNIQUE(chat,message));
    CREATE TABLE IF NOT EXISTS council_reminders(task INTEGER, uid INTEGER, day TEXT, PRIMARY KEY(task,uid,day));
    ''')


def emit(db, queue, chat, text, buttons=(), task=None, reply=None):
    # All group output is bound to its originating chat, never another council.
    for i in range(0, len(text), 1700):
        p = {'chat_id':chat, 'text':text[i:i+1700]}
        if i == 0 and buttons:
            p['reply_markup'] = {'inline_keyboard':[[{'text':label,'callback_data':data} for label,data in row] for row in buttons]}
        if reply:
            p['reply_parameters'] = {'message_id':reply, 'allow_sending_without_reply':True}
        if task:
            p['_council_task'] = task
        p['_council'] = True
        queue(db, 'sendMessage', p)


def is_chair(db, chat, uid, admins):
    return uid in admins or bool(db.execute('SELECT 1 FROM council_chairs WHERE chat=? AND uid=?',(chat,uid)).fetchone())


def task_card(db, queue, task, existing=None):
    members = db.execute('SELECT * FROM council_members WHERE task=? ORDER BY uid',(task['id'],)).fetchall()
    tid=task['id']
    # Keep one card with its buttons. The unabridged source remains in materials.
    preview=task['body'][:700] + ('…' if len(task['body'])>700 else '')
    text=f'Кейс №{tid} · ' + ('Обсуждение завершено' if task['closed'] else 'Обсуждение открыто') + '\n' + preview + '\nМнения до: ' + (task['deadline'] or 'дата не указана')
    text+='\n\n' + ('\n'.join(f'{r["name"][:50]}: {STATES[r["state"]]}' for r in members[:6]) or 'Участников пока нет.')
    if len(members)>6:text+=f'\nВсего участников: {len(members)}.'
    text+='\n\nПолный текст и файлы — «Материалы и мнения».'
    buttons=[] if task['closed'] else [[('Подключиться',f'g:take:{tid}'),('Представить мнение',f'g:submit:{tid}')]]
    buttons += [[('Подобрать экспертов',f'g:recommend:{tid}')]]
    buttons += [[('Материалы и мнения',f'g:results:{tid}'),('Обновить карточку',f'g:card:{tid}')]]
    if not task['closed']:
        buttons += [[('Завершить обсуждение',f'g:close:{tid}')]]
    if existing and existing.get('message_id') and len(text)<=3500:
        markup={'inline_keyboard':[[{'text':label,'callback_data':data} for label,data in row] for row in buttons]}
        if existing.get('text')!=text or existing.get('reply_markup')!=markup:
            queue(db,'editMessageText',{'chat_id':task['chat'],'message_id':existing['message_id'],
                  'text':text,'reply_markup':markup,'_council':True,'_council_task':tid})
    else:
        emit(db,queue,task['chat'],text,buttons,tid)


def attach(db, queue, task, uid, m, chair):
    tid=task['id']; chat=task['chat']
    member=db.execute('SELECT * FROM council_members WHERE task=? AND uid=?',(tid,uid)).fetchone()
    # A chair participating as an expert submits results like any other expert.
    if task['closed']:
        emit(db,queue,chat,'Обсуждение завершено. Материал не добавлен.');return
    if member:
        if member['state'] not in ('working','revision'):
            emit(db,queue,chat,'Мнение уже представлено. Дополнения можно внести после запроса уточнения.');return
        rev=member['revision']
    elif chair:
        rev=0
    else:
        emit(db,queue,chat,'Сначала нажмите «Подключиться», затем отправьте материал повторно.');return
    doc=m.get('document') or {}
    body=m.get('text') or m.get('caption') or ''
    if not body and not doc.get('file_id'):
        return
    db.execute('INSERT OR IGNORE INTO council_materials(task,uid,revision,body,file_id,name,message) VALUES(?,?,?,?,?,?,?)',
               (tid,uid,rev,body,doc.get('file_id',''),doc.get('file_name','Документ'),m['message_id']))
    db.execute('INSERT OR IGNORE INTO council_links VALUES(?,?,?)',(chat,m['message_id'],tid))



def results(db,queue,task):
    tid=task['id'];chat=task['chat']
    emit(db,queue,chat,f'Материалы кейса №{tid}. Черновики отмечены отдельно.')
    for r in db.execute('SELECT * FROM council_materials WHERE task=? ORDER BY id',(tid,)).fetchall():
        member=db.execute('SELECT * FROM council_members WHERE task=? AND uid=?',(tid,r['uid'])).fetchone()
        label='Материалы депутата' if not r['revision'] else f'{member["name"]} · версия {r["revision"]} · ' + (STATES[member['state']] if r['revision']==member['revision'] else 'предыдущая версия')
        if r['body']:
            emit(db,queue,chat,label+'\n'+r['body'],task=tid)
        if r['file_id']:
            queue(db,'sendDocument',{'chat_id':chat,'document':r['file_id'],'caption':label[:900], '_council':True})
    for r in db.execute("SELECT * FROM council_members WHERE task=? AND state='submitted'",(tid,)).fetchall():
        suffix=f'{tid}:{r["uid"]}:{r["revision"]}'
        emit(db,queue,chat,f'Мнение эксперта: {r["name"]}, кейс №{tid}, версия {r["revision"]}.',
             [[('Учесть',f'g:accept:{suffix}'),('Запросить уточнение',f'g:revise:{suffix}')]],tid)


def handle_group(db,admins,update,queue):
    cb=update.get('callback_query');m=(cb or {}).get('message') if cb else update.get('message')
    m=m or {};chat=m.get('chat') or {}
    if chat.get('type') not in ('group','supergroup'):
        return False
    cid=chat['id'];actor=(cb or {}).get('from') if cb else m.get('from');actor=actor or {};uid=actor.get('id')
    if not isinstance(uid,int) or actor.get('is_bot') or m.get('sender_chat'):
        return True
    text=(m.get('text') or m.get('caption') or '').strip();parts=text.split();cmd=parts[0].split('@')[0] if parts else ''
    cmd={'/case':'/task','/cases':'/tasks'}.get(cmd,cmd)
    if not cb and cmd in ('/council_on','/council_off') and uid in admins:
        db.execute('INSERT INTO council_groups VALUES(?,?) ON CONFLICT(chat) DO UPDATE SET enabled=excluded.enabled',(cid,int(cmd=='/council_on')))
        # Plain output for off command must still be delivered.
        queue(db,'sendMessage',{'chat_id':cid,'text':'Кейсы включены. Для нового кейса начните сообщение с «Кейс:». /grouphelp — помощь.' if cmd=='/council_on' else 'Приём кейсов в этом чате отключён.'})
        return True
    enabled=db.execute('SELECT enabled FROM council_groups WHERE chat=?',(cid,)).fetchone()
    if not enabled or not enabled[0]:
        if cb: queue(db,'answerCallbackQuery',{'callback_query_id':cb['id'],'text':'Этот чат не подключён.'})
        return True
    chair=is_chair(db,cid,uid,admins)
    if cb:
        queue(db,'answerCallbackQuery',{'callback_query_id':cb['id']})
        ack_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
        def feedback(text):
            db.execute('UPDATE outbox SET payload=? WHERE id=?',
                       (json.dumps({'callback_query_id':cb['id'],'text':text},ensure_ascii=False),ack_id))
        data=cb.get('data','')
        if not re.fullmatch(r'g:[a-z]+:[0-9]+(?::[0-9]+){0,2}',data):return True
        _,action,*nums=data.split(':');nums=list(map(int,nums));tid=nums[0]
        if action in ('create','choose','bind','ignore'):
            p=db.execute('SELECT * FROM council_pending WHERE id=? AND chat=? AND uid=? AND used=0',(tid,cid,uid)).fetchone()
            if not p:
                emit(db,queue,cid,'Это сообщение уже обработано или принадлежит другому участнику.');return True
            original=json.loads(p['payload'])
            if action=='ignore':
                db.execute('UPDATE council_pending SET used=1,payload=\'{}\' WHERE id=?',(tid,));return True
            if action=='create' and chair:
                body=original.get('text') or original.get('caption') or (original.get('document') or {}).get('file_name') or 'Кейс'
                taskid=db.execute('INSERT INTO council_tasks(chat,creator,body) VALUES(?,?,?)',(cid,uid,body)).lastrowid
                task=db.execute('SELECT * FROM council_tasks WHERE id=?',(taskid,)).fetchone()
                # Save original source, including a document without a caption.
                attach(db,queue,task,uid,original,True)
                db.execute('UPDATE council_pending SET used=1,payload=\'{}\' WHERE id=?',(tid,))
                task_card(db,queue,task,m)
            elif action=='choose':
                offset=nums[1] if len(nums)>1 else 0
                tasks=db.execute("SELECT t.* FROM council_tasks t JOIN council_members m ON m.task=t.id WHERE t.chat=? AND t.closed=0 AND m.uid=? AND m.state IN ('working','revision') ORDER BY t.id LIMIT 11 OFFSET ?",(cid,uid,offset)).fetchall()
                buttons=[[(f'№{t["id"]}: {t["body"][:40]}',f'g:bind:{tid}:{t["id"]}')] for t in tasks[:10]]
                if len(tasks)>10:buttons.append([('Далее',f'g:choose:{tid}:{offset+10}')])
                emit(db,queue,cid,'Выберите кейс для этого сообщения.' if tasks else 'Нет активных кейсов с вашим участием. Нажмите «Подключиться» в нужном кейсе.',buttons)
            elif action=='bind' and len(nums)==2:
                task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(nums[1],cid)).fetchone()
                member=db.execute("SELECT 1 FROM council_members WHERE task=? AND uid=? AND state IN ('working','revision')",(nums[1],uid)).fetchone()
                if task and not task['closed'] and member:
                    attach(db,queue,task,uid,original,chair)
                    db.execute('UPDATE council_pending SET used=1,payload=\'{}\' WHERE id=?',(tid,))
            return True
        task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(tid,cid)).fetchone()
        if not task:return True
        member=db.execute('SELECT * FROM council_members WHERE task=? AND uid=?',(tid,uid)).fetchone()
        if action=='recommend':
            from expert_profiles import group_profiles
            group_profiles(db,queue,cid,uid,f'/recommend {tid}')
        elif action=='card':task_card(db,queue,task,m)
        elif action=='results':results(db,queue,task)
        elif task['closed']:emit(db,queue,cid,'Обсуждение завершено.')
        elif action=='take':
            name=' '.join(actor.get(k,'') for k in ('first_name','last_name')).strip() or str(uid)
            added=db.execute('INSERT OR IGNORE INTO council_members(task,uid,name) VALUES(?,?,?)',(tid,uid,name)).rowcount
            feedback('Вы подключились. Ответьте на карточку текстом или файлом.' if added else 'Вы уже участвуете в этом кейсе.')
            if added:task_card(db,queue,task,m)
        elif action=='submit':
            if not member or member['state'] not in ('working','revision'):
                emit(db,queue,cid,'Нажмите «Подключиться». Уже представленное мнение повторно отправлять не нужно.');return True
            if not db.execute('SELECT 1 FROM council_materials WHERE task=? AND uid=? AND revision=?',(tid,uid,member['revision'])).fetchone():
                emit(db,queue,cid,'Сначала отправьте текст или документ ответом на карточку кейса.');return True
            db.execute("UPDATE council_members SET state='submitted' WHERE task=? AND uid=?",(tid,uid))
            emit(db,queue,cid,f'{member["name"]} · кейс №{tid}: мнение представлено.',
                 [[('Посмотреть мнения',f'g:results:{tid}')]],tid)
        elif action in ('accept','revise') and len(nums)==3 and chair:
            target,rev=nums[1:]
            changed=db.execute("UPDATE council_members SET state=?,revision=revision+? WHERE task=? AND uid=? AND state='submitted' AND revision=?",('accepted' if action=='accept' else 'revision',int(action=='revise'),tid,target,rev)).rowcount
            if changed:emit(db,queue,cid,f'Кейс №{tid}, эксперт {target}: '+('мнение учтено.' if action=='accept' else 'ожидаем уточнение.'),task=tid)
            else:emit(db,queue,cid,'Эта версия уже рассмотрена. Откройте актуальные мнения.')
        elif action=='close' and chair:
            emit(db,queue,cid,'Завершить обсуждение для всех экспертов?',[[('Завершить обсуждение',f'g:confirm:{tid}')]],tid)
        elif action=='confirm' and chair:
            db.execute('UPDATE council_tasks SET closed=1 WHERE id=?',(tid,));emit(db,queue,cid,f'Обсуждение кейса №{tid} завершено.')
        elif action in ('accept','revise','close','confirm'):
            emit(db,queue,cid,'Это действие доступно депутату или администратору бота.')
        return True
    from expert_profiles import group_profiles
    if group_profiles(db,queue,cid,uid,text):return True
    if cmd in ('/chair','/unchair') and uid in admins:
        target=(m.get('reply_to_message') or {}).get('from') or {}
        targetid=target.get('id')
        if len(parts)==2 and parts[1].isdigit():targetid=int(parts[1])
        if isinstance(targetid,int) and not target.get('is_bot'):
            if cmd=='/chair':db.execute('INSERT OR IGNORE INTO council_chairs VALUES(?,?)',(cid,targetid))
            else:db.execute('DELETE FROM council_chairs WHERE chat=? AND uid=?',(cid,targetid))
            emit(db,queue,cid,'Права ведения обсуждений '+('выданы.' if cmd=='/chair' else 'сняты.'))
        else:emit(db,queue,cid,'Отправьте /chair ответом на сообщение депутата или /chair TELEGRAM_ID.')
        return True
    if cmd=='/due' and chair:
        try:
            tid=int(parts[1]);date=datetime.strptime(parts[2],'%d.%m.%Y').date().isoformat()
            changed=db.execute('UPDATE council_tasks SET deadline=? WHERE id=? AND chat=? AND closed=0',(date,tid,cid)).rowcount
            emit(db,queue,cid,'Мнения до: '+parts[2] if changed else 'Открытый кейс не найден.')
        except (ValueError,IndexError):emit(db,queue,cid,'Формат: /due 1 15.11.2026. Мнения ожидаются до конца указанного дня по времени Астаны.')
        return True
    if cmd=='/tasks':
        offset=int(parts[1]) if len(parts)==2 and parts[1].isdigit() else 0
        rows=db.execute('SELECT * FROM council_tasks WHERE chat=? ORDER BY id DESC LIMIT 10 OFFSET ?',(cid,offset)).fetchall()
        emit(db,queue,cid,'Кейсы этого чата. Следующая страница: /cases '+str(offset+10),[[(f'№{t["id"]}: {t["body"][:45]}',f'g:card:{t["id"]}')] for t in rows]);return True
    if cmd=='/grouphelp':
        emit(db,queue,cid,'Новый кейс: начните сообщение с «Кейс:» или отправьте /case ответом на текст/документ.\nЭксперт: «Подключиться» → ответ на карточку текстом/файлом → «Представить мнение». Материалы сохраняются без отдельных уведомлений.\nДля привязки ранее отправленного сообщения: /attach ответом на своё сообщение.\n/cases — кейсы; /due НОМЕР ДД.ММ.ГГГГ — дата для представления мнений.');return True
    # Deliberate intake replaces a prompt under every ordinary chat message.
    deliberate_task = chair and (cmd=='/task' or text.casefold().startswith(('кейс:','задание:')))
    deliberate_attach = cmd=='/attach'
    if deliberate_task or deliberate_attach:
        original=m.get('reply_to_message') if cmd in ('/task','/attach') else m
        if not original and cmd=='/task' and m.get('document'):
            original=m
        if not original and cmd=='/task' and len(parts)>1:
            original=dict(m,text=text.split(None,1)[1])
        if not original or not original.get('message_id') or not (original.get('text') or original.get('document')):
            emit(db,queue,cid,'Отправьте команду ответом на нужный текст или документ.');return True
        if deliberate_attach and (original.get('from',{}).get('id')!=uid or original.get('sender_chat')):
            emit(db,queue,cid,'Можно привязать только своё сообщение.');return True
        if db.execute('SELECT 1 FROM council_links WHERE chat=? AND message=?',(cid,original['message_id'])).fetchone():
            emit(db,queue,cid,'Это сообщение уже относится к кейсу.');return True
        db.execute('INSERT OR IGNORE INTO council_pending(chat,uid,message,payload) VALUES(?,?,?,?)',
                   (cid,uid,original['message_id'],json.dumps({k:original[k] for k in ('text','caption','document','message_id') if k in original},ensure_ascii=False)))
        pending=db.execute('SELECT * FROM council_pending WHERE chat=? AND message=?',(cid,original['message_id'])).fetchone()
        if pending['uid']!=uid or pending['used']:
            emit(db,queue,cid,'Это сообщение уже обработано.');return True
        pid=pending['id']
        buttons=[[('Открыть кейс',f'g:create:{pid}')]] if deliberate_task else [[('Добавить к кейсу',f'g:choose:{pid}')]]
        emit(db,queue,cid,'Открыть кейс?' if deliberate_task else 'Выберите кейс для материала.',buttons,reply=original['message_id'])
        return True
    if cmd.startswith('/') or not m.get('message_id'):return True
    if not (m.get('text') or m.get('document')):return True
    reply=(m.get('reply_to_message') or {}).get('message_id')
    linked=db.execute('SELECT task FROM council_links WHERE chat=? AND message=?',(cid,reply)).fetchone() if reply else None
    if linked:
        task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(linked[0],cid)).fetchone()
        if task:attach(db,queue,task,uid,m,chair)
    return True


def schedule_groups(db,queue,now):
    if now.hour<9:return
    day=now.date().isoformat()
    rows=db.execute("SELECT t.*,m.uid,m.name FROM council_tasks t JOIN council_groups g ON g.chat=t.chat JOIN council_members m ON m.task=t.id WHERE g.enabled=1 AND t.closed=0 AND t.deadline!='' AND t.deadline<=? AND m.state IN ('working','revision')",(day,)).fetchall()
    grouped={}
    for r in rows:
        if db.execute('INSERT OR IGNORE INTO council_reminders VALUES(?,?,?)',(r['id'],r['uid'],day)).rowcount:
            grouped.setdefault((r['chat'],r['id'],r['deadline']),[]).append(r['name'])
    for (chat,tid,deadline),names in grouped.items():
        emit(db,queue,chat,f'№{tid} · '+('мнения ожидаются сегодня' if deadline==day else 'ожидаем мнение')+'\nУчастники: '+', '.join(names),task=tid)
