"""CV-derived profile drafts with account-bound confirmation and council scope."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

FIELDS={'name':'ФИО','role':'Работа и должность','topics':'Темы','experience':'Опыт','education':'Образование'}


def initialize_profiles(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS expert_profiles(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, role TEXT NOT NULL,
      topics TEXT NOT NULL, experience TEXT NOT NULL, education TEXT NOT NULL,
      source_file TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL DEFAULT '',
      target INTEGER, chat INTEGER, confirmed INTEGER NOT NULL DEFAULT 0,
      revision INTEGER NOT NULL DEFAULT 1, updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE UNIQUE INDEX IF NOT EXISTS expert_council_identity ON expert_profiles(target,chat) WHERE target IS NOT NULL AND chat IS NOT NULL;
    CREATE TABLE IF NOT EXISTS expert_invites(hash TEXT PRIMARY KEY, profile INTEGER NOT NULL REFERENCES expert_profiles(id), target INTEGER NOT NULL, expires TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS expert_uploads(uid INTEGER PRIMARY KEY, profile INTEGER NOT NULL REFERENCES expert_profiles(id));
    ''')


def send(db,queue,uid,text,buttons=()):
    for i in range(0,len(text),1700):
        p={'chat_id':uid,'text':text[i:i+1700]}
        # Confirmation buttons follow the entire profile, not its first fragment.
        if i+1700>=len(text) and buttons:
            p['reply_markup']={'inline_keyboard':[[{'text':a,'callback_data':b} for a,b in row] for row in buttons]}
        queue(db,'sendMessage',p)


def profile_text(p,private=False):
    text=f'Профиль №{p["id"]} · '+('Подтверждён экспертом' if p['confirmed'] else ('По резюме, привязан администратором' if p['published'] else 'Черновик — не подтверждён'))
    for key,label in FIELDS.items():text+=f'\n\n{label}: {p[key]}'
    text+='\n\nСведения по резюме и подтверждению владельца. Независимая проверка квалификации не проводилась.'
    if private:
        text+=f'\nРезюме: {p["source_name"] or "не прикреплено"}\nЧат совета: {p["chat"] or "не назначен"}\nОбновлён: {p["updated"]} UTC.'
        text+='\nПри публикации администратором или подтверждении владельцем ФИО, должность, темы, опыт и образование доступны участникам указанного чата. Исходное резюме остаётся в личном доступе эксперта и администраторов бота.'
    return text


def review(db,queue,uid,p):
    send(db,queue,uid,profile_text(p,True),[[('Подтвердить профиль',f'ep:confirm:{p["id"]}:{p["revision"]}')],[('Как исправить',f'ep:edit:{p["id"]}:{p["revision"]}'),('Скрыть профиль',f'ep:hide:{p["id"]}:{p["revision"]}')]])


def now():return datetime.now(timezone.utc)


def handle_profiles(db,admins,update,queue):
    cb=update.get('callback_query');m=(cb or {}).get('message') if cb else update.get('message');m=m or {}
    chat=m.get('chat') or {};actor=(cb or {}).get('from') if cb else m.get('from');actor=actor or {};uid=actor.get('id')
    if chat.get('type')!='private' or chat.get('id')!=uid or actor.get('is_bot'):return False
    if cb:
        match=re.fullmatch(r'ep:(confirm|edit|hide):([0-9]+):([0-9]+)',cb.get('data',''))
        if not match:return False
        queue(db,'answerCallbackQuery',{'callback_query_id':cb['id'],'text':'Проверяю профиль…'})
        action,pid,rev=match.groups()
        p=db.execute('SELECT * FROM expert_profiles WHERE id=? AND target=?',(pid,uid)).fetchone()
        if not p:send(db,queue,uid,'Этот профиль не привязан к вашему аккаунту.');return True
        if action=='edit':
            send(db,queue,uid,f'Для изменения отправьте /profile_edit {pid} topics | Новые темы через запятую\nПоля: name, role, topics, experience, education. После правки подтвердите профиль повторно.');return True
        if action=='hide':
            db.execute('UPDATE expert_profiles SET confirmed=0,published=0,revision=revision+1 WHERE id=?',(pid,));send(db,queue,uid,'Профиль скрыт из списка и подбора. Ваше участие в кейсах сохранено.');return True
        enabled=db.execute('SELECT enabled FROM council_groups WHERE chat=?',(p['chat'],)).fetchone()
        if p['revision']!=int(rev) or not enabled or not enabled[0]:
            send(db,queue,uid,'Карточка изменилась или чат отключён. Откройте /profile для актуальной версии.');return True
        db.execute('UPDATE expert_profiles SET confirmed=1,updated=CURRENT_TIMESTAMP WHERE id=?',(pid,))
        send(db,queue,uid,'Профиль подтверждён и доступен в указанном чате. Резюме в группу не публикуется.');return True
    text=(m.get('text') or '').strip();parts=text.split();cmd=parts[0].split('@')[0] if parts else ''
    if cmd=='/start' and len(parts)==2 and parts[1].startswith('expert_'):
        digest=hashlib.sha256(parts[1][7:].encode()).hexdigest()
        invite=db.execute('SELECT * FROM expert_invites WHERE hash=? AND target=? AND expires>?',(digest,uid,now().isoformat())).fetchone()
        if not invite:send(db,queue,uid,'Приглашение недействительно, истекло или предназначено другому Telegram-аккаунту.');return True
        p=db.execute('SELECT * FROM expert_profiles WHERE id=? AND target=?',(invite['profile'],uid)).fetchone()
        if p:review(db,queue,uid,p)
        db.execute('DELETE FROM expert_invites WHERE hash=?',(digest,));return True
    if cmd=='/profile':
        profiles=db.execute('SELECT * FROM expert_profiles WHERE target=? ORDER BY id',(uid,)).fetchall()
        if not profiles:send(db,queue,uid,'Профиль пока не привязан. Передайте администратору свой Telegram ID через /id.')
        for p in profiles:review(db,queue,uid,p)
        return True
    if cmd=='/profile_edit':
        match=re.fullmatch(r'/profile_edit(?:@\w+)?\s+(\d+)\s+(name|role|topics|experience|education)\s*\|\s*(.+)',text,re.S)
        if not match:send(db,queue,uid,'Формат: /profile_edit НОМЕР topics | Темы через запятую');return True
        pid,field,value=match.groups();value=value.strip()
        p=db.execute('SELECT * FROM expert_profiles WHERE id=?',(pid,)).fetchone()
        if not p or (uid not in admins and p['target']!=uid):send(db,queue,uid,'Нет доступа к этому профилю.');return True
        if not 1<=len(value)<=1500:send(db,queue,uid,'Поле должно содержать от 1 до 1500 символов.');return True
        db.execute(f'UPDATE expert_profiles SET {field}=?,confirmed=0,published=0,revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=?',(value,pid))
        p=db.execute('SELECT * FROM expert_profiles WHERE id=?',(pid,)).fetchone()
        if uid==p['target']:review(db,queue,uid,p)
        else:send(db,queue,uid,'Изменение сохранено. Профиль скрыт до повторного подтверждения экспертом через /profile.')
        return True
    if cmd=='/resume':
        pid=parts[1] if len(parts)==2 and parts[1].isdigit() else None
        p=db.execute('SELECT * FROM expert_profiles WHERE id=?',(pid,)).fetchone()
        if not p or (uid not in admins and p['target']!=uid):send(db,queue,uid,'Нет доступа к резюме.');return True
        if p['source_file']:queue(db,'sendDocument',{'chat_id':uid,'document':p['source_file'],'caption':'Исходное резюме: '+p['name'][:200]})
        else:send(db,queue,uid,'Резюме ещё не прикреплено.')
        return True
    if cmd in ('/expert_new','/expert_invite','/expert_resume','/expert_list','/expert_view','/cancel_profile'):
        if uid not in admins:send(db,queue,uid,'Команда доступна администратору бота.');return True
        if cmd=='/cancel_profile':
            db.execute('DELETE FROM expert_uploads WHERE uid=?',(uid,));send(db,queue,uid,'Ожидание резюме отменено.');return True
        if cmd=='/expert_new':
            values=text.partition(' ')[2].split('|');values=[v.strip() for v in values]
            if len(values)!=5 or any(not 1<=len(v)<=1500 for v in values):
                send(db,queue,uid,'Формат: /expert_new ФИО | Работа и должность | Темы через запятую | Опыт | Образование');return True
            existing=db.execute('SELECT id FROM expert_profiles WHERE name=? AND role=? AND topics=? AND experience=? AND education=?',values).fetchone()
            if existing:send(db,queue,uid,f'Такой черновик уже есть: №{existing[0]}.');return True
            pid=db.execute('INSERT INTO expert_profiles(name,role,topics,experience,education) VALUES(?,?,?,?,?)',values).lastrowid
            send(db,queue,uid,f'Черновик профиля №{pid} создан. /expert_resume {pid} — прикрепить исходное резюме. Затем /expert_invite {pid} TELEGRAM_ID CHAT_ID.');return True
        if cmd=='/expert_list':
            offset=int(parts[1]) if len(parts)==2 and parts[1].isdigit() else 0
            rows=db.execute('SELECT id,name,confirmed FROM expert_profiles ORDER BY id DESC LIMIT 20 OFFSET ?',(offset,)).fetchall()
            send(db,queue,uid,'\n'.join(f'№{r["id"]} · {r["name"]} · '+('подтверждён' if r['confirmed'] else 'черновик') for r in rows) or 'Профилей нет.');return True
        pid=parts[1] if len(parts)>1 and parts[1].isdigit() else None
        p=db.execute('SELECT * FROM expert_profiles WHERE id=?',(pid,)).fetchone()
        if not p:send(db,queue,uid,'Укажите существующий номер профиля. /expert_list');return True
        if cmd=='/expert_view':send(db,queue,uid,profile_text(p,True));return True
        if cmd=='/expert_resume':
            if db.execute("SELECT 1 FROM proposals WHERE user_id=? AND status='draft'",(uid,)).fetchone():
                send(db,queue,uid,'Сначала завершите или отмените черновик предложения, чтобы не смешать вложения.');return True
            db.execute('INSERT INTO expert_uploads VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET profile=excluded.profile',(uid,pid))
            send(db,queue,uid,'Отправьте исходное резюме как документ. Оно будет прикреплено к профилю, без автоматической публикации и без передачи ИИ. /cancel_profile — отменить.');return True
        if cmd=='/expert_invite':
            if len(parts)!=4 or not parts[2].isdigit() or not re.fullmatch(r'-[1-9][0-9]*',parts[3]):
                send(db,queue,uid,'Формат: /expert_invite НОМЕР TELEGRAM_ID CHAT_ID\nID эксперта нужно подтвердить у него; ID чата — /groupid в группе.');return True
            target,cid=int(parts[2]),int(parts[3]);enabled=db.execute('SELECT enabled FROM council_groups WHERE chat=?',(cid,)).fetchone()
            if not target or not enabled or not enabled[0]:send(db,queue,uid,'Эксперт или подключённый чат не указан корректно.');return True
            if p['target'] is not None and (p['target']!=target or p['chat']!=cid):
                send(db,queue,uid,'Этот профиль уже привязан к другому аккаунту/чату. Переназначение запрещено; создайте отдельный черновик.');return True
            other=db.execute('SELECT id FROM expert_profiles WHERE target=? AND chat=? AND id!=?',(target,cid,pid)).fetchone()
            if other:send(db,queue,uid,f'У этого эксперта уже есть профиль №{other[0]} в данном чате.');return True
            db.execute('UPDATE expert_profiles SET target=?,chat=? WHERE id=?',(target,cid,pid))
            db.execute('DELETE FROM expert_invites WHERE profile=?',(pid,))
            token=secrets.token_urlsafe(24)
            db.execute('INSERT INTO expert_invites VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),pid,target,(now()+timedelta(days=7)).isoformat()))
            send(db,queue,uid,'Передайте эксперту лично эту команду для отправки в личный чат бота (действует 7 дней и только для указанного Telegram ID):\n/start expert_'+token)
            return True
    pending=db.execute('SELECT profile FROM expert_uploads WHERE uid=?',(uid,)).fetchone()
    if pending and (cmd in ('/new','/import') or text=='Подать предложение'):
        send(db,queue,uid,'Сначала отправьте резюме или /cancel_profile, затем создавайте предложение.');return True
    if pending and not cmd.startswith('/'):
        if uid not in admins:
            db.execute('DELETE FROM expert_uploads WHERE uid=?',(uid,));send(db,queue,uid,'Права администратора отозваны.');return True
        doc=m.get('document') or {}
        if not doc.get('file_id'):send(db,queue,uid,'Ожидаю резюме как документ. /cancel_profile — отменить.');return True
        db.execute('UPDATE expert_profiles SET source_file=?,source_name=?,confirmed=0,published=0,revision=revision+1,updated=CURRENT_TIMESTAMP WHERE id=?',(doc['file_id'],doc.get('file_name','Резюме'),pending[0]))
        db.execute('DELETE FROM expert_uploads WHERE uid=?',(uid,));send(db,queue,uid,'Резюме прикреплено. Сведения профиля требуют подтверждения экспертом.');return True
    return False


def matching_topics(topics,body):
    """Explicit whole-phrase matches only. No proficiency scores or inferred skills."""
    body=' '.join(body.casefold().replace('ё','е').split())
    return [tag.strip() for tag in topics.split(',') if len(tag.strip())>1 and re.search(r'(?<!\w)'+re.escape(' '.join(tag.strip().casefold().replace('ё','е').split()))+r'(?!\w)',body)]


def group_profiles(db,queue,cid,uid,text):
    from group_tasks import emit
    parts=text.split();cmd=parts[0].split('@')[0] if parts else ''
    if cmd=='/groupid':emit(db,queue,cid,f'ID этого чата: {cid}');return True
    if cmd=='/experts':
        offset=int(parts[1]) if len(parts)==2 and parts[1].isdigit() else 0
        rows=db.execute('SELECT * FROM expert_profiles WHERE chat=? AND (confirmed=1 OR published=1) ORDER BY name LIMIT 10 OFFSET ?',(cid,offset)).fetchall()
        emit(db,queue,cid,('Профили экспертов:\n'+'\n\n'.join(f'№{p["id"]} · {p["name"]}\n{p["role"]}\nТемы: {p["topics"]} · Подтверждение эксперта: {"да" if p["confirmed"] else "нет, сведения по резюме"}' for p in rows) if rows else 'Опубликованных профилей пока нет.')+f'\n/experts {offset+10} — следующая страница. /expert НОМЕР — карточка.');return True
    if cmd=='/expert':
        pid=parts[1] if len(parts)==2 and parts[1].isdigit() else None
        p=db.execute('SELECT * FROM expert_profiles WHERE id=? AND chat=? AND (confirmed=1 OR published=1)',(pid,cid)).fetchone()
        emit(db,queue,cid,profile_text(p) if p else 'Опубликованный профиль в этом чате не найден.');return True
    if cmd=='/recommend':
        tid=parts[1] if len(parts)==2 and parts[1].isdigit() else None
        task=db.execute('SELECT * FROM council_tasks WHERE id=? AND chat=?',(tid,cid)).fetchone()
        if not task:emit(db,queue,cid,'Формат: /recommend НОМЕР_КЕЙСА');return True
        lines=[]
        for p in db.execute('SELECT * FROM expert_profiles WHERE chat=? AND (confirmed=1 OR published=1) ORDER BY name',(cid,)).fetchall():
            matches=matching_topics(p['topics'],task['body'])
            if matches:lines.append(f'{p["name"]} (профиль №{p["id"]}): совпадают темы «'+ '», «'.join(matches)+'». '+('Подтверждён экспертом.' if p['confirmed'] else 'По резюме; эксперт не подтвердил.'))
        emit(db,queue,cid,f'Подбор по теме кейса №{tid}:\n'+('\n'.join(lines) if lines else 'Прямых совпадений с темами опубликованных профилей нет.')+'\nСовпадения по темам. Участие открыто всем экспертам.');return True
    return False
