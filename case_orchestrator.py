"""v4.4: Human-controlled case workflow with scoped progress and explicit creation.

The plan does not create records, send invitations, or establish conclusions before
an administrator approves. All queries are limited to the originating council chat.
"""
import re
from datetime import datetime, timezone, timedelta

START = re.compile(
    r'^\s*(?:организуй|организовать|запусти рассмотрение|начни работу над|'
    r'нужно решить проблему|рассмотреть проблему|'
    r'(?:создай|создать|создайте)\s+(?:(?:новый|научный)\s+)?кейс\b|'
    r'открой новый\s+кейс\b|открыть новый\s+кейс\b|'
    r'новый кейс\b|начни новый\s+кейс\b|'
    r'ұйымдастыр|қарастыруды баста|жаңа кейс аш|кейс құр)', re.I)
NEGATED_CREATE = re.compile(r'^\s*(?:не|не надо|не нужно|пока не|не следует)\s+(?:созда|открыва|запуска)', re.I)
PROGRESS = re.compile(r'(?:что дальше|следующий шаг|на каком этапе|статус (?:работы|кейса)|план по кейсу|ход работы|какой этап|не істейміз|келесі қадам)', re.I)


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS case_workflow_proposals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER NOT NULL,
        requester INTEGER NOT NULL, issue TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        task_id INTEGER)''')


def topic_from_request(raw):
    text=raw.strip()
    text=START.sub('',text,count=1).strip(' :,.—-')
    text=re.sub(r'^(?:новый\s+)?(?:кейс|кейс\s+по|обсуждение)\s*[:—-]?\s*','',text,flags=re.I)
    text=re.sub(r'^(?:о|об|по|на тему|вопрос|проблему|проблемы|проблеме)\s+','',text,flags=re.I)
    return text.strip(' :,.—-')[:2000]


def related(db,chat,issue,exclude=None):
    from knowledge import search
    return search(db,chat,issue,exclude=exclude,limit=3)


def candidates(db,issue):
    try:
        from expert_registry import rank
        return rank(db,issue,limit=3)
    except Exception:
        return []


def prepare(db,queue,chat,uid,raw,emit):
    issue=topic_from_request(raw)
    if len(issue)<15:
        emit(db,queue,chat,'Опиши проблему одним-двумя предложениями. Например: «Жанель, организуй рассмотрение проблемы задержек финансирования научных проектов».')
        return True
    if len(issue)>1500:
        emit(db,queue,chat,'Для начала достаточно краткого описания проблемы до 1500 символов.')
        return True
    initialize(db)
    # Invalidate earlier unconfirmed proposals by the same actor in this group.
    db.execute("UPDATE case_workflow_proposals SET state='cancelled' WHERE chat=? AND requester=? AND state='pending'",(chat,uid))
    now=datetime.now(timezone.utc)
    pid=db.execute('''INSERT INTO case_workflow_proposals(chat,requester,issue,created_at,expires_at)
                       VALUES(?,?,?,?,?)''',(chat,uid,issue,now.isoformat(),(now+timedelta(hours=24)).isoformat())).lastrowid
    found=related(db,chat,issue)
    names=', '.join('№'+str(x['id']) for x in found) if found else 'близких записей пока не найдено'
    from expert_matching import find
    approved,applicants=find(db,issue,chat=chat,limit=3)
    def brief(items,is_profile):
        return ', '.join((f'№{p["id"]} {p["name"]}' if is_profile else p['name']) for _,p,_ in items) or 'нет тематических совпадений'
    titles=', '.join(f'№{x["id"]}: {x["body"][:65]}' for x in found) if found else 'похожих кейсов не найдено'
    text=(f'Предлагаю открыть кейс по проблеме:\n«{issue[:700]}»\n\n'
          f'Похожие кейсы: {titles}.\n'
          f'Утверждённые эксперты: {brief(approved,True)}.\n'
          f'Кандидаты из заявок (не утверждены): {brief(applicants,False)}.\n'
          'Подбор предварительный, без независимой проверки квалификации. '
          'После создания я предложу вопросы, сбор мнений и подготовку заключения. '
          'Никого автоматически не приглашаю.\n\nСоздать кейс?')
    emit(db,queue,chat,text,[[('Создать кейс',f'g:plancreate:{pid}'),('Отмена',f'g:plancancel:{pid}')]])
    return True


def confirm(db,queue,chat,uid,action,proposal_id,chair,emit):
    if not chair:return 'Действие доступно только администратору или ведущему.'
    initialize(db)
    row=db.execute('SELECT * FROM case_workflow_proposals WHERE id=? AND chat=? AND requester=?',(proposal_id,chat,uid)).fetchone()
    if not row or row['state']!='pending':return 'Предложение уже обработано либо недоступно.'
    if row['expires_at']<=datetime.now(timezone.utc).isoformat():
        db.execute("UPDATE case_workflow_proposals SET state='expired' WHERE id=?",(proposal_id,))
        return 'Срок подтверждения истёк. Попроси Жанель подготовить новый план.'
    if action=='plancancel':
        db.execute("UPDATE case_workflow_proposals SET state='cancelled' WHERE id=?",(proposal_id,))
        return 'Создание кейса отменено.'
    if action!='plancreate':return 'Неизвестное действие.'
    task_id=db.execute('INSERT INTO council_tasks(chat,creator,body) VALUES(?,?,?)',(chat,uid,row['issue'])).lastrowid
    db.execute("UPDATE case_workflow_proposals SET state='created',task_id=? WHERE id=?",(task_id,proposal_id))
    from group_tasks import task_card
    task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(task_id,chat)).fetchone()
    task_card(db,queue,task)
    emit(db,queue,chat,f'Кейс №{task_id} создан. Следующий шаг — уточнить вопросы для экспертов и собрать мнения. Можно написать «Жанель, что дальше по кейсу №{task_id}».')
    return 'Кейс создан.'


def progress(db,queue,chat,uid,task,emit):
    if not task:
        emit(db,queue,chat,'Не поняла, какой кейс проверить. Укажи номер: «Жанель, что дальше по кейсу №7».')
        return True
    tid=task['id']
    if task['archived']:
        emit(db,queue,chat,f'Кейс №{tid} в архиве. Для продолжения его нужно восстановить через /archivedcases.')
        return True
    opinions=db.execute('''SELECT COUNT(*) FROM council_materials WHERE task=? AND revision>0 AND TRIM(COALESCE(body,''))!='' ''',(tid,)).fetchone()[0]
    members=db.execute("SELECT COUNT(*) FROM council_members WHERE task=? AND state IN ('submitted','accepted')",(tid,)).fetchone()[0]
    draft=db.execute('SELECT version FROM conclusion_drafts WHERE task=? AND chat=?',(tid,chat)).fetchone()
    recs=db.execute('SELECT COUNT(*) FROM council_recommendations WHERE task=? AND chat=?',(tid,chat)).fetchone()[0]
    if not opinions:
        stage='Сбор экспертных мнений'
        next_step='Предложить вопросы для обсуждения и получить первые содержательные мнения.'
        buttons=[[('Вопросы для обсуждения',f'g:questions:{tid}'),('Подобрать экспертов',f'g:recommend:{tid}')]]
    elif not draft:
        stage='Мнения собраны — готовим проект заключения'
        next_step='Подвести итоги и подготовить проект заключения с позициями участников и разногласиями.'
        buttons=[[('Подвести итоги',f'g:summary:{tid}'),('Сформировать заключение',f'g:conclusion:{tid}')]]
    elif not recs:
        stage='Есть проект заключения — требуются проверка и рекомендации'
        next_step='Проверить проект человеком и выбрать рекомендации для постановки на контроль.'
        buttons=[[('Скачать Word',f'g:wordconclusion:{tid}'),('Из заключения',f'g:recpreview:{tid}')]]
    else:
        stage='Рекомендации поставлены на контроль'
        next_step='Уточнить адресатов, сроки и отслеживать результаты. Принятие решения советом автоматически не предполагается.'
        buttons=[[('Рекомендации',f'g:reclist:{tid}'),('Скачать Word',f'g:wordconclusion:{tid}')]]
    text=(f'Кейс №{tid}\nЭтап: {stage}\n'
          f'Текстовых мнений: {opinions}; участников с представленным мнением: {members}; '
          f'проект заключения: {"редакция "+str(draft[0]) if draft else "нет"}; '
          f'рекомендаций на контроле: {recs}.\n\nЧто дальше: {next_step}')
    emit(db,queue,chat,text,buttons)
    return True
