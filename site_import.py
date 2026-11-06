"""Manual, admin-only intake from quryltai.kz; no website credentials."""
import re


def initialize_import(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(proposals)')}
    for field in ('external_id', 'source_site'):
        if field not in columns:
            db.execute(f"ALTER TABLE proposals ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS unique_site_proposal ON proposals(source_site,external_id) WHERE external_id!=''")


def start_import(db, uid, admins, text, say):
    if uid not in admins:
        say(db, uid, 'Импорт доступен только администратору.')
        return
    parts = text.split(maxsplit=1)
    fields = [p.strip() for p in parts[1].split('|')] if len(parts) == 2 else []
    if (len(fields) != 3 or not re.fullmatch(r'PRP-[0-9]{4}-[0-9]{6}', fields[0])
            or not 1 <= len(fields[1]) <= 200 or not 1 <= len(fields[2]) <= 200):
        say(db, uid, 'Формат: /import PRP-2026-000038 | Сергей Ван | Тема предложения\nАвтор и тема — до 200 символов каждый. Затем пришлите описание и файлы, завершите /done. Это ручной импорт; сайт автоматически не синхронизируется.')
        return
    external_id, author, topic = fields
    existing = db.execute("SELECT id,status FROM proposals WHERE source_site='admin.quryltai.kz' AND external_id=?", (external_id,)).fetchone()
    if existing:
        hint = 'Импорт уже начат. Завершите существующий черновик.' if existing['status'] == 'draft' else f'Уже импортировано: /view {existing["id"]}'
        say(db, uid, f'{external_id}: {hint} Повторная запись не создана.')
        return
    if db.execute("SELECT 1 FROM proposals WHERE user_id=? AND status='draft'", (uid,)).fetchone():
        say(db, uid, 'Сначала завершите текущий черновик через /done или отмените через /cancel. Он не изменён.')
        return
    db.execute('INSERT INTO proposals(user_id,author,topic,external_id,source_site) VALUES(?,?,?,?,?)',
               (uid, author, topic, external_id, 'admin.quryltai.kz'))
    say(db, uid, f'Черновик импорта {external_id}\nАвтор: {author}\nТема: {topic}\nПришлите сопроводительный текст и приложения как «Документ», затем /done. /cancel — отменить импорт.\nАвтор не связывается с вашим Telegram. Сайт и его статусы не изменяются.')
