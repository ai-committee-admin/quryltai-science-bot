import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from app import initialize, process, connect, handler_factory

class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'test.sqlite3')
        initialize(self.path)
        self.seq = 0

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, text='', uid=1, **extra):
        self.seq += 1
        update = {'update_id': self.seq, 'message': {'from': {'id': uid, 'first_name': 'Expert'}, 'chat': {'id': uid, 'type': 'private'}, 'text': text, **extra}}
        process(self.path, {99}, update)
        return update

    def query(self, sql, params=()):
        with connect(self.path) as db:
            return db.execute(sql, params).fetchall()

    def submit(self, uid=1):
        self.send('/new', uid)
        self.send('Организация; проблема; предлагаемое решение', uid)
        self.send('/done', uid)

    def test_intake_duplicate_files_and_restart(self):
        self.send('/new')
        event = self.send('Описание')
        process(self.path, {99}, event)
        self.send(document={'file_id': 'telegram-file', 'file_name': 'report.pdf'})
        event = self.send('/done')
        process(self.path, {99}, event)
        initialize(self.path)
        self.assertEqual(len(self.query('SELECT * FROM proposals')), 1)
        self.assertEqual(self.query('SELECT body,status FROM proposals')[0]['body'], '\nОписание')
        self.assertEqual(self.query('SELECT status FROM proposals')[0]['status'], 'new')
        self.assertEqual(len(self.query('SELECT * FROM files')), 1)
        self.assertEqual(sum('зарегистрировано' in r['payload'] for r in self.query('SELECT payload FROM outbox')), 1)

    def test_unauthorized_admin_and_private_statuses(self):
        self.submit()
        self.send('/status 1 done', uid=2)
        self.assertEqual(self.query('SELECT status FROM proposals')[0]['status'], 'new')
        self.send('/view 1', uid=2)
        self.assertIn('только администратору', self.query('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1')[0]['payload'])
        self.send('/my', uid=2)
        self.assertIn('пока нет', self.query('SELECT payload FROM outbox ORDER BY id DESC LIMIT 1')[0]['payload'])
        self.send('/status 1 review', uid=99)
        self.assertEqual(self.query('SELECT status FROM proposals')[0]['status'], 'review')

    def test_empty_and_cancel_and_groups(self):
        self.send('/new')
        self.send(document={'file_id': 'file'})
        self.send('/done')
        self.assertEqual(self.query('SELECT status FROM proposals')[0]['status'], 'draft')
        self.send('/cancel')
        self.assertEqual(len(self.query('SELECT * FROM files')), 0)
        self.send('/new', chat={'id': -1, 'type': 'group'})
        self.assertEqual(len(self.query('SELECT * FROM proposals')), 0)

    def test_webhook_auth_and_health(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_factory(self.path, 'a' * 32, {99}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(base + '/health') as r:
                self.assertEqual(r.status, 200)
            req = urllib.request.Request(base + '/webhook', data=b'{"update_id":99}', headers={'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(req)
            self.assertEqual(err.exception.code, 403)
            self.assertEqual(len(self.query('SELECT * FROM updates')), 0)
            req.add_header('X-Telegram-Bot-Api-Secret-Token', 'a' * 32)
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

if __name__ == '__main__':
    unittest.main()
