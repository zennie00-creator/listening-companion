"""Lossless, size-bounded backup validation. No database/network dependencies."""
import hashlib
import io
import json
import zipfile
KINDS = ('note', 'environment', 'experiment', 'run', 'reaction')

def read_backup(content):
    if len(content) > 80 * 1024 * 1024:
        raise ValueError('백업은 80MB 이하로 선택해 주세요.')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(e.file_size for e in entries) > 100 * 1024 * 1024 or len({e.filename for e in entries}) != len(entries):
            raise ValueError('백업 크기 또는 중복 경로를 확인해 주세요.')
        document = json.loads(archive.read('records.json'))
        if document.get('schemaVersion') != 1 or not isinstance(document.get('records'), dict):
            raise ValueError('지원하지 않는 백업 형식입니다.')
        ids = {}
        for kind, rows in document['records'].items():
            if kind not in KINDS or not isinstance(rows, list):
                raise ValueError('백업의 기록 종류를 확인해 주세요.')
            seen = set()
            for row in rows:
                record_id = row.get('id') if isinstance(row, dict) else None
                if not isinstance(record_id, str) or not record_id or len(record_id) > 200 or record_id in seen:
                    raise ValueError('기록 ID가 없거나 중복됩니다.')
                if not isinstance(row.get('revision', 1), int) or row.get('revision', 1) < 1:
                    raise ValueError('수정 번호를 확인해 주세요.')
                seen.add(record_id)
            ids[kind] = seen
        photos = []
        seen_photos = set()
        for photo in document.get('photos', []):
            path = photo['path']
            if not path.startswith('photos/') or '..' in path.split('/') or path.startswith('/') or '\\' in path:
                raise ValueError('사진 파일 경로를 확인해 주세요.')
            if photo['noteId'] not in ids.get('note', set()) or photo['noteId'] in seen_photos:
                raise ValueError('사진과 감상 ID가 일치하지 않습니다.')
            data = archive.read(path)
            if hashlib.sha256(data).hexdigest() != photo['sha256']:
                raise ValueError('사진 검증값이 다릅니다. 파일이 손상됐습니다.')
            if photo['contentType'] not in ('image/jpeg', 'image/png') or not (data.startswith(b'\xff\xd8\xff') if photo['contentType'] == 'image/jpeg' else data.startswith(b'\x89PNG\r\n\x1a\n')):
                raise ValueError('사진 형식을 확인해 주세요.')
            seen_photos.add(photo['noteId'])
            photos.append((photo, data))
        expected = {row['id'] for row in document['records'].get('note', []) if row.get('photoKey')}
        if expected - seen_photos:
            raise ValueError('감상에 연결된 사진이 백업에 빠져 있습니다.')
        history_ids = set()
        for history in document.get('histories', []):
            key = (history.get('kind'), history.get('recordId'), history.get('revision'))
            if history.get('recordId') not in ids.get(history.get('kind'), set()) or key in history_ids or not isinstance(history.get('payload'), dict):
                raise ValueError('수정 이력과 원본을 확인해 주세요.')
            history_ids.add(key)
        return document, photos
