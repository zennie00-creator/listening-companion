import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from listening.store import Store

HAS_STREAMLIT = importlib.util.find_spec('streamlit') is not None


@unittest.skipUnless(HAS_STREAMLIT, 'Streamlit must be installed for UI tests')
class UITests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        self.AppTest = AppTest
        self.folder = tempfile.TemporaryDirectory()
        self.url = 'sqlite:///' + str(Path(self.folder.name) / 'ui.sqlite3')
        self.env = patch.dict(os.environ, {'LC_DEPLOYMENT_MODE': 'local', 'DATABASE_URL': self.url, 'OPENAI_API_KEY': '', 'STORAGE_BACKEND': 'postgres'})
        self.env.start()
        self.path = str(Path(__file__).resolve().parents[1] / 'streamlit_app.py')

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_all_sections_and_journal_save(self):
        at = self.AppTest.from_file(self.path, default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        for section in ['감상 기록', '청음 실험', '청취 환경', '나의 취향', '백업·가져오기', '오늘의 음악']:
            at.radio[0].set_value(section).run()
            self.assertEqual(len(at.exception), 0, section)
            self.assertEqual(len(at.error), 0, section)
        at.radio[0].set_value('감상 기록').run()
        next(w for w in at.text_area if w.label == '감상').set_value('합성 테스트 감상')
        next(w for w in at.text_input if w.label == '앨범 공유 주소').set_value('https://tidal.com/browse/album/123')
        next(w for w in at.text_input if w.label == '직접 연결할 음반명').set_value('테스트 앨범')
        next(w for w in at.text_input if w.label == '직접 연결할 아티스트 · 연주자').set_value('테스트 연주자')
        next(w for w in at.button if w.label == '감상 저장하기').click().run()
        self.assertEqual(len(at.exception), 0)
        notes = Store(self.url).list('note')
        self.assertEqual(notes[0]['content'], '합성 테스트 감상')
        self.assertTrue(__import__('json').loads(notes[0]['albumDetails'])['userProvided'])

    def test_cloud_refuses_ephemeral_sqlite_before_showing_data(self):
        Store(self.url).save('note', {'content': 'must-not-be-visible'})
        with patch.dict(os.environ, {'LC_DEPLOYMENT_MODE': 'cloud'}):
            at = self.AppTest.from_file(self.path, default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any('영구 보관' in item.value for item in at.info))
        self.assertFalse(any('must-not-be-visible' in item.value for item in at.markdown))

    def test_recent_records_are_visible_on_home_and_open_in_editor(self):
        import io
        from PIL import Image
        picture = io.BytesIO()
        Image.new('RGB', (4, 4), 'white').save(picture, format='JPEG')
        store = Store(self.url)
        note = store.save('note', {'kind': '감상 대화', 'content': '기존 형식의 합성 감상 원문', 'photoKey': 'old-photo.jpg'}, photo=picture.getvalue())
        at = self.AppTest.from_file(self.path, default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any('기존 형식의 합성 감상 원문' in item.value for item in at.markdown))
        next(w for w in at.button if w.label == '감상 보기·보완').click().run()
        self.assertEqual(at.radio[0].value, '감상 기록')
        self.assertEqual(next(w for w in at.text_area if w.label == '감상').value, note['content'])

    def test_imported_array_scores_can_be_corrected_without_losing_original(self):
        import json
        store = Store(self.url)
        experiment = store.save('experiment', {'title': '합성 A/B 실험', 'criteria': ['공간감']})
        scores = [{'criterion': '공간감', 'a': 4, 'b': 5, 'impression': 'B가 나음', 'detail': '처음 들은 차이'}]
        run = store.save('run', {'experimentId': experiment['id'], 'listenedOn': '2026-10-26', 'observationsA': 'A 관찰', 'observationsB': 'B 관찰', 'scores': scores, 'decision': 'B 채택', 'conclusion': '최초 결론'})
        at = self.AppTest.from_file(self.path, default_timeout=30).run()
        at.radio[0].set_value('청음 실험').run()
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(len(at.error), 0)
        next(w for w in at.selectbox if w.label == '결과 추가 / 한 번 정정').set_value(run['id']).run()
        next(w for w in at.text_input if w.label == '공간감 차이 메모').set_value('다시 들으며 보완')
        next(w for w in at.text_input if w.label == '정정 이유').set_value('같은 구간 재확인')
        next(w for w in at.button if w.label == '결과 한 번 정정').click().run()
        self.assertEqual(len(at.exception), 0)
        updated = store.get('run', run['id'])
        self.assertEqual(updated['scores'][0]['a'], 4)
        self.assertEqual(updated['scores'][0]['b'], 5)
        self.assertEqual(updated['scores'][0]['detail'], '다시 들으며 보완')
        self.assertEqual(json.loads(updated['originalSnapshot']), run)

    def test_editorial_point_opens_journal_and_preserves_point_id(self):
        import json
        item = json.loads((Path(self.path).parent / 'data/curated-albums.json').read_text())[0]
        at = self.AppTest.from_file(self.path, default_timeout=30).run()
        next(w for w in at.button if w.key == 'detail_' + item['id']).click().run()
        self.assertEqual(len(at.exception), 0)
        next(w for w in at.button if w.key == 'point_' + item['id'] + item['points'][0]['id']).click().run()
        self.assertEqual(at.radio[0].value, '감상 기록')
        next(w for w in at.text_area if w.label == '감상').set_value('감상 포인트를 따라 들은 합성 기록')
        next(w for w in at.button if w.label == '감상 저장하기').click().run()
        self.assertEqual(Store(self.url).list('note')[0]['pointId'], item['points'][0]['id'])

    def test_notion_missing_settings_never_falls_back_to_local_data(self):
        Store(self.url).save('note', {'content': 'must-not-be-visible'})
        with patch.dict(os.environ, {'LC_DEPLOYMENT_MODE': 'cloud', 'STORAGE_BACKEND': 'notion', 'NOTION_TOKEN': '', 'NOTION_PAGE_ID': ''}):
            at = self.AppTest.from_file(self.path, default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any('NOTION_TOKEN' in item.value for item in at.info))
        self.assertFalse(any('must-not-be-visible' in item.value for item in at.markdown))

    def test_notion_cloud_login_and_save(self):
        from test_notion import FakeNotion, ROOT
        from listening.notion_store import NotionStore
        api = FakeNotion()
        store = NotionStore('', ROOT, api=api)
        settings = {'LC_DEPLOYMENT_MODE': 'cloud', 'STORAGE_BACKEND': 'notion',
                    'NOTION_TOKEN': 'synthetic-ci-only-token', 'NOTION_PAGE_ID': ROOT,
                    'APP_PASSWORD': 'synthetic test password', 'APP_PASSWORD_HASH': ''}
        with patch.dict(os.environ, settings), patch('listening.notion_store.NotionStore', return_value=store):
            at = self.AppTest.from_file(self.path, default_timeout=30).run()
            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.radio), 0)
            next(w for w in at.text_input if w.label == '앱 비밀번호').set_value('synthetic test password')
            next(w for w in at.button if w.label == '감상실 열기').click().run()
            self.assertEqual(len(at.exception), 0)
            at.radio[0].set_value('감상 기록').run()
            next(w for w in at.text_area if w.label == '감상').set_value('Notion 합성 테스트 감상')
            next(w for w in at.button if w.label == '감상 저장하기').click().run()
            self.assertEqual(len(at.exception), 0)
            self.assertEqual(len(at.error), 0)
        self.assertEqual(NotionStore('', ROOT, api=api).list('note')[0]['content'], 'Notion 합성 테스트 감상')

    def test_reader_code_update_replaces_old_cached_storage_object(self):
        from test_notion import FakeNotion, ROOT
        from listening.notion_store import NotionStore
        old = NotionStore('', ROOT, api=FakeNotion())
        fresh = NotionStore('', ROOT, api=FakeNotion())
        fresh.save('note', {'content': '새 읽기 코드가 찾은 합성 기록'})
        version = [b'reader version one']
        original_read = Path.read_bytes
        def source_bytes(path):
            return version[0] if path.name == 'notion_store.py' else original_read(path)
        env = {'LC_DEPLOYMENT_MODE': 'local', 'STORAGE_BACKEND': 'notion',
               'NOTION_TOKEN': 'synthetic-cache-test-only', 'NOTION_PAGE_ID': ROOT}
        with patch.dict(os.environ, env), patch.object(Path, 'read_bytes', source_bytes), patch('listening.notion_store.NotionStore', return_value=old) as factory:
            at = self.AppTest.from_file(self.path, default_timeout=30).run()
            self.assertFalse(any('새 읽기 코드가 찾은 합성 기록' in item.value for item in at.markdown))
            version[0] = b'reader version two'
            factory.return_value = fresh
            at.run()
            self.assertEqual(len(at.exception), 0)
            self.assertTrue(any('새 읽기 코드가 찾은 합성 기록' in item.value for item in at.markdown))


if __name__ == '__main__':
    unittest.main()
