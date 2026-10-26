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
        self.env = patch.dict(os.environ, {'LC_DEPLOYMENT_MODE': 'local', 'DATABASE_URL': self.url, 'OPENAI_API_KEY': ''})
        self.env.start()
        self.path = str(Path(__file__).resolve().parents[1] / 'streamlit_app.py')

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_all_sections_and_journal_save(self):
        at = self.AppTest.from_file(self.path, default_timeout=30).run()
        self.assertEqual(len(at.exception), 0)
        for section in ['감상 기록', '청음 실험', '청취 환경', '나의 취향', '백업·가져오기', '오늘의 음악']:
            at.sidebar.radio[0].set_value(section).run()
            self.assertEqual(len(at.exception), 0, section)
        at.sidebar.radio[0].set_value('감상 기록').run()
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


if __name__ == '__main__':
    unittest.main()
