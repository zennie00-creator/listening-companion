import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch
from listening.auth import valid_secret_password
from listening.backup import read_backup
from listening.notion_store import NotionAPI, NotionStore, NotionError, PART_SIZE, page_id
from listening.store import Conflict
import test_core
from test_core import backup, PHOTO

ROOT = '00000000-0000-0000-0000-000000000001'


class FakeNotion:
    """Stateful fake API: real ZIP uploads, pages and expiring URL read contract."""
    def __init__(self):
        self.pages, self.uploads, self.roots = {}, {}, {}
        self.fail_upload = False
        self.timeout_after_create = False
        self.before_create = None
        self.corrupt_download = False

    def request(self, method, path, **kwargs):
        if method == 'GET' and path.startswith('pages/'):
            return {'id': path.split('/')[-1]}
        if method == 'POST' and path == 'pages':
            if self.before_create:
                callback, self.before_create = self.before_create, None
                callback()
            page = copy.deepcopy(kwargs['json'])
            ident = str(uuid.uuid4())
            page['id'] = ident
            for block in page['children']:
                block['id'] = str(uuid.uuid4())
                if block['type'] == 'file':
                    file = block['file']
                    file['file'] = {'url': 'https://files.notion.so/' + file['file_upload']['id']}
                    file['type'] = 'file'
                    del file['file_upload']
            self.pages[ident] = page
            parent = page['parent']['page_id']
            title = page['properties']['title']['title'][0]['text']['content']
            self.roots.setdefault(parent, []).append({'id': ident, 'type': 'child_page', 'child_page': {'title': title}})
            if self.timeout_after_create:
                self.timeout_after_create = False
                raise NotionError('synthetic timeout')
            return {'id': ident}
        raise AssertionError((method, path))

    def children(self, ident):
        return copy.deepcopy(self.pages[ident]['children'] if ident in self.pages else self.roots.get(ident, []))

    def upload(self, filename, data):
        if self.fail_upload:
            raise NotionError('synthetic upload failure')
        ident = str(uuid.uuid4())
        self.uploads[ident] = bytes(data)
        return ident

    def download(self, url, limit):
        data = self.uploads[url.split('/')[-1]]
        return data + b'corrupt' if self.corrupt_download else data


class NotionCoreTests(test_core.CoreTests):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.api = FakeNotion()
        self.store = NotionStore('', ROOT, api=self.api)

    def test_owner_isolation(self):
        self.store.import_backup(backup(), dry_run=False)
        other = NotionStore('', ROOT, owner='other', api=self.api)
        self.assertEqual(other.list('note'), [])
        self.assertEqual(other.revisions('note', 'note-1'), [])
        self.assertIsNone(other.photo('note-1'))

    def test_password_hash_and_persistent_rate_gate(self):
        # Notion's login gate is shared in process, explicitly not persistent.
        for _ in range(10):
            self.store.login_result(False)
        self.assertGreater(self.store.login_blocked(), 0)
        self.store.login_result(True)
        self.assertEqual(self.store.login_blocked(), 0)
        self.assertTrue(valid_secret_password('synthetic test password', 'synthetic test password'))
        self.assertFalse(valid_secret_password('wrong', 'synthetic test password'))
        self.assertFalse(valid_secret_password('short', 'short'))


class NotionFailureTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeNotion()
        self.store = NotionStore('', ROOT, api=self.api)

    def test_new_process_reads_original_photos_and_history(self):
        self.store.import_backup(backup(), dry_run=False)
        other = NotionStore('', ROOT, api=self.api)
        document, photos = read_backup(other.export())
        original, _ = read_backup(backup())
        self.assertEqual(document['records'], original['records'])
        self.assertEqual(document['histories'], original['histories'])
        self.assertEqual(photos[0][1], PHOTO)
        count = len(self.api.pages)
        other.import_backup(backup(), dry_run=False)
        self.assertEqual(len(self.api.pages), count)

    def test_diagnostics_reports_actual_record_coverage_without_note_text(self):
        empty = self.store.diagnostics()
        self.assertFalse(empty['snapshotFound'])
        self.assertEqual(empty['records']['note'], 0)
        self.store.import_backup(backup(), dry_run=False)
        report = self.store.diagnostics()
        self.assertTrue(report['snapshotFound'])
        self.assertEqual(report['records']['note'], 1)
        self.assertEqual(report['blockTypes'], {'child_page': 1})
        self.assertEqual(report['childPages'][0]['recognized'], True)
        self.assertNotIn('긴 원문', json.dumps(report, ensure_ascii=False))
        self.assertNotIn('Authorization', json.dumps(report))

    def test_preview_and_upload_failure_never_publish(self):
        self.store.import_backup(backup())
        self.assertEqual(len(self.api.pages), 0)
        self.api.fail_upload = True
        with self.assertRaises(NotionError):
            self.store.save('note', {'content': 'not saved'})
        self.assertEqual(NotionStore('', ROOT, api=self.api).list('note'), [])

    def test_timeout_after_creation_resolves_without_duplicate(self):
        self.api.timeout_after_create = True
        note = self.store.save('note', {'content': 'created remotely'})
        self.assertEqual(len(self.api.pages), 1)
        self.assertEqual(NotionStore('', ROOT, api=self.api).get('note', note['id']), note)

    def test_corrupt_or_missing_attachment_stops_read(self):
        self.store.import_backup(backup(), dry_run=False)
        self.api.corrupt_download = True
        with self.assertRaises(NotionError):
            NotionStore('', ROOT, api=self.api).list('note')
        self.api.corrupt_download = False
        for page in self.api.pages.values():
            page['children'] = [b for b in page['children'] if b['type'] != 'file']
        with self.assertRaises(NotionError):
            NotionStore('', ROOT, api=self.api).list('note')

    def test_concurrent_servers_preserve_both_and_refuse_last_writer_wins(self):
        self.store.save('note', {'content': 'base'})
        other = NotionStore('', ROOT, api=self.api)
        self.api.before_create = lambda: other.save('note', {'content': 'other server'})
        with self.assertRaises(Conflict):
            self.store.save('note', {'content': 'this server'})
        self.assertEqual(len(self.api.pages), 3)
        with self.assertRaises(Conflict):
            NotionStore('', ROOT, api=self.api).list('note')

    def test_missing_parent_and_cycle_are_not_an_empty_store(self):
        self.store.save('note', {'content': 'base'})
        self.store.save('note', {'content': 'next'})
        self.api.roots[ROOT].pop(0)
        with self.assertRaises(NotionError):
            NotionStore('', ROOT, api=self.api).list('note')

    def test_split_archive_under_free_file_limit_and_roundtrip(self):
        picture = b'\xff\xd8\xff' + os.urandom(PART_SIZE + 2000)
        self.store.save('note', {'content': 'large photo', 'photoKey': 'large.jpg'}, photo=picture)
        self.assertEqual(len(self.api.uploads), 2)
        self.assertTrue(all(len(part) < 5 * 1024 * 1024 for part in self.api.uploads.values()))
        notes = NotionStore('', ROOT, api=self.api).list('note')
        self.assertEqual(NotionStore('', ROOT, api=self.api).photo(notes[0]['id'])[0], picture)

    def test_page_id_and_url(self):
        self.assertEqual(page_id('https://app.notion.com/p/Listening-Companion-' + ROOT.replace('-', '') + '?source=copy_link'), ROOT)
        self.assertEqual(page_id('https://www.notion.so/feed-' + ROOT.replace('-', '')), ROOT)
        self.assertEqual(page_id(ROOT), ROOT)
        with self.assertRaises(ValueError):
            page_id('https://evil.com/' + ROOT)

    def test_pagination_and_repeated_cursor(self):
        api = object.__new__(NotionAPI)
        calls = []
        def request(method, path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {'results': [{'id': '1'}], 'has_more': True, 'next_cursor': 'next'}
            return {'results': [{'id': '2'}], 'has_more': False}
        api.request = request
        self.assertEqual(api.children(ROOT), [{'id': '1'}, {'id': '2'}])
        self.assertEqual(calls[1]['params']['start_cursor'], 'next')
        api.request = lambda *a, **kw: {'results': [], 'has_more': True, 'next_cursor': 'loop'}
        with self.assertRaises(NotionError):
            api.children(ROOT)

    def text_seed(self, transform=None):
        self.store.import_backup(backup(), dry_run=False)
        page = next(iter(self.api.pages.values()))
        code = next(b for b in page['children'] if b['type'] == 'code')
        manifest = json.loads(code['code']['rich_text'][0]['text']['content'])
        original_part = next(iter(self.api.uploads.values()))
        with zipfile.ZipFile(io.BytesIO(original_part)) as archive:
            raw = archive.read('snapshot.part')
        manifest['parts'] = []
        page['children'] = [b for b in page['children'] if b['type'] != 'file']
        midpoint = len(raw) // 2
        for i, chunk in enumerate((raw[:midpoint], raw[midpoint:])):
            data = json.dumps({'format': 'listening-notion-part-1', 'data': base64.b64encode(chunk).decode()}).encode()
            if transform:
                data = transform(data)
            name = f'part-{i:03d}.json'
            ident = self.api.upload(name, data)
            manifest['parts'].append({'name': name, 'encoding': 'base64-json', 'sha256': hashlib.sha256(data).hexdigest()})
            page['children'].append({'type': 'file', 'file': {'name': name, 'type': 'file', 'file': {'url': 'https://files.notion.so/' + ident}}})
        code['code']['rich_text'][0]['text']['content'] = json.dumps(manifest)

    def test_text_seed_lossless_read_and_next_zip_save(self):
        self.text_seed()
        fresh = NotionStore('', ROOT, api=self.api)
        exported, photos = read_backup(fresh.export())
        original, _ = read_backup(backup())
        self.assertEqual(exported['records'], original['records'])
        self.assertEqual(exported['histories'], original['histories'])
        self.assertEqual(photos[0][1], PHOTO)
        note = fresh.get('note', 'note-1')
        fresh.save('note', dict(note, content='after text seed'), expected_revision=2)
        newest = NotionStore('', ROOT, api=self.api)
        self.assertEqual(newest.get('note', 'note-1')['content'], 'after text seed')
        self.assertEqual(newest.photo('note-1')[0], PHOTO)
        self.assertEqual(newest.revisions('note', 'note-1')[0]['note'], note)

    def test_invalid_base64_and_oversized_text_parts_rejected(self):
        self.text_seed(lambda data: data.replace(b'"data": "', b'"data": "!'))
        with self.assertRaises(NotionError):
            NotionStore('', ROOT, api=self.api).export()
        self.setUp()
        self.text_seed(lambda data: data + b' ' * (200 * 1024))
        with self.assertRaises(NotionError):
            NotionStore('', ROOT, api=self.api).export()

    def test_text_encoding_extension_mismatch_rejected(self):
        self.text_seed()
        page = next(iter(self.api.pages.values()))
        code = next(b for b in page['children'] if b['type'] == 'code')
        manifest = json.loads(code['code']['rich_text'][0]['text']['content'])
        manifest['parts'][0]['encoding'] = 'zip'
        code['code']['rich_text'][0]['text']['content'] = json.dumps(manifest)
        with self.assertRaises(NotionError):
            NotionStore('', ROOT, api=self.api).export()


if __name__ == '__main__':
    unittest.main()
