"""Ғылым: Telegram intake bot. Python standard library only."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MENU = {'keyboard': [['Подать предложение', 'Мои предложения'], ['О совете', 'Мой ID']], 'resize_keyboard': True}
STATUSES = {'new': 'Получено', 'review': 'На рассмотрении', 'done': 'Рассмотрено'}


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS proposals(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          author TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'draft', created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE UNIQUE INDEX IF NOT EXISTS one_draft ON proposals(user_id) WHERE status='draft';
        CREATE TABLE IF NOT EXISTS files(
          id INTEGER PRIMARY KEY, proposal_id INTEGER REFERENCES proposals(id) ON DELETE CASCADE,
          file_id TEXT NOT NULL, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, method TEXT NOT NULL, payload TEXT NOT NULL,
          attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, state TEXT DEFAULT 'pending');
        ''')


def queue(db, method, payload):
    db.execute('INSERT INTO outbox(method,payload) VALUES (?,?)', (method, json.dumps(payload, ensure_ascii=False)))


def say(db, user, text, menu=False):
    # Telegram counts UTF-16 code units; conservative chunks also handle emoji.
    for i in range(0, len(text), 1800):
        payload = {'chat_id': user, 'text': text[i:i + 1800]}
        if menu:
            payload['reply_markup'] = MENU
        queue(db, 'sendMessage', payload)


def process(path, admins, update):
    if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
        return
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM updates WHERE id=?', (update['update_id'],)).fetchone():
            return
        db.execute('INSERT INTO updates VALUES (?)', (update['update_id'],))
        m = update.get('message') or {}
        # Intake and admin documents are private-chat only, including in groups with admins.
        if m.get('chat', {}).get('type') != 'private' or m.get('from', {}).get('is_bot'):
            return
        uid = m.get('from', {}).get('id')
        if not isinstance(uid, int) or uid != m['chat'].get('id'):
            return
        text = m.get('text', '').strip()
        cmd = text.split()[0].split('@')[0] if text else ''
        draft = db.execute("SELECT * FROM proposals WHERE user_id=? AND status='draft'", (uid,)).fetchone()
        if cmd in ('/start', '/help'):
            say(db, uid, 'Ғылым — помощник экспертного совета по науке.\n'
                'Для подачи предложения нажмите «Подать предложение». Ваш Telegram ID, имя, текст и ссылки на файлы будут сохранены и доступны администратору совета. '
                'Отправляйте только материалы, которыми вправе делиться. Регистрация предложения не означает его одобрения или включения автора в состав совета.\n'
                '/done — отправить предложение; /cancel — удалить черновик; /my — мои предложения; /id — мой ID.\n'
                + ('Администратор: /list [страница], /view НОМЕР, /status НОМЕР new|review|done, /summary.' if uid in admins else ''), True)
        elif cmd == '/id' or text == 'Мой ID':
            say(db, uid, f'Ваш Telegram ID: {uid}')
        elif cmd == '/about' or text == 'О совете':
            say(db, uid, 'Совет по науке и научно-технологическому развитию осуществляет экспертное сопровождение работы Комитета по цифровизации, инновациям и науке Курултая РК. '
                'Рекомендации совета носят рекомендательный характер. Персональный состав утверждается Комитетом.')
        elif cmd == '/new' or text == 'Подать предложение':
            if not draft:
                author = ' '.join(str(m['from'].get(k, '')) for k in ('first_name', 'last_name')).strip()
                db.execute('INSERT INTO proposals(user_id,author) VALUES (?,?)', (uid, author))
            say(db, uid, 'Пришлите текст: ФИО и организация; проблема; предлагаемое решение; обоснование. '
                'Можно отправить несколько сообщений и до 10 файлов как «Документ». Затем /done. Для отмены /cancel.')
        elif cmd == '/cancel':
            if draft:
                db.execute('DELETE FROM proposals WHERE id=?', (draft['id'],))
            say(db, uid, 'Черновик удалён.' if draft else 'Активного черновика нет.', True)
        elif cmd == '/done':
            if not draft or not draft['body'].strip():
                say(db, uid, 'Сначала создайте предложение и добавьте описание текстом. Файл не заменяет описание.')
            else:
                db.execute("UPDATE proposals SET status='new' WHERE id=?", (draft['id'],))
                say(db, uid, f'Предложение №{draft["id"]} зарегистрировано. Статус: Получено.', True)
                for admin in admins:
                    say(db, admin, f'Новое предложение №{draft["id"]}. Открыть: /view {draft["id"]}')
        elif cmd == '/my' or text == 'Мои предложения':
            rows = db.execute("SELECT id,status FROM proposals WHERE user_id=? AND status!='draft' ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
            say(db, uid, '\n'.join(f'№{r["id"]}: {STATUSES[r["status"]]}' for r in rows) or 'Отправленных предложений пока нет.')
        elif cmd in ('/list', '/view', '/status', '/summary'):
            if uid not in admins:
                say(db, uid, 'Команда доступна только администратору.')
                return
            args = text.split()
            if cmd == '/summary':
                rows = db.execute("SELECT status,count(*) n FROM proposals WHERE status!='draft' GROUP BY status").fetchall()
                say(db, uid, 'Сводка по статусам (без ИИ):\n' + ('\n'.join(f'{STATUSES[r["status"]]}: {r["n"]}' for r in rows) or 'Нет предложений.'))
            elif cmd == '/list':
                if len(args) > 2 or (len(args) == 2 and not re.fullmatch(r'[1-9][0-9]{0,6}', args[1])):
                    say(db, uid, 'Формат: /list 1')
                    return
                page = int(args[1]) if len(args) == 2 else 1
                rows = db.execute("SELECT id,author,status FROM proposals WHERE status!='draft' ORDER BY id DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,)).fetchall()
                say(db, uid, f'Страница {page}\n' + ('\n'.join(f'№{r["id"]} · {r["author"]} · {STATUSES[r["status"]]}' for r in rows) or 'Нет записей.') + f'\nСледующая: /list {page + 1}')
            else:
                if len(args) != (3 if cmd == '/status' else 2) or not re.fullmatch(r'[1-9][0-9]{0,12}', args[1]):
                    say(db, uid, 'Формат: /view НОМЕР или /status НОМЕР new|review|done')
                    return
                row = db.execute("SELECT * FROM proposals WHERE id=? AND status!='draft'", (int(args[1]),)).fetchone()
                if not row:
                    say(db, uid, 'Предложение не найдено.')
                elif cmd == '/view':
                    say(db, uid, f'№{row["id"]} · {STATUSES[row["status"]]}\n{row["created"]} UTC\n{row["author"]}\n\n{row["body"]}')
                    for file in db.execute('SELECT * FROM files WHERE proposal_id=?', (row['id'],)):
                        queue(db, 'sendDocument', {'chat_id': uid, 'document': file['file_id'], 'caption': f'Предложение №{row["id"]}'})
                elif args[2] not in STATUSES:
                    say(db, uid, 'Допустимые статусы: new, review, done.')
                else:
                    db.execute('UPDATE proposals SET status=? WHERE id=?', (args[2], row['id']))
                    say(db, uid, 'Статус обновлён.')
                    say(db, row['user_id'], f'Предложение №{row["id"]}: {STATUSES[args[2]]}.')
        elif cmd.startswith('/'):
            say(db, uid, 'Неизвестная команда. Справка: /help')
        elif draft:
            doc = m.get('document')
            body = text or m.get('caption', '').strip()
            if not doc and not body:
                say(db, uid, 'Пришлите текст или файл как «Документ». Голосовые и фотографии пока не обрабатываются.')
                return
            if len(draft['body']) + len(body) > 20000:
                say(db, uid, 'Лимит текста — 20 000 символов. Дополнительные материалы можно приложить файлом.')
                return
            if doc:
                count = db.execute('SELECT count(*) FROM files WHERE proposal_id=?', (draft['id'],)).fetchone()[0]
                if count >= 10:
                    say(db, uid, 'Лимит — 10 файлов на предложение.')
                    return
                db.execute('INSERT INTO files(proposal_id,file_id,name) VALUES (?,?,?)', (draft['id'], doc['file_id'], doc.get('file_name', 'document')))
            if body:
                db.execute('UPDATE proposals SET body=body || ? WHERE id=?', ('\n' + body, draft['id']))
            say(db, uid, 'Добавлено в черновик. Пришлите ещё материалы или /done для отправки.')
        else:
            say(db, uid, 'Для подачи материалов нажмите «Подать предложение».', True)


def api(token, method, payload):
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read())
        except Exception:
            return {'ok': False, 'error_code': error.code}
    except Exception:
        return {'ok': False, 'error_code': 503}


def deliver(path, token):
    while True:
        with connect(path) as db:
            row = db.execute("SELECT * FROM outbox WHERE state='pending' AND next_try<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
        if not row:
            time.sleep(1)
            continue
        result = api(token, row['method'], json.loads(row['payload']))
        with connect(path) as db:
            if result.get('ok'):
                db.execute('DELETE FROM outbox WHERE id=?', (row['id'],))
            else:
                code = result.get('error_code', 503)
                delay = max(result.get('parameters', {}).get('retry_after', 0), min(300, 2 ** min(row['attempts'] + 1, 8)))
                state = 'failed' if code in (400, 401, 403, 404) else 'pending'
                db.execute('UPDATE outbox SET attempts=attempts+1,next_try=?,state=? WHERE id=?', (time.time() + delay, state, row['id']))
                print(f'Delivery failed: outbox={row["id"]}, code={code}, state={state}', flush=True)
        time.sleep(0.1)


def handler_factory(path, secret, admins):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not log incoming content or credentials.

        def reply(self, code, text):
            data = text.encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != '/health':
                return self.reply(404, 'Not found')
            try:
                with connect(path) as db:
                    db.execute('SELECT count(*) FROM updates').fetchone()
                self.reply(200, 'ok')
            except Exception:
                self.reply(503, 'Database unavailable')

        def do_POST(self):
            if self.path != '/webhook':
                return self.reply(404, 'Not found')
            supplied = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if not hmac.compare_digest(supplied.encode(), secret.encode()):
                return self.reply(403, 'Forbidden')
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1048576:
                    return self.reply(413, 'Invalid size')
                self.connection.settimeout(15)
                update = json.loads(self.rfile.read(length))
                if not isinstance(update, dict) or not isinstance(update.get('update_id'), int):
                    return self.reply(400, 'Invalid update')
            except (ValueError, TimeoutError):
                return self.reply(400, 'Invalid request')
            try:
                process(path, admins, update)
                self.reply(200, 'ok')
            except Exception:
                print('Update processing failed; Telegram may retry.', flush=True)
                self.reply(500, 'Retry later')
    return Handler


def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    secret = os.environ.get('WEBHOOK_SECRET', '')
    if not token or not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', secret):
        raise SystemExit('Set TELEGRAM_BOT_TOKEN and WEBHOOK_SECRET (32–256 ASCII letters, digits, _ or -).')
    try:
        admins = {int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()}
    except ValueError:
        raise SystemExit('ADMIN_IDS must contain numeric Telegram user IDs separated by commas.')
    path = os.environ.get('DB_PATH', './data/bot.sqlite3')
    if os.environ.get('RENDER') and not path.startswith('/var/data/'):
        raise SystemExit('On Render, set DB_PATH=/var/data/bot.sqlite3 and attach a persistent disk.')
    initialize(path)
    base = os.environ.get('RENDER_EXTERNAL_URL') or os.environ.get('PUBLIC_URL')
    if not base or not base.startswith('https://'):
        raise SystemExit('PUBLIC_URL or RENDER_EXTERNAL_URL must be an HTTPS service URL.')
    result = api(token, 'setWebhook', {'url': base.rstrip('/') + '/webhook', 'secret_token': secret,
        'allowed_updates': ['message'], 'max_connections': 1})
    if not result.get('ok'):
        raise SystemExit('Webhook registration failed. Check token, service URL and Telegram availability.')
    threading.Thread(target=deliver, args=(path, token), daemon=True).start()
    print('Bot started. Webhook configured.', flush=True)
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '10000'))), handler_factory(path, secret, admins)).serve_forever()


if __name__ == '__main__':
    main()
