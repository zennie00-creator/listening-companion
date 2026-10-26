import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import os
import uuid
import zipfile
from listening.auth import hash_password, valid_password
from listening.links import manual_album, MusicError
from listening.store import Store, Conflict
from listening.backup import read_backup

PHOTO = b'\xff\xd8\xffsynthetic-photo-bytes'


def backup(document=None):
    doc = document or {'schemaVersion': 1, 'records': {
        'note': [{'id': 'note-1', 'kind': '감상 대화', 'content': '긴 원문\n' * 700,
                  'photoKey': 'old-photo.jpg', 'createdAt': '2026-09-01T10:00:00Z', 'revision': 2}],
        'environment': [{'id': 'room-1', 'name': '청음실', 'details': '배치\n' * 900, 'revision': 1}],
        'experiment': [], 'run': [], 'reaction': []},
        'histories': [{'kind': 'note', 'recordId': 'note-1', 'revision': 1,
                       'payload': {'id': 'note-1', 'content': '처음 감상', 'revision': 1}, 'savedAt': '2026-09-02T10:00:00Z'}],
        'photos': [{'noteId': 'note-1', 'path': 'photos/note-1.jpg', 'contentType': 'image/jpeg', 'sha256': hashlib.sha256(PHOTO).hexdigest()}]}
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('records.json', json.dumps(doc, ensure_ascii=False))
        archive.writestr('photos/note-1.jpg', PHOTO)
    return data.getvalue()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.url = 'sqlite:///' + str(Path(self.folder.name) / 'records.sqlite3')
        self.store = Store(self.url)

    def tearDown(self):
        self.folder.cleanup()

    def test_lossless_import_roundtrip_and_idempotence(self):
        original, _ = read_backup(backup())
        counts = self.store.import_backup(backup(), dry_run=False)
        self.assertEqual((counts['note'], counts['photos'], counts['histories']), (1, 1, 1))
        self.store.import_backup(backup(), dry_run=False)
        exported, pictures = read_backup(self.store.export())
        self.assertEqual(exported['records'], original['records'])
        self.assertEqual(exported['histories'], original['histories'])
        self.assertEqual(pictures[0][1], PHOTO)

    def test_preview_does_not_save(self):
        self.store.import_backup(backup())
        self.assertEqual(self.store.list('note'), [])
        self.assertIsNone(self.store.photo('note-1'))

    def test_conflicting_import_rolls_back_all_records(self):
        self.store.save('note', {'id': 'note-1', 'content': '기존 원문'})
        with self.assertRaises(Conflict):
            self.store.import_backup(backup(), dry_run=False)
        self.assertEqual(self.store.get('note', 'note-1')['content'], '기존 원문')
        self.assertEqual(self.store.list('environment'), [])
        self.assertIsNone(self.store.photo('note-1'))

    def test_stale_edit_keeps_history_and_latest(self):
        first = self.store.save('note', {'content': '원문'})
        second = self.store.save('note', dict(first, content='보완'), expected_revision=1)
        with self.assertRaises(Conflict):
            self.store.save('note', dict(first, content='낡은 수정'), expected_revision=1)
        self.assertEqual(self.store.get('note', first['id']), second)
        self.assertEqual(self.store.revisions('note', first['id'])[0]['note'], first)

    def test_owner_isolation(self):
        self.store.import_backup(backup(), dry_run=False)
        other = Store(self.url, owner='other')
        self.assertEqual(other.list('note'), [])
        self.assertEqual(other.revisions('note', 'note-1'), [])
        self.assertIsNone(other.photo('note-1'))

    def test_exactly_one_default_environment(self):
        first = self.store.save('environment', {'name': '집', 'isDefault': 1})
        self.store.save('environment', {'name': '밖', 'isDefault': 1})
        self.assertEqual(sum(e['isDefault'] for e in self.store.list('environment')), 1)
        self.assertEqual(self.store.get('environment', first['id'])['revision'], 2)

    def test_tampered_photo_and_missing_photo_rejected(self):
        doc, _ = read_backup(backup())
        doc['photos'][0]['sha256'] = 'wrong'
        with self.assertRaises(ValueError):
            read_backup(backup(doc))
        doc['photos'] = []
        with self.assertRaises(ValueError):
            read_backup(backup(doc))

    def test_duplicate_record_and_path_traversal_rejected(self):
        doc, _ = read_backup(backup())
        doc['records']['note'].append(copy.deepcopy(doc['records']['note'][0]))
        with self.assertRaises(ValueError):
            read_backup(backup(doc))
        doc['records']['note'].pop()
        doc['photos'][0]['path'] = 'photos/../../etc/passwd'
        with self.assertRaises(ValueError):
            read_backup(backup(doc))

    def test_links_validate_hosts_and_album_paths(self):
        self.assertEqual(manual_album('https://music.apple.com/kr/album/title/123456?i=999', '음반', '연주자')['url'], 'https://music.apple.com/kr/album/123456')
        self.assertEqual(manual_album('https://listen.tidal.com/album/123', '음반', '연주자')['url'], 'https://tidal.com/browse/album/123')
        for url in ['http://tidal.com/album/123', 'https://tidal.com/track/123', 'https://music.apple.com.evil.com/kr/album/123', 'https://user:pass@tidal.com/album/123', 'https://tidal.com:123/album/123', 'javascript:alert(1)']:
            with self.assertRaises(MusicError):
                manual_album(url, '음반', '연주자')

    def test_password_hash_and_persistent_rate_gate(self):
        hashed = hash_password('synthetic test password')
        self.assertTrue(valid_password('synthetic test password', hashed))
        self.assertFalse(valid_password('wrong', hashed))
        self.assertFalse(valid_password('anything', 'malformed'))
        for _ in range(10):
            self.store.login_result(False)
        self.assertGreater(Store(self.url, owner=self.store.owner).login_blocked(), 0)
        self.store.login_result(True)
        self.assertEqual(self.store.login_blocked(), 0)


@unittest.skipUnless(os.getenv('LC_TEST_POSTGRES_URL'), 'PostgreSQL test service is not configured')
class PostgreSQLCoreTests(CoreTests):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.url = os.environ['LC_TEST_POSTGRES_URL']
        self.store = Store(self.url, owner='test-' + str(uuid.uuid4()))


if __name__ == '__main__':
    unittest.main()
