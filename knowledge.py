"""Local, permission-gated knowledge search over existing council cases.

Never sends archived material or other groups' data to the model.
No external API calls and no changes to existing schema.
"""
import json
import re

STOP = set('и в во на по для из от до при к с со о об а но что как где или это эту этого эти тех их мы вы он она оно они все всех же ли не бы был была были быть есть про над под между через без вопрос вопросы вопроса кейс кейса научный научная научные науки научных'.split())

def tokens(s):
    parts=re.findall(r'[а-яёa-z0-9]{3,}',(s or '').casefold())
    out=[]
    for token in parts:
        if token in STOP:continue
        # Light Russian stemming: retain at least 4 chars, tolerant of inflection.
        for ending in ('ирования','ование','ениями','ениями','ения','ению','иями','иями','иями','ыми','ого','ему','ами','ями','иях','иях','иях','ная','ные','ных','ной','ный','ние','ние','ния','ии','ий','ие','ия','ов','ев','ам','ям','ах','ях','ом','ем','ую','ая','ое','ые','ых','ый','ой','а','ы','и','е','у','ю'):
            if len(token)-len(ending)>=4 and token.endswith(ending):
                token=token[:-len(ending)];break
        if token not in STOP:out.append(token)
    return set(out)

def draft_text(raw):
    try: data=json.loads(raw)
    except (TypeError,ValueError):return ''
    strings=[]
    def walk(x):
        if isinstance(x,str):strings.append(x)
        elif isinstance(x,list):
            for v in x:walk(v)
        elif isinstance(x,dict):
            for key,val in x.items():
                if key not in ('sources','source_ids','metadata'):walk(val)
    walk(data)
    return ' '.join(strings)[:15000]

def search(db,chat,query,exclude=None,limit=5):
    q=tokens(query)
    if not q:return []
    rows=db.execute("""SELECT t.id,t.body,t.archived,t.closed,d.version,d.result
       FROM council_tasks t LEFT JOIN conclusion_drafts d ON d.task=t.id AND d.chat=t.chat
       WHERE t.chat=? AND t.archived=0 ORDER BY t.id DESC LIMIT 1000""",(chat,)).fetchall()
    matches=[]
    for row in rows:
        if row['id']==exclude:continue
        title=tokens(row['body']); conclusion=tokens(draft_text(row['result']))
        opinions=db.execute("""SELECT body FROM council_materials WHERE task=? AND revision>0
                          AND body IS NOT NULL ORDER BY id DESC LIMIT 200""",(row['id'],)).fetchall()
        opinion_tokens=set()
        for opinion in opinions:opinion_tokens.update(tokens(opinion['body'][:1500]))
        score=4*len(q & title)+2*len(q & conclusion)+len(q & opinion_tokens)
        if score:
            matches.append({'id':row['id'],'title':row['body'].replace('\n',' ')[:115],
                'score':score,'matches':len(q & (title|conclusion|opinion_tokens)),
                'has_conclusion':bool(row['result']),'closed':bool(row['closed'])})
    return sorted(matches,key=lambda x:(-x['score'],-x['matches'],-x['id']))[:limit]

def display(db,queue,chat,query,exclude=None):
    from group_tasks import emit
    results=search(db,chat,query,exclude)
    if not results:
        emit(db,queue,chat,'По запросу ничего не найдено среди активных кейсов этого совета. Попробуйте другие ключевые слова.')
        return
    lines=['Найденные материалы по запросу «'+query[:90]+'»:']
    buttons=[]
    for result in results:
        lines.append(f'\nКейс №{result["id"]}: {result["title"]}\n'+
                     ('Есть проект заключения.' if result['has_conclusion'] else 'Заключение пока не подготовлено.'))
        buttons.append([(f'Открыть кейс №{result["id"]}',f'g:card:{result["id"]}')])
    lines.append('\nЭто поиск по сохранённым текстам, не подтверждение правильности выводов. Проверяйте оригинальные мнения и документы в карточке кейса.')
    emit(db,queue,chat,'\n'.join(lines),buttons)
