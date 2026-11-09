"""Human approval of recommendations extracted from an existing AI conclusion draft."""
import json
import re


def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS conclusion_recommendation_candidates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      chat INTEGER NOT NULL,
      task INTEGER NOT NULL,
      conclusion_version INTEGER NOT NULL,
      position INTEGER NOT NULL,
      body TEXT NOT NULL,
      imported_rec INTEGER DEFAULT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(task, conclusion_version, position)
    );
    CREATE INDEX IF NOT EXISTS candidate_by_task ON conclusion_recommendation_candidates(task,conclusion_version);
    ''')


def split_recommendations(text):
    """Split only explicit list boundaries; never invent actions not in the conclusion."""
    text = (text or '').strip()
    if not text or text.lower() == 'недостаточно данных':
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    chunks = []
    current = ''
    explicit = re.compile(r'^(?:[-•*]\s+|\d{1,2}[.)]\s+)')
    for line in lines:
        is_item = bool(explicit.match(line))
        cleaned = explicit.sub('', line).strip()
        if is_item and current:
            chunks.append(current)
            current = ''
        current = (current + ' ' + cleaned).strip() if current else cleaned
    if current:
        chunks.append(current)
    # If not a list, keep as ONE candidate rather than splitting sentences speculatively.
    unique = []
    for chunk in chunks:
        chunk = re.sub(r'\s+', ' ', chunk).strip()
        if 12 <= len(chunk) <= 1500 and chunk.casefold() not in {x.casefold() for x in unique}:
            unique.append(chunk)
    return unique[:15]


def prepare(db, chat, task):
    from group_tasks import emit
    row = db.execute('''SELECT d.version,d.result FROM conclusion_drafts d
      JOIN council_tasks t ON t.id=d.task AND t.chat=d.chat
      WHERE d.task=? AND d.chat=? AND t.archived=0''',(task,chat)).fetchone()
    if not row:
        return 'Сначала сформируйте экспертное заключение.', []
    try:
        recommendations = json.loads(row['result'])['recommendations']
    except (ValueError, KeyError, TypeError):
        return 'Не удалось прочитать раздел рекомендаций. Пересоздайте заключение.', []
    candidates = split_recommendations(recommendations)
    if not candidates:
        return 'В заключении нет отдельных рекомендаций для регистрации.', []
    for pos,body in enumerate(candidates,1):
        db.execute('''INSERT OR IGNORE INTO conclusion_recommendation_candidates
        (chat,task,conclusion_version,position,body) VALUES(?,?,?,?,?)''',
        (chat,task,row['version'],pos,body))
    rows=db.execute('''SELECT id,position,body,imported_rec FROM conclusion_recommendation_candidates
        WHERE chat=? AND task=? AND conclusion_version=? ORDER BY position LIMIT 15''',
        (chat,task,row['version'])).fetchall()
    buttons=[]
    for candidate in rows:
        if not candidate['imported_rec']:
            buttons.append([(f'Добавить №{candidate["position"]}',f'g:recimport:{candidate["id"]}')])
    lines=[f'Кейс №{task} · рекомендации из проекта заключения v{row["version"]}.',
           'Проверьте содержание перед регистрацией. Каждая кнопка добавляет только один пункт.']
    for candidate in rows:
        flag=f' (зарегистрирована №{candidate["imported_rec"]})' if candidate['imported_rec'] else ''
        lines.append(f'\n{candidate["position"]}. {candidate["body"]}{flag}')
    return '\n'.join(lines), buttons


def approve(db, chat, uid, candidate_id):
    """Explicit approval; atomic in caller's transaction; safe against repeated callback."""
    row=db.execute('''SELECT c.*,d.version AS current_version FROM conclusion_recommendation_candidates c
       JOIN council_tasks t ON t.id=c.task AND t.chat=c.chat AND t.archived=0
       JOIN conclusion_drafts d ON d.task=c.task AND d.chat=c.chat
       WHERE c.id=? AND c.chat=?''',(candidate_id,chat)).fetchone()
    if row is None:return 'Предложение не найдено или кейс находится в архиве.'
    if row['conclusion_version'] != row['current_version']:
        return 'Заключение обновлено. Откройте предложения заново.'
    if row['imported_rec']:
        return f'Этот пункт уже зарегистрирован как рекомендация №{row["imported_rec"]}.'
    from recommendations import add
    response=add(db,chat,row['task'],uid,row['body'])
    match=re.search(r'Рекомендация №(\d+) добавлена',response)
    if not match:return response
    rid=int(match.group(1))
    db.execute('UPDATE conclusion_recommendation_candidates SET imported_rec=? WHERE id=? AND imported_rec IS NULL',(rid,candidate_id))
    return f'Пункт добавлен как рекомендация №{rid}. /rec {rid}\nАдресат и срок можно указать отдельно.'
