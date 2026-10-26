"""Single-owner Notion persistence using immutable, verified backup snapshots.

Each save adds a child page with a manifest and complete portable backup. SQL
is used only inside a temporary working directory; Notion remains authoritative.
No last-writer-wins: divergent snapshot chains stop reads and preserve both files.
"""
import hashlib
import io
import json
import re
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit
from .backup import KINDS, read_backup
from .store import Store, Conflict, encoded, now

PART_SIZE = 4 * 1024 * 1024
MAX_ARCHIVE = 80 * 1024 * 1024
VERSION = '2026-03-11'


class NotionError(ValueError):
    pass


def page_id(value):
    value = value.strip()
    if value.startswith('https://'):
        parsed = urlsplit(value)
        if parsed.hostname not in ('notion.so', 'www.notion.so', 'app.notion.com'):
            raise ValueError('Notion 페이지 주소 또는 페이지 ID를 입력해 주세요.')
        value = parsed.path.rstrip('/').split('/')[-1]
        match = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})$', value)
        value = match.group(1) if match else value
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError('Notion 페이지 ID를 확인해 주세요.') from None


def text_block(content, kind='paragraph'):
    return {'object': 'block', 'type': kind, kind: {'rich_text': [
        {'type': 'text', 'text': {'content': content[i:i + 1800]}}
        for i in range(0, len(content), 1800)]}}


def plain(block, kind):
    return ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                   for t in block.get(kind, {}).get('rich_text', []))


