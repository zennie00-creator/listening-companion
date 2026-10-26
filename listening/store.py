"""Portable SQL persistence. Original note payloads and media stay lossless."""
import hashlib
import io
import json
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .backup import KINDS, read_backup


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def encoded(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Conflict(ValueError):
    pass


class Database:
    def __init__(self, connection, postgres):
        self.connection, self.postgres = connection, postgres

    def execute(self, sql, values=()):
        return self.connection.execute(sql.replace('?', '%s') if self.postgres else sql, values)


class Store:
    def __init__(self, url, owner='owner'):
        self.owner = owner
        self.postgres = url.startswith(('postgres://', 'postgresql://', 'postgresql+psycopg://'))
        if self.postgres:
            self.url = url.replace('postgresql+psycopg://', 'postgresql://', 1)
        elif url.startswith('sqlite:///'):
            self.path = url[len('sqlite:///'):]
            if self.path == ':memory:':
                raise ValueError('Use a temporary file for local/test persistence.')
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError('PostgreSQL 또는 SQLite 연결 주소를 확인해 주세요.')
        binary = 'BYTEA' if self.postgres else 'BLOB'
        with self.db(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS lc_records (owner TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(owner,kind,id))')
            db.execute('CREATE TABLE IF NOT EXISTS lc_history (owner TEXT NOT NULL, kind TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL, saved_at TEXT NOT NULL, PRIMARY KEY(owner,kind,record_id,revision))')
            db.execute(f'CREATE TABLE IF NOT EXISTS lc_media (owner TEXT NOT NULL, note_id TEXT NOT NULL, data {binary} NOT NULL, mime TEXT NOT NULL, source_key TEXT, PRIMARY KEY(owner,note_id))')
            db.execute('CREATE TABLE IF NOT EXISTS lc_login_gate (owner TEXT PRIMARY KEY, failures INTEGER NOT NULL, blocked_until TEXT NOT NULL)')
            db.execute('INSERT INTO lc_login_gate(owner,failures,blocked_until) VALUES (?,0,?) ON CONFLICT(owner) DO NOTHING', (owner, '0'))

    @contextmanager
    def db(self, write=False):
        if self.postgres:
            import psycopg
            from psycopg.rows import dict_row
            connection = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=15)
            connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ' + ('' if write else ' READ ONLY'))
        else:
            connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        try:
            yield Database(connection, self.postgres)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list(self, kind):
        if kind not in KINDS:
            raise ValueError('Unknown record kind')
        with self.db() as db:
            rows = db.execute('SELECT payload FROM lc_records WHERE owner=? AND kind=?', (self.owner, kind)).fetchall()
        return sorted([json.loads(row['payload']) for row in rows],
                      key=lambda p: (p.get('createdAt', p.get('updatedAt', '')), p.get('id', '')), reverse=True)

    def get(self, kind, record_id):
        with self.db() as db:
            row = db.execute('SELECT payload FROM lc_records WHERE owner=? AND kind=? AND id=?', (self.owner, kind, record_id)).fetchone()
        return json.loads(row['payload']) if row else None

    def save(self, kind, payload, expected_revision=None, photo=None, photo_mime='image/jpeg'):
        if kind not in KINDS:
            raise ValueError('Unknown record kind')
        payload = dict(payload)
        record_id = payload.get('id') or str(uuid.uuid4())
        payload['id'] = record_id
        with self.db(write=True) as db:
            row = db.execute('SELECT * FROM lc_records WHERE owner=? AND kind=? AND id=?', (self.owner, kind, record_id)).fetchone()
            if row:
                if expected_revision != row['revision']:
                    raise Conflict('다른 창에서 기록이 바뀌었습니다. 최신 기록을 다시 열어 주세요.')
                previous = json.loads(row['payload'])
                payload['createdAt'] = previous.get('createdAt', now())
                payload['revision'] = row['revision'] + 1
                payload['updatedAt'] = now()
                changed = db.execute('UPDATE lc_records SET payload=?,revision=?,updated_at=? WHERE owner=? AND kind=? AND id=? AND revision=?', (encoded(payload), payload['revision'], now(), self.owner, kind, record_id, expected_revision))
                if changed.rowcount != 1:
                    raise Conflict('동시에 변경되었습니다. 최신 기록을 다시 열어 주세요.')
                db.execute('INSERT INTO lc_history(owner,kind,record_id,revision,payload,saved_at) VALUES (?,?,?,?,?,?)', (self.owner, kind, record_id, row['revision'], row['payload'], now()))
            else:
                if expected_revision is not None:
                    raise Conflict('수정할 원본을 찾지 못했습니다.')
                payload.setdefault('createdAt', now())
                payload['revision'] = 1
                db.execute('INSERT INTO lc_records(owner,kind,id,payload,revision,updated_at) VALUES (?,?,?,?,?,?)', (self.owner, kind, record_id, encoded(payload), 1, now()))
            if kind == 'environment' and payload.get('isDefault'):
                others = db.execute('SELECT * FROM lc_records WHERE owner=? AND kind=? AND id<>?', (self.owner, kind, record_id)).fetchall()
                for other in others:
                    value = json.loads(other['payload'])
                    if value.get('isDefault'):
                        value.update(isDefault=0, revision=other['revision'] + 1, updatedAt=now())
                        db.execute('INSERT INTO lc_history(owner,kind,record_id,revision,payload,saved_at) VALUES (?,?,?,?,?,?)', (self.owner, kind, other['id'], other['revision'], other['payload'], now()))
                        db.execute('UPDATE lc_records SET payload=?,revision=?,updated_at=? WHERE owner=? AND kind=? AND id=?', (encoded(value), value['revision'], now(), self.owner, kind, other['id']))
            if photo is not None:
                self._put_photo(db, record_id, photo, photo_mime, payload.get('photoKey'))
        return payload

    def _put_photo(self, db, note_id, data, mime, source_key=None):
        db.execute('INSERT INTO lc_media(owner,note_id,data,mime,source_key) VALUES (?,?,?,?,?) ON CONFLICT(owner,note_id) DO UPDATE SET data=excluded.data,mime=excluded.mime,source_key=excluded.source_key', (self.owner, note_id, data, mime, source_key))

    def photo(self, note_id):
        with self.db() as db:
            row = db.execute('SELECT data,mime FROM lc_media WHERE owner=? AND note_id=?', (self.owner, note_id)).fetchone()
        return (bytes(row['data']), row['mime']) if row else None

    def revisions(self, kind, record_id):
        with self.db() as db:
            rows = db.execute('SELECT * FROM lc_history WHERE owner=? AND kind=? AND record_id=? ORDER BY revision DESC', (self.owner, kind, record_id)).fetchall()
        return [{'revision': row['revision'], 'savedAt': row['saved_at'], 'note': json.loads(row['payload'])} for row in rows]

    def login_blocked(self):
        with self.db() as db:
            row = db.execute('SELECT blocked_until FROM lc_login_gate WHERE owner=?', (self.owner,)).fetchone()
        return max(0, int(float(row['blocked_until']) - time.time()))

    def login_result(self, success):
        with self.db(write=True) as db:
            if success:
                db.execute('UPDATE lc_login_gate SET failures=0,blocked_until=? WHERE owner=?', ('0', self.owner))
            else:
                db.execute('UPDATE lc_login_gate SET failures=failures+1 WHERE owner=?', (self.owner,))
                count = db.execute('SELECT failures FROM lc_login_gate WHERE owner=?', (self.owner,)).fetchone()['failures']
                if count >= 10:
                    db.execute('UPDATE lc_login_gate SET failures=0,blocked_until=? WHERE owner=?', (str(time.time() + 600), self.owner))

    def export(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM lc_records WHERE owner=?', (self.owner,)).fetchall()
            history = db.execute('SELECT * FROM lc_history WHERE owner=?', (self.owner,)).fetchall()
            photos = db.execute('SELECT * FROM lc_media WHERE owner=?', (self.owner,)).fetchall()
        records = {kind: [json.loads(row['payload']) for row in rows if row['kind'] == kind] for kind in KINDS}
        document = {'schemaVersion': 1, 'exportedAt': now(), 'records': records,
            'histories': [{'kind': row['kind'], 'recordId': row['record_id'], 'revision': row['revision'], 'payload': json.loads(row['payload']), 'savedAt': row['saved_at']} for row in history], 'photos': []}
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for photo in photos:
                suffix = 'png' if photo['mime'] == 'image/png' else 'jpg'
                path = 'photos/' + photo['note_id'] + '.' + suffix
                data = bytes(photo['data'])
                document['photos'].append({'noteId': photo['note_id'], 'path': path, 'contentType': photo['mime'], 'sourceKey': photo['source_key'], 'sha256': hashlib.sha256(data).hexdigest()})
                archive.writestr(path, data)
            archive.writestr('records.json', json.dumps(document, ensure_ascii=False, indent=2))
        return output.getvalue()

    def import_backup(self, content, dry_run=True):
        document, pictures = read_backup(content)
        counts = {kind: len(document['records'].get(kind, [])) for kind in KINDS}
        counts.update(histories=len(document.get('histories', [])), photos=len(pictures))
        with self.db(write=True) as db:
            # All imports are atomic. Existing different IDs are never overwritten.
            for kind in KINDS:
                for payload in document['records'].get(kind, []):
                    record_id = payload['id']
                    row = db.execute('SELECT payload FROM lc_records WHERE owner=? AND kind=? AND id=?', (self.owner, kind, record_id)).fetchone()
                    if row:
                        if json.loads(row['payload']) != payload:
                            raise Conflict('같은 ID의 다른 기록이 있습니다. 가져오기를 중단했습니다: ' + kind)
                        continue
                    db.execute('INSERT INTO lc_records(owner,kind,id,payload,revision,updated_at) VALUES (?,?,?,?,?,?)', (self.owner, kind, record_id, encoded(payload), payload.get('revision', 1), now()))
            for history in document.get('histories', []):
                condition = (self.owner, history['kind'], history['recordId'], history['revision'])
                row = db.execute('SELECT payload FROM lc_history WHERE owner=? AND kind=? AND record_id=? AND revision=?', condition).fetchone()
                if row and json.loads(row['payload']) != history['payload']:
                    raise Conflict('기존 수정 이력과 겹칩니다.')
                if not row:
                    db.execute('INSERT INTO lc_history(owner,kind,record_id,revision,payload,saved_at) VALUES (?,?,?,?,?,?)', (*condition, encoded(history['payload']), history['savedAt']))
            for photo, data in pictures:
                row = db.execute('SELECT data FROM lc_media WHERE owner=? AND note_id=?', (self.owner, photo['noteId'])).fetchone()
                if row and bytes(row['data']) != data:
                    raise Conflict('기존 사진과 다른 파일이 있습니다.')
                if not row:
                    self._put_photo(db, photo['noteId'], data, photo['contentType'], photo.get('sourceKey'))
            if dry_run:
                db.connection.rollback()
        return counts
