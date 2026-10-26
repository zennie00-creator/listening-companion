"""Convert a private Sites export into a portable Listening Companion ZIP.
No network, API keys, or user data are embedded in source code.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from datetime import datetime, timezone


def convert(source, destination):
    root = Path(source).resolve()
    original = json.loads((root / 'records-full.json').read_text())
    document = {'schemaVersion': 1, 'exportedAt': datetime.now(timezone.utc).isoformat(),
        'records': {'note': original['notes'], 'environment': original['environments'],
                    'experiment': original['experiments'], 'run': [], 'reaction': []},
        'histories': [], 'photos': []}
    for note_id, history in original['histories'].items():
        for entry in history:
            document['histories'].append({'kind': 'note', 'recordId': note_id, 'revision': entry['revision'],
                                         'payload': entry['note'], 'savedAt': entry['savedAt']})
    for detail in original['experimentDetails']:
        if detail.get('nextCursor'):
            raise ValueError('실험 결과의 다음 페이지가 빠져 있습니다.')
        document['records']['run'].extend(detail['runs'])
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        seen = set()
        for photo in original['photos']:
            path = (root / photo['path']).resolve()
            if root not in path.parents:
                raise ValueError('사진 경로가 백업 밖을 가리킵니다.')
            data = path.read_bytes()
            if photo['encoding'] != 'original' or not data.startswith(b'\xff\xd8\xff'):
                raise ValueError('사진 원본 또는 형식을 확인해 주세요.')
            archive.writestr(photo['path'], data)
            document['photos'].append({**photo, 'sha256': hashlib.sha256(data).hexdigest()})
            seen.add(photo['noteId'])
        if {note['id'] for note in original['notes'] if note.get('photoKey')} != seen:
            raise ValueError('사진이 누락됐습니다.')
        archive.writestr('records.json', json.dumps(document, ensure_ascii=False, indent=2))
    return {**{key: len(value) for key, value in document['records'].items()},
            'histories': len(document['histories']), 'photos': len(document['photos'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', help='Private export folder; never commit it')
    parser.add_argument('destination', help='Private ZIP path; never commit it')
    args = parser.parse_args()
    print(json.dumps(convert(args.source, args.destination)))
