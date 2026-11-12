"""Shared, conservative expertise discovery for personal chat and council cases.

No automatic membership, invitation or competence verification. Public results do not
include contact details, and candidates are always labelled unapproved.
"""
import re
import unicodedata

STOP = set("вопрос вопросы проблем проблема проблемы научный научная научное научные научных научного наука науки казахстан казахстана республики эксперт экспертов специалист специалисты совет совета тематика темы тема обеспечить организация организации организацией развитие развития развитии система системы управление управления также которые за счет из за для при над под можно нужно этой этом данных покажи статус статусы утверждения каждый каждого объясни почему подходит подходящих".split())
# Explicit *related areas* are suggestions, never a claim of verified competence.
AREAS = {
  'финансирование науки': ('финансир', 'грант', 'бюджет', 'лаборатор', 'институт', 'оборудован', 'научн политик', 'ниокр', 'научн техническ', 'экономик наук', 'исследовательск инфраструктур'),
  'научная политика': ('научн политик', 'инновационн политик', 'управлени наук', 'научн техническ', 'стратегическ планирован', 'управлени ниокр', 'научн систем', 'финансир наук'),
  'цифровизация': ('цифров', 'информационн технолог', 'искусственн интеллект', 'цифров трансформац'),
  'инновации': ('инновац', 'научн техническ развити', 'ниокр', 'технологическ развити'),
}

def norm(s):
    return unicodedata.normalize('NFKC', str(s or '')).casefold().replace('ё','е')

def words(s):
    return {w for w in re.findall(r'[^\W_]+',norm(s)) if len(w)>=5 and w not in STOP}

def stem(w):
    return w[:max(5,len(w)-4)] if len(w)>=8 else w

def areas(text):
    s=norm(text)
    return {name for name,keys in AREAS.items() if any(k in s for k in keys)}

def score(query, topics, supplementary=''):
    q=words(query)
    t=words(topics)
    x=words(supplementary)
    matched=sorted({w for w in q if stem(w) in {stem(z) for z in t}})
    supporting=sorted({w for w in q if stem(w) in {stem(z) for z in x} and w not in matched})
    common=areas(query) & (areas(topics) | areas(supplementary))
    # No match solely from generic science/management words.
    points=4*len(matched)+len(supporting)+2*len(common)
    if not points:return None
    reasons=[]
    if matched:reasons.append('совпадение терминов: '+', '.join(matched[:4]))
    if common:reasons.append('смежная область: '+', '.join(sorted(common)[:2]))
    if supporting:reasons.append('в должности/опыте: '+', '.join(supporting[:3]))
    return points, '; '.join(reasons)

def find(db,query,chat=None,limit=5):
    """Return approved published profiles first, and separate unapproved applicants.
    chat=None is only for the administrator's private query.
    """
    if chat is None:
        profiles=db.execute('SELECT * FROM expert_profiles WHERE published=1 ORDER BY name').fetchall()
    else:
        profiles=db.execute('SELECT * FROM expert_profiles WHERE published=1 AND chat=? ORDER BY name',(chat,)).fetchall()
    approved=[]; names=set()
    for p in profiles:
        names.add(norm(p['name']).strip())
        match=score(query,p['topics'],p['role']+' '+p['experience']+' '+p['education'])
        if match:approved.append((match[0],p,match[1]))
    approved.sort(key=lambda v:(-v[0],norm(v[1]['name'])))
    candidates=[]
    for a in db.execute('SELECT * FROM expert_applications WHERE promoted_profile IS NULL'):
        if norm(a['name']).strip() in names:continue
        match=score(query,a['topics'],(a['position'] or '')+' '+(a['organization'] or '')+' '+(a['experience'] or '')+' '+(a['education'] or ''))
        if match:candidates.append((match[0],a,match[1]))
    candidates.sort(key=lambda v:(-v[0],norm(v[1]['name'])))
    return approved[:limit],candidates[:limit]

def format_results(query,approved,candidates):
    lines=['Подбор по теме: '+query[:280], 'Оценка ориентировочная по указанным направлениям; квалификация независимо не проверена.']
    if approved:
        lines+=['\nУтверждённые эксперты:']
        for n,(_,p,reason) in enumerate(approved,1):
            lines.append(f'{n}. №{p["id"]} · {p["name"]}\n{p["role"][:130]}\nОснование: {reason}.')
    else:lines+=['\nУтверждённых экспертов с обоснованным тематическим совпадением не найдено.']
    if candidates:
        lines+=['\nКандидаты из заявок (НЕ утверждены советом):']
        for n,(_,a,reason) in enumerate(candidates,1):
            lines.append(f'{n}. {a["name"]} · {(a["organization"] or "Организация не указана")[:90]}\nОснование: {reason}.')
    elif not approved:lines+=['Уточните специализацию или расширьте формулировку запроса.']
    lines+=['\nНикто не добавлен к кейсу и не приглашён автоматически. Решение принимает администратор.']
    return '\n'.join(lines)