class NotionAPI:
    def __init__(self, token):
        import requests
        self.session = requests.Session()
        self.headers = {'Authorization': 'Bearer ' + token, 'Notion-Version': VERSION}
        self._network_lock = threading.Lock()
        self._last_request = 0.0

    def request(self, method, path, **kwargs):
        # Mutations are never blindly replayed after an uncertain response.
        with self._network_lock:
            for attempt in range(4):
                time.sleep(max(0, .36 - (time.monotonic() - self._last_request)))
                try:
                    response = self.session.request(method, 'https://api.notion.com/v1/' + path,
                        headers=self.headers, timeout=(10, 60), allow_redirects=False, **kwargs)
                except Exception:
                    raise NotionError('Notion 응답을 확인하지 못했습니다. 저장 중이었다면 새로고침으로 기록을 먼저 확인해 주세요.') from None
                finally:
                    self._last_request = time.monotonic()
                if response.status_code == 429 and attempt < 3:
                    try:
                        delay = min(10, max(1, float(response.headers.get('Retry-After', '1'))))
                    except ValueError:
                        delay = 1
                    time.sleep(delay)
                    continue
                if response.status_code in (401, 403, 404):
                    raise NotionError('Notion 연결 권한을 확인해 주세요. 전용 연결을 이 페이지에 추가하고 토큰을 Secrets에 등록해야 합니다.')
                if not 200 <= response.status_code < 300:
                    raise NotionError('Notion 요청을 완료하지 못했습니다. 잠시 후 기록을 확인해 주세요. (HTTP ' + str(response.status_code) + ')')
                try:
                    return response.json()
                except ValueError:
                    raise NotionError('Notion 응답 형식을 확인하지 못했습니다.') from None

    def children(self, block_id):
        rows, cursor, seen = [], None, set()
        while True:
            params = {'page_size': 100}
            if cursor:
                params['start_cursor'] = cursor
            result = self.request('GET', 'blocks/' + block_id + '/children', params=params)
            rows.extend(result['results'])
            if not result.get('has_more'):
                return rows
            cursor = result.get('next_cursor')
            if not cursor or cursor in seen:
                raise NotionError('Notion 기록 목록의 다음 페이지를 확인하지 못했습니다.')
            seen.add(cursor)

    def upload(self, filename, data):
        result = self.request('POST', 'file_uploads', json={
            'mode': 'single_part', 'filename': filename, 'content_type': 'application/zip'})
        upload_id = str(uuid.UUID(result['id']))
        result = self.request('POST', 'file_uploads/' + upload_id + '/send',
                              files={'file': (filename, data, 'application/zip')})
        if result.get('status') != 'uploaded':
            raise NotionError('Notion 파일 업로드가 완료되지 않았습니다. 기존 기록은 유지됩니다.')
        return upload_id

    def download(self, url, limit):
        # Signed media URLs are fetched without the Notion Authorization header.
        import requests
        for _ in range(4):
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            allowed = any(host == h or host.endswith('.' + h)
                          for h in ('amazonaws.com', 'notion.so', 'notion.com', 'notionusercontent.com'))
            if parsed.scheme != 'https' or not allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise NotionError('Notion 첨부 파일의 다운로드 주소를 확인해 주세요.')
            try:
                with requests.get(url, timeout=(10, 60), stream=True, allow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = response.headers.get('Location', '')
                        continue
                    if response.status_code != 200:
                        raise NotionError('Notion 파일을 읽지 못했습니다. 새로고침 후 다시 시도해 주세요.')
                    data = bytearray()
                    for chunk in response.iter_content(65536):
                        data.extend(chunk)
                        if len(data) > limit:
                            raise NotionError('Notion 첨부 파일이 예상 크기를 초과했습니다.')
                    return bytes(data)
            except NotionError:
                raise
            except Exception:
                raise NotionError('Notion 파일 다운로드가 중단되었습니다. 기존 기록을 유지합니다.') from None
        raise NotionError('Notion 파일의 리디렉션을 확인하지 못했습니다.')


class NotionStore:
    def __init__(self, token, root, owner='owner', api=None):
        self.owner, self.root = owner, page_id(root)
        self.api = api or NotionAPI(token)
        self.prefix = 'LC1-' + hashlib.sha256(owner.encode()).hexdigest()[:12] + '-'
        self._lock = threading.RLock()
        self._cache = {}
        self._head_cache = None
        self._read_at = 0.0
        self._content = None
        self._content_head = None
        self._known = set()
        self._failures = 0
        self._blocked_until = 0
        root_page = self.api.request('GET', 'pages/' + self.root)
        if root_page.get('archived') or root_page.get('in_trash'):
            raise NotionError('Notion 보관 페이지가 휴지통에 있습니다.')

    def _manifest(self, block):
        op = block['child_page']['title'][len(self.prefix):]
        if not re.fullmatch(r'[0-9a-f]{32}', op):
            raise NotionError('앱 보관 페이지의 이름이 바뀌었습니다. 원래 이름을 복구해 주세요.')
        if op not in self._cache:
            blocks = self.api.children(block['id'])
            manifests = [plain(b, 'code') for b in blocks if b.get('type') == 'code']
            try:
                if len(manifests) != 1:
                    raise ValueError()
                value = json.loads(manifests[0])
                if value['format'] != 'listening-notion-1' or value['op'] != op or value['owner'] != self.owner:
                    raise ValueError()
                if value['parent'] is not None and not re.fullmatch(r'[0-9a-f]{32}', value['parent']):
                    raise ValueError()
                if not isinstance(value['size'], int) or not 0 < value['size'] <= MAX_ARCHIVE:
                    raise ValueError()
                if not re.fullmatch(r'[0-9a-f]{64}', value['sha256']):
                    raise ValueError()
                if not 1 <= len(value['parts']) <= 20:
                    raise ValueError()
                names = [p['name'] for p in value['parts']]
                if len(set(names)) != len(names) or any(not re.fullmatch(r'part-[0-9]{3}\.zip', n) for n in names):
                    raise ValueError()
                if any(not re.fullmatch(r'[0-9a-f]{64}', p['sha256']) for p in value['parts']):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise NotionError('Notion 저장 정보가 손상되었거나 수정되었습니다. 원본 백업을 확인해 주세요.') from None
            self._cache[op] = (value, block['id'])
        return self._cache[op][0]

    def _head(self, force=False):
        if not force and self._read_at and time.monotonic() - self._read_at < 10:
            return self._head_cache
        blocks = self.api.children(self.root)
        commits = {}
        for block in blocks:
            if block.get('type') == 'child_page' and block['child_page']['title'].startswith(self.prefix):
                value = self._manifest(block)
                if value['op'] in commits:
                    raise Conflict('중복 저장 페이지가 발견되었습니다. 두 페이지를 보존한 상태로 복구가 필요합니다.')
                commits[value['op']] = value
        if self._known - commits.keys():
            raise NotionError('이전에 확인한 Notion 저장 페이지가 목록에 없습니다. 새로고침 후 다시 확인해 주세요. 기록을 빈 상태로 덮어쓰지 않습니다.')
        parents = {c['parent'] for c in commits.values() if c['parent'] is not None}
        if parents - commits.keys():
            raise NotionError('이전 저장 이력이 빠졌습니다. Notion 보관 페이지를 복구해 주세요.')
        leaves = commits.keys() - parents
        roots = [c for c in commits.values() if c['parent'] is None]
        if commits and (len(leaves) != 1 or len(roots) != 1):
            raise Conflict('동시에 저장된 두 갈래의 기록이 있습니다. 모든 백업을 보존하고 자동 덮어쓰기를 중단했습니다.')
        head = next(iter(leaves), None)
        visited, current = set(), head
        while current is not None:
            if current in visited:
                raise NotionError('Notion 저장 이력의 순서를 확인하지 못했습니다.')
            visited.add(current)
            current = commits[current]['parent']
        if visited != commits.keys():
            raise NotionError('연결되지 않은 Notion 저장 이력이 있습니다.')
        self._known.update(commits)
        self._head_cache, self._read_at = head, time.monotonic()
        return head

    def _download(self, head):
        if head is None:
            return None
        if head == self._content_head:
            return self._content
        manifest, ident = self._cache[head]
        # Re-fetch the blocks for fresh, expiring attachment URLs.
        blocks = self.api.children(ident)
        files = {}
        for b in blocks:
            if b.get('type') == 'file':
                file = b['file']
                name = file.get('name')
                if name in files:
                    raise NotionError('중복된 Notion 첨부 파일이 있습니다.')
                files[name] = file
        chunks = []
        for part in manifest['parts']:
            file = files.get(part['name'], {})
            if file.get('type') != 'file' or not file.get('file', {}).get('url'):
                raise NotionError('저장된 백업의 첨부 파일이 빠져 있습니다.')
            data = self.api.download(file['file']['url'], PART_SIZE + 4096)
            if hashlib.sha256(data).hexdigest() != part['sha256']:
                raise NotionError('Notion 백업 파일의 검증값이 다릅니다. 원문을 덮어쓰지 않습니다.')
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    info = archive.infolist()
                    if len(info) != 1 or info[0].filename != 'snapshot.part' or info[0].file_size > PART_SIZE:
                        raise ValueError()
                    chunks.append(archive.read('snapshot.part'))
            except (ValueError, zipfile.BadZipFile, RuntimeError):
                raise NotionError('Notion 백업 조각이 손상되었습니다.') from None
        content = b''.join(chunks)
        if len(content) != manifest['size'] or hashlib.sha256(content).hexdigest() != manifest['sha256']:
            raise NotionError('Notion 전체 백업의 크기 또는 검증값이 다릅니다.')
        read_backup(content)
        self._content, self._content_head = content, head
        return content

    def _publish(self, content, parent, label):
        if len(content) > MAX_ARCHIVE:
            raise ValueError('Notion 저장 백업이 80MB를 초과했습니다. 백업을 내려받고 저장소 확장을 진행해 주세요.')
        op, children, parts = uuid.uuid4().hex, [], []
        for index, offset in enumerate(range(0, len(content), PART_SIZE)):
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w', zipfile.ZIP_STORED) as archive:
                archive.writestr('snapshot.part', content[offset:offset + PART_SIZE])
            packed = data.getvalue()
            name = f'part-{index:03d}.zip'
            upload_id = self.api.upload(name, packed)
            parts.append({'name': name, 'sha256': hashlib.sha256(packed).hexdigest()})
            children.append({'object': 'block', 'type': 'file', 'file': {
                'type': 'file_upload', 'file_upload': {'id': upload_id}, 'name': name}})
        manifest = {'format': 'listening-notion-1', 'op': op, 'owner': self.owner,
            'parent': parent, 'createdAt': now(), 'size': len(content),
            'sha256': hashlib.sha256(content).hexdigest(), 'parts': parts}
        document, _ = read_backup(content)
        counts = ' · '.join(kind + ': ' + str(len(document['records'].get(kind, []))) for kind in KINDS)
        code = text_block(encoded(manifest), 'code')
        code['code']['language'] = 'json'
        children = [text_block(label + ' · ' + now(), 'heading_2'),
            text_block('감상 원문·사진·수정 이력을 보존한 앱 저장본입니다. 기록 수정과 ZIP 내보내기는 Streamlit 앱에서 해 주세요. 이 페이지와 첨부 파일은 삭제하거나 수정하지 마세요.'),
            text_block(counts), code] + children
        if self._head(force=True) != parent:
            raise Conflict('다른 창에서 먼저 저장했습니다. 최신 기록을 다시 열어 주세요.')
        try:
            self.api.request('POST', 'pages', json={'parent': {'page_id': self.root},
                'properties': {'title': {'type': 'title', 'title': [{'type': 'text', 'text': {'content': self.prefix + op}}]}},
                'children': children})
        except NotionError:
            # A timeout can mean the page was created. Resolve by the operation ID.
            self._read_at = 0
            try:
                self._head(force=True)
            except (NotionError, Conflict):
                raise NotionError('Notion 저장 결과를 확정하지 못했습니다. 재입력 전에 새로고침으로 기록을 확인해 주세요.') from None
            if op not in self._cache:
                raise NotionError('Notion 저장 결과를 확인하지 못했습니다. 새로고침으로 기록을 먼저 확인해 주세요.') from None
        # Verify remote bytes before reporting success. Never keep a local-only save.
        self._read_at = 0
        head = self._head(force=True)
        if head != op:
            raise NotionError('Notion 저장본이 아직 목록에 반영되지 않았습니다. 새로고침으로 기록을 확인해 주세요.')
        self._content_head = None
        self._download(op)

    def _run(self, method, *args, mutate=False, **kwargs):
        with self._lock:
            head = self._head(force=mutate)
            content = self._download(head)
            with tempfile.TemporaryDirectory(prefix='listening-notion-') as folder:
                local = Store('sqlite:///' + str(Path(folder) / 'working.sqlite3'), self.owner)
                if content:
                    local.import_backup(content, dry_run=False)
                result = getattr(local, method)(*args, **kwargs)
                if mutate:
                    output = local.export()
                    # Ignore export timestamp and ZIP timestamps for idempotent imports.
                    before = read_backup(content)[0] if content else None
                    after = read_backup(output)[0]
                    if before:
                        before.pop('exportedAt', None)
                    after.pop('exportedAt', None)
                    if before != after:
                        self._publish(output, head, '기록 가져오기' if method == 'import_backup' else '기록 저장')
                return result

    def list(self, kind):
        return self._run('list', kind)

    def get(self, kind, record_id):
        return self._run('get', kind, record_id)

    def save(self, kind, payload, expected_revision=None, photo=None, photo_mime='image/jpeg'):
        return self._run('save', kind, payload, expected_revision, photo, photo_mime, mutate=True)

    def photo(self, note_id):
        return self._run('photo', note_id)

    def revisions(self, kind, record_id):
        return self._run('revisions', kind, record_id)

    def export(self):
        return self._run('export')

    def import_backup(self, content, dry_run=True):
        return self._run('import_backup', content, dry_run=dry_run, mutate=not dry_run)

    def login_blocked(self):
        with self._lock:
            return max(0, int(self._blocked_until - time.time()))

    def login_result(self, success):
        # Shared across app sessions in one process. Resets on server restart.
        with self._lock:
            if success:
                self._failures, self._blocked_until = 0, 0
            else:
                self._failures += 1
                if self._failures >= 10:
                    self._failures, self._blocked_until = 0, time.time() + 600
