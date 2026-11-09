"""Manual WhatsApp invitations to applicants; Telegram binding with admin approval.

No WhatsApp API, no unsolicited sending. Private phone numbers stay in admin DM.
Telegram deep links prove possession of a link, not identity: an admin approves claims.
"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from expert_registry import rank

ID = re.compile(r'EXP-\d{4}-\d{6}', re.I)
GROUP_REQUEST = re.compile(r'^\s*(?:жанель|жәнел|жанел|zhanel|janel)[,\s!:.—-]+.*(?:приглас|приглашен|шақыр|invite).*?(?:whats\s*app|ватсап|уатсап|wa\b).*?(?:кейс\w*\s*(?:№|#)?\s*)(\d{1,10})', re.I)
GROUP_REQUEST2 = re.compile(r'^\s*(?:жанель|жәнел|жанел|zhanel|janel)[,\s!:.—-]+.*(?:whats\s*app|ватсап|уатсап).*?(?:приглас|приглашен|шақыр|invite).*?(?:кейс\w*\s*(?:№|#)?\s*)(\d{1,10})', re.I)


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS wa_invitations(
      id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT UNIQUE NOT NULL,
      application_id TEXT NOT NULL, council_chat INTEGER NOT NULL DEFAULT 0,
      case_id INTEGER NOT NULL DEFAULT 0, created_by INTEGER NOT NULL,
      state TEXT NOT NULL DEFAULT 'issued', claimed_uid INTEGER,
      created TEXT NOT NULL, expires TEXT NOT NULL,
      claimed_at TEXT);
    CREATE INDEX IF NOT EXISTS wa_invites_candidate ON wa_invitations(application_id,state);
    ''')


def _send(db,queue,uid,body,buttons=None):
    data={'chat_id':uid,'text':body}
    if buttons:data['reply_markup']={'inline_keyboard':[[{'text':label,'callback_data':action} for label,action in row] for row in buttons]}
    queue(db,'sendMessage',data)


def _digits(phone):
    # Telegram source numbers are unverified; preserve only a single E.164-like number.
    s=str(phone or '').strip()
    if any(c in s for c in (';',',','/','\n')):return None
    digits=re.sub(r'\D','',s)
    if not 8<=len(digits)<=15:return None
    if s.startswith('8') and len(digits)==11:digits='7'+digits[1:]
    return digits


def _bot_name():
    name=os.environ.get('TELEGRAM_BOT_USERNAME','').strip().lstrip('@')
    return name if re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{4,31}',name) else None


def create(db,p,admin,group=0,case=0):
    username=_bot_name()
    if not username:return None,'Не удалось определить имя Telegram-бота. Установите TELEGRAM_BOT_USERNAME в Render Environment.'
    digits=_digits(p['phone'])
    if not digits:return None,f'В заявке {p["application_id"]} нет однозначного телефонного номера в международном формате. Проверьте номер вручную.'
    if group:
        enabled=db.execute('SELECT 1 FROM council_groups WHERE chat=? AND enabled=1',(group,)).fetchone()
        if not enabled:return None,'Чат экспертного совета не подключён.'
    token=secrets.token_urlsafe(24)
    digest=hashlib.sha256(token.encode()).hexdigest()
    now=datetime.now(timezone.utc)
    db.execute('''INSERT INTO wa_invitations(token_hash,application_id,council_chat,case_id,created_by,created,expires)
                  VALUES(?,?,?,?,?,?,?)''',(digest,p['application_id'],int(group),int(case),int(admin),now.isoformat(),(now+timedelta(days=7)).isoformat()))
    deep=f'https://t.me/{username}?start=wa_{token}'
    msg=('Уважаемый коллега!\n\n'
         'Приглашаем Вас рассмотреть возможность участия в работе экспертного совета по науке при Комитете по цифровизации, инновациям и науке Құрылтая Республики Казахстан.\n\n'
         'Для подтверждения интереса откройте персональную ссылку в Telegram:\n'+deep+'\n\n'
         'После перехода администратор проверит заявку и подтвердит доступ. Если приглашение неактуально, просто проигнорируйте сообщение.')
    url='https://wa.me/'+digits+'?text='+quote(msg,safe='')
    return {'url':url,'message':msg,'link':deep,'name':p['name'],'id':p['application_id'],'phone':digits},None


def handle_private(db,admins,update,queue):
    cb=update.get('callback_query')
    m=(cb or {}).get('message') if cb else update.get('message')
    m=m or {};chat=m.get('chat') or {};actor=(cb or {}).get('from') if cb else m.get('from') or {}
    uid=actor.get('id')
    if chat.get('type')!='private' or chat.get('id')!=uid or not isinstance(uid,int) or actor.get('is_bot'):return False
    if cb:
        match=re.fullmatch(r'wi:(approve|reject):(\d{1,12})',str(cb.get('data','')))
        if not match:return False
        queue(db,'answerCallbackQuery',{'callback_query_id':cb['id']})
        if uid not in admins:_send(db,queue,uid,'Только администратор может подтвердить приглашение.');return True
        action,i=match.groups()
        row=db.execute('SELECT * FROM wa_invitations WHERE id=?',(int(i),)).fetchone()
        if not row or row['state']!='claimed' or not row['claimed_uid']:
            _send(db,queue,uid,'Заявка уже рассмотрена или не найдена.');return True
        if action=='reject':
            db.execute("UPDATE wa_invitations SET state='rejected' WHERE id=? AND state='claimed'",(row['id'],))
            _send(db,queue,uid,'Подтверждение отклонено.');_send(db,queue,row['claimed_uid'],'Приглашение не подтверждено. Уточните детали у организатора.')
            return True
        result=approve(db,row)
        if result:
            _send(db,queue,uid,'Не удалось привязать: '+result);return True
        _send(db,queue,uid,f'Привязан Telegram ID {row["claimed_uid"]} к заявке {row["application_id"]}. Черновик профиля ожидает подтверждения экспертом. Добавление в группу — после вашего разрешения.')
        _send(db,queue,row['claimed_uid'],'Участие подтверждено администратором. Профиль привязан к Telegram. Отправьте /profile, проверьте данные и подтвердите профиль. Доступ к группе предоставляет организатор.')
        return True
    text=(m.get('text') or '').strip()
    match=re.fullmatch(r'/start(?:@\w+)?\s+wa_([A-Za-z0-9_-]{20,80})',text)
    if match:
        digest=hashlib.sha256(match.group(1).encode()).hexdigest()
        row=db.execute('SELECT * FROM wa_invitations WHERE token_hash=?',(digest,)).fetchone()
        if not row or row['state']!='issued' or row['expires']<=datetime.now(timezone.utc).isoformat():
            _send(db,queue,uid,'Ссылка недействительна или уже использована. Запросите новую у организатора.');return True
        db.execute("UPDATE wa_invitations SET state='claimed',claimed_uid=?,claimed_at=? WHERE id=? AND state='issued'",(uid,datetime.now(timezone.utc).isoformat(),row['id']))
        _send(db,queue,uid,'Спасибо! Заявка на участие получена. Организатор проверит соответствие приглашения и подтвердит привязку профиля. Пока доступ к группе не открыт.')
        p=db.execute('SELECT name,organization FROM expert_applications WHERE application_id=?',(row['application_id'],)).fetchone()
        title=(p['name'] if p else row['application_id'])
        for admin in admins:
            _send(db,queue,admin,f'Запрос на привязку из WhatsApp-приглашения №{row["id"]}.\nКандидат: {title} · {row["application_id"]}\nTelegram ID: {uid}\nКейс: {row["case_id"] or "без кейса"}\nВНИМАНИЕ: ссылка могла быть переслана. Сверьте личность с кандидатом до подтверждения.',[[('Подтвердить',f'wi:approve:{row["id"]}'),('Отклонить',f'wi:reject:{row["id"]}')]])
        return True
    if uid not in admins:return False
    match=re.fullmatch(r'/wa_invite(?:@\w+)?\s+(EXP-\d{4}-\d{6})(?:\s+(-?\d+))?',text,re.I)
    if not match:return False
    aid,group=match.groups()
    p=db.execute('SELECT * FROM expert_applications WHERE application_id=?',(aid.upper(),)).fetchone()
    if not p:_send(db,queue,uid,'Заявка не найдена. Используйте /candidate НОМЕР.');return True
    invitation,error=create(db,p,uid,int(group or 0))
    if error:_send(db,queue,uid,error);return True
    _send(db,queue,uid,'Для '+invitation['name']+' ('+aid.upper()+') подготовлен WhatsApp-текст.\nСначала проверьте согласие на контакт и правильность номера. Отправка НЕ выполнялась автоматически.\n\nОткройте ссылку и вручную нажмите «Отправить»:\n'+invitation['url'])
    return True


def approve(db,inv):
    p=db.execute('SELECT * FROM expert_applications WHERE application_id=?',(inv['application_id'],)).fetchone()
    if not p:return 'Заявка удалена.'
    uid=inv['claimed_uid'];group=inv['council_chat']
    existing=db.execute('SELECT id,target,chat FROM expert_profiles WHERE id=?',(p['promoted_profile'],)).fetchone() if p['promoted_profile'] else None
    if existing and existing['target'] is not None and existing['target']!=uid:return 'Профиль уже привязан к другому Telegram ID.'
    if group and db.execute('SELECT 1 FROM expert_profiles WHERE target=? AND chat=? AND id!=?',(uid,group,existing['id'] if existing else -1)).fetchone():
        return 'Этот Telegram ID уже имеет профиль в данной группе; объедините записи вручную.'
    if existing:
        db.execute('UPDATE expert_profiles SET target=?,chat=?,confirmed=0,updated=CURRENT_TIMESTAMP WHERE id=?',(uid,group or None,existing['id']))
    else:
        role=' · '.join(filter(None,(p['organization'],p['position']))) or 'Не указано'
        new=db.execute('''INSERT INTO expert_profiles(name,role,topics,experience,education,target,chat,confirmed)
                          VALUES(?,?,?,?,?,?,?,0)''',(p['name'][:1500],role[:1500],(p['topics'] or 'Не указано')[:1500],(p['experience'] or 'Не указано')[:1500],(p['education'] or 'Не указано')[:1500],uid,group or None)).lastrowid
        db.execute('UPDATE expert_applications SET promoted_profile=? WHERE application_id=?',(new,p['application_id']))
    db.execute("UPDATE wa_invitations SET state='approved' WHERE id=? AND state='claimed'",(inv['id'],))
    return None


def handle_group(db,admins,update,queue):
    m=update.get('message') or {};ch=m.get('chat') or {};who=m.get('from') or {}
    cid=ch.get('id');uid=who.get('id')
    if ch.get('type') not in ('group','supergroup') or not isinstance(uid,int):return False
    text=(m.get('text') or '').strip()
    found=GROUP_REQUEST.search(text) or GROUP_REQUEST2.search(text)
    if not found:return False
    from group_tasks import is_chair
    if not is_chair(db,cid,uid,admins):_send(db,queue,cid,'Приглашения может подготовить только администратор или ведущий.');return True
    tid=int(found.group(1))
    task=db.execute('SELECT body FROM council_tasks WHERE id=? AND chat=? AND archived=0',(tid,cid)).fetchone()
    if not task:_send(db,queue,cid,'Кейс не найден.');return True
    matches=rank(db,task['body'],3)
    if not matches:_send(db,queue,cid,'Подходящих кандидатов по специализации пока не найдено.');return True
    count=0
    for _,p,_ in matches:
        invitation,error=create(db,p,uid,cid,tid)
        if error:
            _send(db,queue,uid,p['application_id']+': '+error)
        else:
            count+=1
            _send(db,queue,uid,'Приглашение по кейсу №'+str(tid)+' для '+p['name']+' ('+p['application_id']+').\nПроверьте согласие и номер; отправка вручную.\n'+invitation['url'])
    _send(db,queue,cid,f'Подготовлено {count} WhatsApp-приглашений. Ссылки переданы ведущему лично; никому сообщения пока не отправлены.')
    return True
