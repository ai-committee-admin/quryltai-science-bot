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
    if 'archived' not in {c[1] for c in db.execute('PRAGMA table_info(council_tasks)')}:
        db.execute('ALTER TABLE council_tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
    from recommendations import initialize
    initialize(db)
    from recommendation_import import initialize as initialize_import
    initialize_import(db)


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
    text=f'Кейс №{tid} · ' + ('В архиве' if task['archived'] else '') + ' ' + ('Обсуждение завершено' if task['closed'] else 'Обсуждение открыто') + '\n' + preview + '\nМнения до: ' + (task['deadline'] or 'дата не указана')
    text+='\n\n' + ('\n'.join(f'{r["name"][:50]}: {STATES[r["state"]]}' for r in members[:6]) or 'Участников пока нет.')
    if len(members)>6:text+=f'\nВсего участников: {len(members)}.'
    text+='\n\nПолный текст и файлы — «Материалы и мнения».'
    if task['archived']:
        emit(db,queue,task['chat'],text,[[('Восстановить',f'g:restore:{tid}')]],tid)
        return
    buttons=[]  # Responses to linked case messages are recorded automatically.
    buttons += [[('Подобрать экспертов',f'g:recommend:{tid}'),('Похожие кейсы',f'g:similar:{tid}')]]
    buttons += [[('Подготовить справку',f'g:brief:{tid}')]]
    buttons += [[('Вопросы для обсуждения',f'g:questions:{tid}'),('Подвести итоги',f'g:summary:{tid}')]]
    buttons += [[('Сформировать заключение',f'g:conclusion:{tid}'),('Скачать Word',f'g:wordconclusion:{tid}')]]
    buttons += [[('Рекомендации',f'g:reclist:{tid}'),('Из заключения',f'g:recpreview:{tid}')]]
    buttons += [[('Доработать заключение',f'g:editconclusion:{tid}')]]
    buttons += [[('Материалы и мнения',f'g:results:{tid}'),('Обновить карточку',f'g:card:{tid}')]]
    if not task['closed']:
        buttons += [[('Завершить обсуждение',f'g:close:{tid}')]]
    buttons += [[('Удалить кейс',f'g:archive:{tid}')]]
    if existing and existing.get('message_id') and len(text)<=3500:
        markup={'inline_keyboard':[[{'text':label,'callback_data':data} for label,data in row] for row in buttons]}
        if existing.get('text')!=text or existing.get('reply_markup')!=markup:
            queue(db,'editMessageText',{'chat_id':task['chat'],'message_id':existing['message_id'],
                  'text':text,'reply_markup':markup,'_council':True,'_council_task':tid})
    else:
        emit(db,queue,task['chat'],text,buttons,tid)


def attach(db, queue, task, uid, m, chair, automatic=False):
    tid=task['id']; chat=task['chat']
    member=db.execute('SELECT * FROM council_members WHERE task=? AND uid=?',(tid,uid)).fetchone()
    # A chair participating as an expert submits results like any other expert.
    if task['closed'] or task['archived']:
        if not automatic: emit(db,queue,chat,'Обсуждение закрыто. Материал не добавлен.')
        return
    if automatic and not member:
        name=' '.join(str(m.get('from',{}).get(k,'') or '') for k in ('first_name','last_name')).strip() or str(uid)
        db.execute('INSERT OR IGNORE INTO council_members(task,uid,name) VALUES(?,?,?)',(tid,uid,name))
        member=db.execute('SELECT * FROM council_members WHERE task=? AND uid=?',(tid,uid)).fetchone()
    if member:
        if not automatic and member['state'] not in ('working','revision'):
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
    if automatic and member and rev>0:
        db.execute("UPDATE council_members SET state='submitted' WHERE task=? AND uid=?",(tid,uid))



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
        queue(db,'sendMessage',{'chat_id':cid,'text':'Кейсы включены. Для нового кейса начните сообщение с «Кейс:». /analytics — сводный анализ науки; /grouphelp — помощь.' if cmd=='/council_on' else 'Приём кейсов в этом чате отключён.'})
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
        if action=='recimport':
            if not chair: feedback('Только администратор или ведущий.')
            else:
                from recommendation_import import approve
                feedback(approve(db,cid,uid,tid))
            return True
        if action in ('recview','recstatus'):
            if not chair:feedback('Только администратор или ведущий.')
            else:
                from recommendations import show,update_field
                if action=='recstatus':
                    if len(nums)!=2:feedback('Ошибка команды.');return True
                    feedback(update_field(db,cid,tid,uid,'status',nums[1]))
                text2,buttons2=show(db,cid,tid)
                if text2:emit(db,queue,cid,text2,buttons2)
                else:feedback('Рекомендация не найдена.')
            return True
        if action=='analyticword':
            if not chair:feedback('Только администратор или ведущий.')
            else:
                from science_analytics import send_word
                feedback(send_word(db,queue,cid,tid))
            return True
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
                tasks=db.execute("SELECT t.* FROM council_tasks t JOIN council_members m ON m.task=t.id WHERE t.chat=? AND t.closed=0 AND t.archived=0 AND m.uid=? AND m.state IN ('working','revision') ORDER BY t.id LIMIT 11 OFFSET ?",(cid,uid,offset)).fetchall()
                buttons=[[(f'№{t["id"]}: {t["body"][:40]}',f'g:bind:{tid}:{t["id"]}')] for t in tasks[:10]]
                if len(tasks)>10:buttons.append([('Далее',f'g:choose:{tid}:{offset+10}')])
                emit(db,queue,cid,'Выберите кейс для этого сообщения.' if tasks else 'Нет активных кейсов с вашим участием. Нажмите «Подключиться» в нужном кейсе.',buttons)
            elif action=='bind' and len(nums)==2:
                task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(nums[1],cid)).fetchone()
                member=db.execute("SELECT 1 FROM council_members WHERE task=? AND uid=? AND state IN ('working','revision')",(nums[1],uid)).fetchone()
                if task and not task['closed'] and not task['archived'] and member:
                    attach(db,queue,task,uid,original,chair)
                    db.execute('UPDATE council_pending SET used=1,payload=\'{}\' WHERE id=?',(tid,))
            return True
        task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(tid,cid)).fetchone()
        if not task:return True
        if task['archived'] and action not in ('restore',):
            feedback('Кейс находится в архиве.');return True
        member=db.execute('SELECT * FROM council_members WHERE task=? AND uid=?',(tid,uid)).fetchone()
        if action=='archive':
            if chair: emit(db,queue,cid,f'Переместить кейс №{tid} в архив? Мнения и файлы сохранятся.',[[('Да, в архив',f'g:confirmarchive:{tid}'),('Отмена',f'g:card:{tid}')]])
            else: feedback('Только администратор или ведущий.')
        elif action=='confirmarchive':
            if chair:
                db.execute('UPDATE council_tasks SET archived=1 WHERE id=? AND chat=?',(tid,cid))
                emit(db,queue,cid,f'Кейс №{tid} перенесён в архив. Для восстановления: /archivedcases')
            else:feedback('Нет прав.')
        elif action=='restore':
            if chair:
                db.execute('UPDATE council_tasks SET archived=0 WHERE id=? AND chat=?',(tid,cid))
                emit(db,queue,cid,f'Кейс №{tid} восстановлен.')
                task=db.execute('SELECT * FROM council_tasks WHERE id=?',(tid,)).fetchone()
                task_card(db,queue,task)
            else:feedback('Нет прав.')
        elif action=='recpreview':
            if not chair:feedback('Только администратор или ведущий.')
            else:
                from recommendation_import import prepare
                result,buttons=prepare(db,cid,tid)
                emit(db,queue,cid,result,buttons)
        elif action=='reclist':
            if not chair:feedback('Только администратор или ведущий.')
            else:
                from recommendations import list_items
                text2,buttons2=list_items(db,cid,tid)
                emit(db,queue,cid,text2,buttons2)
        elif action=='similar':
            if chair:
                from semantic_knowledge import request
                feedback(request(db,cid,uid,task['body'][:1000],exclude=tid))
            else:feedback('Поиск по базе знаний доступен ведущему или администратору.')
        elif action=='brief':
            if not chair:
                feedback('Справку запрашивает депутат или администратор.')
            else:
                from case_brief import request_brief
                feedback(request_brief(db,task,uid))
        elif action in ('questions','summary'):
            if not chair:
                feedback('Доступно депутату или администратору.')
            else:
                from moderator import request_moderation
                feedback(request_moderation(db,task,uid,action))
        elif action in ('conclusion','wordconclusion','editconclusion'):
            if not chair:
                feedback('Доступно только ведущему или администратору.')
            else:
                from conclusions import request_conclusion,export_conclusion
                if action=='conclusion':feedback(request_conclusion(db,task,uid,'draft'))
                elif action=='wordconclusion':feedback(export_conclusion(db,queue,task))
                else:
                    if not db.execute('SELECT 1 FROM conclusion_drafts WHERE task=?',(tid,)).fetchone():
                        feedback('Сначала сформируйте заключение.')
                    else:
                        db.execute('INSERT INTO conclusion_edits(chat,uid,task) VALUES(?,?,?) ON CONFLICT(chat,uid) DO UPDATE SET task=excluded.task,created=CURRENT_TIMESTAMP',(cid,uid,tid))
                        emit(db,queue,cid,f'Кейс №{tid}: напишите одним сообщением, что изменить в заключении. Для отмены: /cancelconclusion. Это сообщение не попадёт в мнения экспертов.')
        elif action=='recommend':
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
    # Explicit edit mode: consume the administrator's next message, never record it as an opinion.
    if chair and cmd=='/cancelconclusion':
        db.execute('DELETE FROM conclusion_edits WHERE chat=? AND uid=?',(cid,uid))
        emit(db,queue,cid,'Доработка заключения отменена.');return True
    if chair and not cmd.startswith('/'):
        pending_edit=db.execute('SELECT * FROM conclusion_edits WHERE chat=? AND uid=?',(cid,uid)).fetchone()
        if pending_edit:
            db.execute('DELETE FROM conclusion_edits WHERE chat=? AND uid=?',(cid,uid))
            target=db.execute('SELECT * FROM council_tasks WHERE chat=? AND id=?',(cid,pending_edit['task'])).fetchone()
            from conclusions import request_conclusion
            if target:
                emit(db,queue,cid,request_conclusion(db,target,uid,'edit',text))
            return True
    from expert_registry import handle_group as handle_expert_candidates
    if not cb and handle_expert_candidates(db,admins,update,queue):return True
    from zhanel import handle as handle_zhanel
    if not cb and handle_zhanel(db,queue,cid,uid,m,admins):return True
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
    if cmd=='/recfrom':
        if not chair:emit(db,queue,cid,'Только администратор или ведущий.')
        elif len(parts)<2 or not parts[1].isdigit():emit(db,queue,cid,'Пример: /recfrom 1')
        else:
            from recommendation_import import prepare
            result,buttons=prepare(db,cid,int(parts[1]))
            emit(db,queue,cid,result,buttons)
        return True
    if cmd in ('/recommendations','/rec','/recadd','/recorg','/recdate','/recnote','/rechistory'):
        if not chair:
            emit(db,queue,cid,'Рекомендации доступны только администратору или ведущему.');return True
        from recommendations import add,update_field,show,list_items,history
        if cmd=='/recommendations':
            taskid=int(parts[1]) if len(parts)>1 and parts[1].isdigit() else None
            result,buttons=list_items(db,cid,taskid)
            emit(db,queue,cid,result,buttons)
        elif cmd=='/recadd':
            if len(parts)<3 or not parts[1].isdigit():emit(db,queue,cid,'Пример: /recadd 1 Предусмотреть базовое финансирование лабораторий.')
            else:emit(db,queue,cid,add(db,cid,int(parts[1]),uid,text.split(None,2)[2]))
        elif len(parts)<2 or not parts[1].isdigit():
            emit(db,queue,cid,'Нужен номер рекомендации. Например: /rec 1')
        else:
            rid=int(parts[1])
            if cmd=='/rec':
                result,buttons=show(db,cid,rid)
                emit(db,queue,cid,result or 'Рекомендация не найдена.',buttons)
            elif cmd=='/rechistory':emit(db,queue,cid,history(db,cid,rid))
            else:
                if len(parts)<3:emit(db,queue,cid,'После номера укажи значение.')
                else:
                    field={'/recorg':'agency','/recdate':'due','/recnote':'note'}[cmd]
                    emit(db,queue,cid,update_field(db,cid,rid,uid,field,text.split(None,2)[2]))
        return True
    if cmd=='/analytics':
        if not chair:
            emit(db,queue,cid,'Сводная аналитика доступна только администратору или ведущему.')
        else:
            from science_analytics import request
            emit(db,queue,cid,request(db,cid,uid))
        return True
    if cmd=='/reportword':
        if not chair:
            emit(db,queue,cid,'Выгрузка доступна только администратору или ведущему.')
        else:
            from science_analytics import send_word
            try:report_id=int(parts[1])
            except (IndexError,ValueError):
                emit(db,queue,cid,'Формат: /reportword НОМЕР')
                return True
            emit(db,queue,cid,send_word(db,queue,cid,report_id))
        return True
    if cmd in ('/knowledge','/findcase'):
        if not chair:
            emit(db,queue,cid,'Поиск по базе знаний доступен только ведущему или администратору.');return True
        query=text.split(None,1)[1].strip() if len(parts)>1 else ''
        if len(query)<3:
            emit(db,queue,cid,'Пример: /knowledge грантовое финансирование науки');return True
        if cmd=='/findcase':
            from knowledge import display
            display(db,queue,cid,query[:500]);return True
        from semantic_knowledge import request
        emit(db,queue,cid,request(db,cid,uid,query[:500]));return True
    if cmd=='/similar':
        if not chair:
            emit(db,queue,cid,'Поиск доступен только ведущему или администратору.');return True
        try:tid=int(parts[1])
        except (IndexError,ValueError):
            emit(db,queue,cid,'Пример: /similar 12');return True
        old=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=? AND archived=0',(tid,cid)).fetchone()
        if not old:
            emit(db,queue,cid,'Активный кейс в этом чате не найден.');return True
        from semantic_knowledge import request
        emit(db,queue,cid,request(db,cid,uid,old['body'][:1000],exclude=tid));return True
    if cmd=='/archivedcases':
        if not chair:
            emit(db,queue,cid,'Архив доступен только ведущему или администратору.');return True
        rows=db.execute('SELECT * FROM council_tasks WHERE chat=? AND archived=1 ORDER BY id DESC LIMIT 30',(cid,)).fetchall()
        emit(db,queue,cid,'Кейсы в архиве:' if rows else 'Архив кейсов пуст.',[[(f'Восстановить №{t["id"]}',f'g:restore:{t["id"]}')] for t in rows]);return True
    if cmd=='/tasks':
        offset=int(parts[1]) if len(parts)==2 and parts[1].isdigit() else 0
        rows=db.execute('SELECT * FROM council_tasks WHERE chat=? AND archived=0 ORDER BY id DESC LIMIT 10 OFFSET ?',(cid,offset)).fetchall()
        emit(db,queue,cid,'Кейсы этого чата. Следующая страница: /cases '+str(offset+10),[[(f'№{t["id"]}: {t["body"][:45]}',f'g:card:{t["id"]}')] for t in rows]);return True
    if cmd=='/grouphelp':
        emit(db,queue,cid,'Новый кейс: начните сообщение с «Кейс:» или отправьте /case ответом на текст/документ.\nЭксперт: ответьте текстом на карточку кейса — мнение сохранится автоматически. Материалы сохраняются без отдельных уведомлений.\nДля привязки ранее отправленного сообщения: /attach ответом на своё сообщение.\n/cases — кейсы; /archivedcases — архив; /due НОМЕР ДД.ММ.ГГГГ — срок мнений.\n/recommendations — контроль рекомендаций; /recadd НОМЕР текст — добавить рекомендацию.');return True
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
        if task and not task['archived']:attach(db,queue,task,uid,m,chair,automatic=True)
    return True


def schedule_groups(db,queue,now):
    if now.hour<9:return
    from recommendations import reminders
    reminders(db,queue,now,emit)
    day=now.date().isoformat()
    rows=db.execute("SELECT t.*,m.uid,m.name FROM council_tasks t JOIN council_groups g ON g.chat=t.chat JOIN council_members m ON m.task=t.id WHERE g.enabled=1 AND t.closed=0 AND t.archived=0 AND t.deadline!='' AND t.deadline<=? AND m.state IN ('working','revision')",(day,)).fetchall()
    grouped={}
    for r in rows:
        if db.execute('INSERT OR IGNORE INTO council_reminders VALUES(?,?,?)',(r['id'],r['uid'],day)).rowcount:
            grouped.setdefault((r['chat'],r['id'],r['deadline']),[]).append(r['name'])
    for (chat,tid,deadline),names in grouped.items():
        emit(db,queue,chat,f'№{tid} · '+('мнения ожидаются сегодня' if deadline==day else 'ожидаем мнение')+'\nУчастники: '+', '.join(names),task=tid)
