"""Listening Companion — independent, single-owner Streamlit app."""
import hashlib
import io
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, quote
import streamlit as st
from PIL import Image
from listening.auth import setting, valid_password, valid_secret_password
from listening.store import Store, Conflict, now
from listening.notion_store import NotionStore, NotionError
from listening.experience import score_rows, IMPRESSIONS, DECISIONS, editorial_selection, reaction_active, album_identity
from listening.music import (MusicError, apple_albums, brainz_albums, manual_album,
                              plan_searches, photo_clues, normalize)

st.set_page_config(page_title='Listening Companion', page_icon='🎧', layout='wide')
st.markdown('''<style>
.block-container{max-width:1180px;padding-top:2rem}
h1,h2,h3{letter-spacing:-.035em;font-weight:500}h1{font-size:42px;line-height:1.4}
[data-testid="stForm"],[data-testid="stVerticalBlockBorderWrapper"]{border-color:#dce1e7;border-radius:6px;background:#fff}
.lc-brand{font:italic 34px Georgia,serif;display:flex;align-items:center;gap:16px;padding:12px 0 24px;border-bottom:1px solid #dce1e7}
.lc-brand span{font:11px Arial,sans-serif;letter-spacing:2px}.lc-brand small{font:12px Arial,sans-serif;margin-left:auto;color:#687586}
.lc-intro{padding:38px 0 24px}.lc-intro span{font-size:10px;letter-spacing:2px;color:#697f96;font-weight:bold}.lc-intro p{font-size:15px;color:#667482}
.lc-principles{background:#eaf0f5;padding:28px;border-radius:6px}.lc-principles h2{font-size:26px}.lc-principles div{border-top:1px solid #d5dfe8;padding-top:18px;margin-top:22px}.lc-principles p{font-size:13px;color:#64798c;line-height:1.8}
.st-key-page [role=radiogroup]{gap:24px;border-bottom:1px solid #dce1e7;padding:10px 0 14px}.st-key-page label{padding:8px 0}.st-key-page label>div:first-child{display:none}
.lc-footer{margin-top:48px;border-top:1px solid #dce1e7;padding:24px 0;color:#758191;font-size:11px;letter-spacing:1px}
</style>''', unsafe_allow_html=True)

ROOT = Path(__file__).parent
GOALS = ['편안하게 쉬기', '일·독서에 집중하기', '활기를 느끼기', '음악에 깊이 몰입하기', '위로·여운 느끼기', '새로운 발견', '연주 비교하기', '소리의 아름다움 듣기']
GOAL_IDS = ['relax', 'focus', 'energy', 'immerse', 'reflect', 'discover', 'compare', 'sound']
KINDS = ['감상 대화', '음악성', '녹음 품질', '내 시스템 재생', '취향', '감상 방향', '사진 감상']


@st.cache_resource
def database(url, backend='postgres', notion_token='', notion_root=''):
    return NotionStore(notion_token, notion_root) if backend == 'notion' else Store(url)


@st.cache_data(ttl=21600, show_spinner=False)
def catalogue(query, provider):
    return apple_albums(query) if provider == 'Apple Music' else brainz_albums(query)


def parse(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


def message(text):
    st.session_state['notice'] = text
    st.rerun()


def photo_bytes(uploaded):
    if not uploaded:
        return None
    data = uploaded.getvalue()
    if len(data) > 4_000_000:
        raise ValueError('사진은 4MB 이하로 선택해 주세요.')
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in ('JPEG', 'PNG') or image.width * image.height > 25_000_000:
            raise ValueError('25MP 이하의 JPG 또는 PNG 사진을 사용해 주세요.')
        image.verify()
        mime = 'image/jpeg' if image.format == 'JPEG' else 'image/png'
    return data, mime


def show_album(album):
    st.subheader(album.get('title') or album.get('album') or '음반 정보')
    st.write(album.get('artists') or '')
    if album.get('releaseDate'):
        st.caption('발매 정보 · ' + album['releaseDate'])
    if album.get('userProvided'):
        st.caption('직접 입력한 정보 · 카탈로그 대조와 재생 가능 여부는 확인하지 않았습니다.')
    if album.get('url') and urlsplit(album['url']).scheme == 'https':
        st.link_button(album.get('provider', '음반 페이지') + '에서 확인 ↗', album['url'])


def note_album(note):
    details = parse(note.get('albumDetails'), {})
    return dict(details, title=note.get('albumTitle'), artists=note.get('albumArtists')) if note.get('albumTitle') else None


def choose_album(album):
    st.session_state['chosen_album'] = album
    st.session_state['pending_page'] = '감상 기록'
    st.session_state['editing_note'] = None
    st.session_state['editor_version'] = st.session_state.get('editor_version', 0) + 1
    st.rerun()


def render_candidate(album, key, choose=True):
    with st.container(border=True):
        a, b = st.columns([1, 3])
        if album.get('artwork'):
            a.image(album['artwork'], use_container_width=True)
        with b:
            show_album(album)
            if album.get('reason'):
                st.write(album['reason'])
            if choose and st.button('이 음반으로 감상 남기기', key='pick_' + key):
                choose_album(album)


def save_reaction(store, album, action):
    matches = [r for r in store.list('reaction') if album_identity(r.get('albumId')) == album_identity(album['id'])]
    current = matches[0] if matches else None
    ident = current['id'] if current else hashlib.sha256(album['id'].encode()).hexdigest()
    value = 'clear' if current and current.get('action') == action else action
    store.save('reaction', dict(current or {}, id=ident, albumId=album['id'], albumTitle=album['title'], albumArtists=album['artists'], action=value), current.get('revision') if current else None)
    return value


def find_recommendations(store, notes, api_key, model, request, more=False):
    if not api_key:
        raise MusicError('AI 키가 연결되지 않았습니다. 아래의 음반 직접 검색과 리뷰 음반 비교를 사용할 수 있습니다.')
    text = '\n'.join(filter(None, [request['direction'].strip(), '오늘 듣고 싶은 장르: ' + request['genre'].strip() if request['genre'].strip() else '']))
    if text and st.session_state.get('last_saved_direction') != text:
        store.save('note', {'kind': '감상 방향', 'content': text})
        st.session_state['last_saved_direction'] = text
    reactions = [r for r in store.list('reaction') if reaction_active(r)]
    history = notes if request['use_history'] else []
    if request['use_history']:
        history = history + [{'id': 'reaction:' + r['id'], 'kind': '관심 반응' if r['action'] == 'interested' else '들은 사실 또는 이번 탐색 제외', 'content': r.get('albumTitle', '') + ' · ' + r.get('albumArtists', '') + ' · ' + r['action']} for r in reactions]
    previous = st.session_state.get('recommendations', {})
    seen = previous.get('seen', []) if more else []
    round_index = previous.get('round', 0) + 1 if more else 0
    plan = plan_searches(api_key, model, request['direction'], request['genre'], request['goals'], history,
                         round_index=round_index, exclude=seen)
    albums, errors = [], []
    heard = {album_identity(n.get('albumId')) for n in notes if n.get('albumId')}
    skipped = {album_identity(r.get('albumId')) for r in reactions if r.get('action') in ('skip', 'heard', 'known')}
    for search in plan['searches']:
        try:
            for album in catalogue(search['term'], 'Apple Music'):
                artist = normalize(search['artist'])
                if artist not in normalize(album['artists']):
                    continue
                ident = album_identity(album['id'])
                if ident in seen or (not request['include_heard'] and ident in heard | skipped):
                    continue
                if any(a['id'] == album['id'] for a in albums):
                    continue
                albums.append(dict(album, reason=search['reason'], basisIds=search['basisIds']))
        except MusicError as error:
            errors.append(str(error))
    st.session_state['recommendations'] = {'plan': plan, 'albums': albums[:12], 'errors': errors,
        'request': request, 'round': round_index, 'seen': (seen + [album_identity(a['id']) for a in albums[:12]])[-200:]}


def discover(store, notes, api_key, model):
    st.caption('DISCOVER & LISTEN')
    st.subheader('오늘은 어떤 음악을 만나볼까요?')
    st.write('음반을 몰라도 괜찮습니다. 오늘의 기분에서 시작해 보세요.')
    with st.form('discover'):
        direction = st.text_area('오늘 듣고 싶은 음악', max_chars=3000, placeholder='몽환적이지만 너무 과격하지 않은 전자음악을 듣고 싶어요.')
        genre = st.text_input('듣고 싶은 장르 · 선택 사항', max_chars=80)
        goals = st.multiselect('오늘의 청취 경험', GOALS)
        use_history = st.checkbox('내 감상·취향 원문을 AI 검색 조건에 반영', value=False)
        include_heard = st.checkbox('이미 들은 음반도 포함', value=False)
        submitted = st.form_submit_button('나를 위한 음반 발견하기', type='primary')
    request = {'direction': direction, 'genre': genre, 'goals': goals, 'use_history': use_history, 'include_heard': include_heard}
    st.caption('발견하기를 누르면 오늘의 요청을 감상 방향으로 보존합니다. 들은 사실과 좋아하는 취향은 구분합니다.')
    if submitted:
        try:
            with st.spinner('오늘의 요청을 해석하고 실제 카탈로그에서 찾고 있습니다…'):
                find_recommendations(store, notes, api_key, model, request)
        except MusicError as error:
            st.error(str(error))
    result = st.session_state.get('recommendations')
    if result:
        st.info(result['plan']['summary'])
        for error in result['errors']:
            st.warning(error)
        if not result['albums']:
            st.info('이번 검색에서 맞는 음반을 확인하지 못했습니다. 음반이 존재하지 않는다는 뜻은 아닙니다.')
        for album in result['albums']:
            render_candidate(album, 'recommend_' + album['id'])
            if album.get('basisIds'):
                with st.expander('추천에 참고한 내 원문'):
                    for note in notes:
                        if note['id'] in album['basisIds']:
                            st.write(note.get('content', ''))
                            if st.button('원문 보기·보완', key='basis_' + album['id'] + note['id']):
                                st.session_state['editing_note'] = note['id']
                                st.session_state['chosen_album'] = None
                                st.session_state['editor_version'] = st.session_state.get('editor_version', 0) + 1
                                st.session_state['pending_page'] = '감상 기록'
                                st.rerun()
            columns = st.columns(3)
            for column, action, label in zip(columns, ['interested', 'known', 'skip'], ['관심 있어요', '이미 들어봤어요', '이번에는 건너뛰기']):
                if column.button(label, key=action + album['id']):
                    value = save_reaction(store, album, action)
                    st.success('반응을 취소했습니다.' if value == 'clear' else '반응을 저장했습니다. 다음 탐색부터 반영합니다.')
        changed = request != result.get('request')
        if changed:
            st.caption('선택이 바뀌었습니다. 아래는 이전 결과입니다. 발견하기를 눌러 갱신해 주세요.')
        if st.button('다른 후보 찾아보기', disabled=changed):
            try:
                with st.spinner('이미 본 후보를 제외하고 다른 음반을 찾고 있습니다…'):
                    find_recommendations(store, notes, api_key, model, request, more=True)
                st.rerun()
            except MusicError as error:
                st.error(str(error))
    st.divider()
    st.subheader('음반이나 아티스트를 직접 찾기')
    search_panel('discovery', allow_choose=True)
    with st.expander('리뷰 자료가 연결된 음반 비교'):
        editorial_page(store)
    with st.expander('저장한 음반 반응'):
        for reaction in store.list('reaction'):
            if not reaction_active(reaction):
                continue
            st.write(reaction.get('albumTitle', '') + ' · ' + reaction.get('action', ''))
            if st.button('이 반응 취소', key='clear_' + reaction['id']):
                store.save('reaction', dict(reaction, action='clear'), reaction.get('revision'))
                st.rerun()
        st.caption('이번에는 건너뛰기 반응은 7일 동안 적용합니다. 취소한 반응은 추천에 사용하지 않습니다.')
        st.caption('미평가는 부정 평가가 아닙니다. 반응은 이 앱 소유자의 탐색에만 사용합니다.')


def editorial_page(store):
    curated = json.loads((ROOT / 'data/curated-albums.json').read_text())
    st.caption('CURATED LISTENING · 첫 큐레이션')
    st.subheader('무엇에 귀 기울이고 싶으세요?')
    columns = st.columns(3)
    for column, label, term in zip(columns, ['골드베르크 변주곡', '베토벤 4번', '말러 2번'], ['골드베르크', '베토벤 4번', '말러 2번']):
        if column.button(label + ' 비교하기'):
            st.session_state['curated_query'] = term
            st.session_state['curated_goals'] = []
    query = st.text_input('작품·연주자 검색', key='curated_query')
    selected = st.multiselect('감상 방향으로 좁히기', GOALS, key='curated_goals')
    result = editorial_selection(curated, query, [GOAL_IDS[GOALS.index(g)] for g in selected])
    st.caption('여러 연주자나 작품은 쉼표로 나눕니다. 한 음반의 조건은 띄어쓰기로 묶습니다.')
    st.write('검색 결과 ' + str(len(result['items'])) + '개')
    if result['unmatchedQueries']:
        st.info('현재 확인한 음반 목록에서 찾지 못한 검색어: ' + ' · '.join(result['unmatchedQueries']))
    if result['fallback']:
        st.info('선택한 감상 방향과 맞는다는 근거가 부족하여 검색 조건에 맞는 음반을 대신 보여드립니다.')
    columns = st.columns(2)
    for i, entry in enumerate(result['items']):
        item = entry['recording']
        with columns[i % 2], st.container(border=True):
            st.image(item['artwork']['url'], use_container_width=True)
            st.subheader(item['work'])
            st.write(item['artists'])
            st.caption(item['release'])
            st.write(item['reason'])
            st.link_button('Apple Music에서 듣기 ↗', item['catalogue']['url'])
            if st.button('감상 포인트 보기', key='detail_' + item['id']):
                st.session_state['curated_detail'] = item['id']
    item = next((r for r in curated if r['id'] == st.session_state.get('curated_detail')), None)
    if item:
        with st.container(border=True):
            st.subheader(item['work'])
            st.write(item['artists'])
            st.caption(item['release'] + ' · ' + item['recorded'])
            st.markdown('**이 음반을 고른 이유**')
            st.write(item['reason'])
            album = {'id': item['id'], 'title': item['album'], 'artists': item['artists'], 'url': item['catalogue']['url'], 'provider': item['catalogue']['provider'], 'releaseDate': item['release'], 'userProvided': False}
            for point in item['points']:
                st.markdown('**' + point['where'] + '**')
                st.caption(point['type'])
                st.write(point['claim'])
                st.write(point['question'])
                st.link_button('출처 · ' + point['where'] + ' ↗', item['sources'][point['sourceIndex']]['url'])
                if st.button('이 포인트의 감상 남기기', key='point_' + item['id'] + point['id']):
                    choose_album(dict(album, pointId=point['id']))
            st.markdown('**녹음에 관한 근거**')
            st.write(item['sound'])
            for source in item['sources']:
                st.link_button(source['title'], source['url'])
                st.caption(source['scope'])
            st.caption('자료 확인: ' + item['catalogue']['checkedAt'] + ' · 계정별 재생 가능 여부와 스트리밍 마스터는 별도 확인')
            if st.button('음반 감상 남기기', key='curated_' + item['id']):
                choose_album(album)
    with st.expander('오늘의 기분이나 상황도 기록하기'):
        text = st.text_area('자유롭게 적어주세요', max_chars=1500)
        if st.button('감상 방향 저장', disabled=not text.strip()):
            store.save('note', {'kind': '감상 방향', 'content': text})
            message('감상 방향을 원문과 날짜로 보존했습니다.')


def search_panel(prefix, allow_choose=False, initial_query=''):
    with st.form(prefix + '_search'):
        query = st.text_input('음반명 · 작품 · 아티스트', value=initial_query[:160], key=prefix + '_query', max_chars=160)
        provider = st.radio('검색할 카탈로그', ['Apple Music', 'MusicBrainz'], horizontal=True, key=prefix + '_provider')
        find = st.form_submit_button('음반 검색')
    if find and query.strip():
        try:
            with st.spinner('카탈로그에서 확인하고 있습니다…'):
                st.session_state[prefix + '_results'] = catalogue(query.strip(), provider)
        except MusicError as error:
            st.error(str(error))
    for album in st.session_state.get(prefix + '_results', []):
        if allow_choose:
            render_candidate(album, prefix + album['id'])
        else:
            with st.container(border=True):
                show_album(album)
                if st.button('이 음반 정보 선택', key=prefix + album['id']):
                    st.session_state['chosen_album'] = album
                    st.rerun()


def journal(store, notes, environments, api_key, model, show_records=True):
    st.caption('LISTENING JOURNAL')
    st.subheader('감상 쓰기·보완')
    selected_id = st.session_state.get('editing_note')
    current = store.get('note', selected_id) if selected_id else None
    editor = str(st.session_state.get('editor_version', 0)) + (selected_id or 'new')
    if current:
        st.caption('저장한 감상을 보완합니다. 원문과 이전 작성 내용은 이력에 남습니다.')
        old_photo = store.photo(current['id'])
        if old_photo:
            st.image(old_photo[0], width=280)
    if st.button('새 감상 쓰기'):
        st.session_state['editing_note'] = None
        st.session_state['chosen_album'] = None
        st.session_state['editor_version'] = st.session_state.get('editor_version', 0) + 1
        st.rerun()
    chosen = st.session_state.get('chosen_album') or (dict(note_album(current), id=current.get('albumId')) if current and note_album(current) else None)
    if chosen:
        with st.container(border=True):
            show_album(chosen)
    with st.expander('음반 찾기 · 선택 사항', expanded=False):
        search_panel('journal_' + editor, initial_query=(current or {}).get('albumHint') or '')
        if st.session_state.get('journal_' + editor + '_clue'):
            st.info(st.session_state['journal_' + editor + '_clue'])
        if api_key:
            old_photo = store.photo(current['id']) if current else None
            if old_photo and st.button('첨부한 사진으로 음반 정보 확인', key='read_saved_' + editor):
                try:
                    with st.spinner('저장한 사진에서 음반 후보를 찾고 있습니다…'):
                        clue = photo_clues(api_key, model, old_photo)
                        st.session_state['journal_' + editor + '_results'] = catalogue(clue['query'], 'Apple Music')
                        st.session_state['journal_' + editor + '_clue'] = clue['clues']
                    st.rerun()
                except MusicError as error:
                    st.error(str(error))
            image_clue = st.file_uploader('재킷 사진에서 검색어 찾기', type=['jpg', 'jpeg', 'png'], key='ocr_' + editor)
            if image_clue and st.button('사진을 AI로 읽기', key='read_' + editor):
                try:
                    with st.spinner('재킷의 글자를 확인하고 있습니다…'):
                        clue = photo_clues(api_key, model, photo_bytes(image_clue))
                        st.session_state['journal_' + editor + '_results'] = catalogue(clue['query'], 'Apple Music')
                        st.session_state['journal_' + editor + '_clue'] = clue['clues']
                    st.rerun()
                except (MusicError, ValueError) as error:
                    st.error(str(error))
            st.caption('이 기능은 선택한 재킷 사진만 OpenAI로 보냅니다. 따로 작성한 감상은 보내지 않습니다.')
    original = current or {}
    env_map = {e['id']: e for e in environments}
    env_options = [''] + list(env_map)
    old_env = original.get('environmentId') or ''
    if old_env and old_env not in env_options:
        env_options.append(old_env)
    default_id = old_env if current else next((e['id'] for e in environments if e.get('isDefault')), '')
    with st.form('journal_form_' + editor):
        hint = st.text_area('듣던 음반 · 작품 · 연주자', value=original.get('albumHint') or '', max_chars=1000)
        content = st.text_area('감상', value=original.get('content') or '', height=180, max_chars=6000)
        with st.expander('음악적 느낌 · 오디오적 리스닝 포인트', expanded=bool(current)):
            musical = st.text_area('음악적 느낌', value=original.get('musicalNotes') or '', max_chars=6000)
            sonic = st.text_area('오디오적 리스닝 포인트', value=original.get('sonicNotes') or '', max_chars=6000)
        environment_id = st.selectbox('그때의 청취 환경', env_options, index=env_options.index(default_id) if default_id in env_options else 0,
            format_func=lambda v: '환경 미지정' if not v else env_map.get(v, {}).get('name', '기록 당시 환경'))
        refresh = st.checkbox('현재 청취 환경 설정으로 스냅샷 갱신', value=False) if current else False
        context = st.text_area('공간·기기 · 그날의 추가 메모', value=original.get('listeningContext') or '', max_chars=6000)
        kind = st.selectbox('기록 분류', KINDS, index=KINDS.index(original.get('kind')) if original.get('kind') in KINDS else 0)
        uploaded = st.file_uploader('감상에 사진 첨부 · 선택 사항', type=['jpg', 'jpeg', 'png'])
        with st.expander('검색이 안 되나요? Apple Music · TIDAL 주소로 직접 연결'):
            manual_url = st.text_input('앨범 공유 주소', max_chars=2000, placeholder='https://music.apple.com/kr/album/… 또는 https://tidal.com/browse/album/…')
            manual_title = st.text_input('직접 연결할 음반명', max_chars=500)
            manual_artists = st.text_input('직접 연결할 아티스트 · 연주자', max_chars=500)
            st.caption('입력한 정보는 카탈로그에서 검증된 정보와 구분하여 보관합니다.')
        saved = st.form_submit_button('보완 내용 저장' if current else '감상 저장하기', type='primary')
    if saved:
        try:
            if not any(v.strip() for v in (content, musical, sonic)):
                raise ValueError('감상이나 리스닝 포인트를 한 줄 남겨 주세요.')
            album = manual_album(manual_url, manual_title, manual_artists) if manual_url.strip() else chosen
            picture = photo_bytes(uploaded)
            snapshot = original.get('environmentSnapshot')
            if not current or refresh or environment_id != old_env:
                env = env_map.get(environment_id)
                snapshot = json.dumps({k: env.get(k) for k in ('id', 'name', 'details', 'revision')}, ensure_ascii=False) if env else None
            payload = dict(original, kind=kind, content=content, musicalNotes=musical, sonicNotes=sonic, listeningContext=context,
                           albumHint=hint, environmentId=environment_id or None, environmentSnapshot=snapshot)
            if album:
                payload.update(albumId=album['id'], albumTitle=album['title'], albumArtists=album['artists'],
                    albumDetails=json.dumps({k: album.get(k) for k in ('url', 'provider', 'releaseDate', 'catalogueNumbers', 'country', 'userProvided')}, ensure_ascii=False))
                if album.get('pointId'):
                    payload['pointId'] = album['pointId']
            if picture:
                payload['photoKey'] = 'database-photo'
            store.save('note', payload, original.get('revision') if current else None,
                       photo=picture[0] if picture else None, photo_mime=picture[1] if picture else 'image/jpeg')
            st.session_state['editing_note'] = None
            st.session_state['chosen_album'] = None
            st.session_state['editor_version'] = st.session_state.get('editor_version', 0) + 1
            message('감상을 저장했습니다. 원문과 수정 이력을 함께 보관합니다.')
        except (ValueError, OSError) as error:
            st.error(str(error))
    if current:
        with st.expander('이전 작성 내용'):
            for history in store.revisions('note', current['id']):
                st.caption(f"{history['revision']}번째 작성 · {history['savedAt']}")
                for field in ('content', 'musicalNotes', 'sonicNotes', 'listeningContext'):
                    if history['note'].get(field):
                        st.write(history['note'][field])
    if show_records:
        recent_notes(store, notes)


def recent_notes(store, notes):
    st.divider()
    st.subheader('나의 최근 기록')
    st.caption(f'기록 {len(notes)}개 · 나에게만 표시')
    query = st.text_input('감상 · 음반 · 연주자에서 찾기')
    if not notes:
        st.info('아직 표시할 기록이 없습니다. 기존 기록이 있었다면 백업·가져오기의 저장 연결 점검에서 연결 상태를 확인할 수 있습니다.')
    for note in notes:
        if query and query.casefold() not in ' '.join(str(note.get(k) or '') for k in ('content', 'albumTitle', 'albumArtists', 'albumHint', 'musicalNotes', 'sonicNotes')).casefold():
            continue
        title = note.get('albumTitle') or note.get('albumHint') or note.get('kind', '감상')
        with st.container(border=True):
            st.caption((note.get('kind') or '감상') + ' · ' + note.get('createdAt', '')[:10])
            album = note_album(note)
            if album:
                show_album(album)
            photo = store.photo(note['id'])
            if photo:
                st.image(photo[0], width=200)
            elif note.get('photoKey'):
                st.warning('이 감상의 사진은 아직 이전되지 않았습니다.')
            st.write(note.get('content', ''))
            for field, label in [('musicalNotes', '음악적 느낌'), ('sonicNotes', '오디오적 리스닝 포인트'), ('listeningContext', '그날의 상황')]:
                if note.get(field):
                    st.markdown('**' + label + '**')
                    st.write(note[field])
            snapshot = parse(note.get('environmentSnapshot'))
            if snapshot:
                st.caption('기록 당시 청취 환경 · ' + snapshot.get('name', ''))
                st.write(snapshot.get('details', ''))
            if st.button('감상 보기·보완', key='edit_' + note['id']):
                st.session_state['pending_page'] = '감상 기록'
                st.session_state['editing_note'] = note['id']
                st.session_state['chosen_album'] = None
                st.session_state['editor_version'] = st.session_state.get('editor_version', 0) + 1
                st.rerun()


def environment_page(store, environments):
    st.caption('LISTENING ENVIRONMENTS')
    st.subheader('자주 듣는 공간과 오디오')
    choices = [''] + [e['id'] for e in environments]
    by_id = {e['id']: e for e in environments}
    selected = st.selectbox('등록된 환경', choices, format_func=lambda v: by_id[v]['name'] if v else '새 환경 등록')
    current = by_id.get(selected, {})
    with st.form('environment_' + selected):
        name = st.text_input('환경 이름', value=current.get('name', ''), max_chars=200)
        details = st.text_area('공간 · 스피커 배치 · 기기 · 케이블 · 설정', value=current.get('details', ''), height=320, max_chars=12000)
        default = st.checkbox('새 감상의 기본 환경', value=bool(current.get('isDefault')))
        save = st.form_submit_button('청취 환경 저장', type='primary')
    if save:
        if not name.strip():
            st.error('환경 이름을 입력해 주세요.')
        else:
            try:
                store.save('environment', dict(current, name=name, details=details, isDefault=int(default), archived=0), current.get('revision'))
                message('청취 환경을 저장했습니다. 과거 감상에 보관된 환경은 유지됩니다.')
            except Conflict as error:
                st.error(str(error))
    st.caption('환경을 수정해도 이전 감상과 청음 실험의 당시 설정은 바뀌지 않습니다.')
    if current and st.button('선택 목록에서 빼기'):
        store.save('environment', dict(current, archived=1, isDefault=0), current.get('revision'))
        message('선택 목록에서 뺐습니다. 예전 감상과 실험에 남긴 환경은 보존됩니다.')
    for environment in environments:
        with st.container(border=True):
            st.subheader(environment['name'] + (' · 기본 환경' if environment.get('isDefault') else ''))
            st.write(environment.get('details') or '기기·공간 메모를 추가할 수 있습니다.')


def experiment_page(store, environments):
    st.caption('LISTENING EXPERIMENTS')
    st.subheader('나의 청음 실험')
    experiments = store.list('experiment')
    templates = json.loads((ROOT / 'data/experiment-templates.json').read_text())
    with st.expander('새 A/B 청음 실험 만들기'):
        template = st.radio('시작 방법', ['직접 만든 실험', '추천 · 후면 커튼 개폐 비교'], horizontal=True)
        plan = templates['curtain'] if template.startswith('추천') else {}
        with st.form('new_experiment_' + str(bool(plan))):
            title = st.text_input('실험 제목', value=plan.get('title', ''), max_chars=200)
            category = st.selectbox('실험 분류', templates['categories'], index=templates['categories'].index(plan.get('category', '기타')))
            variable = st.text_input('이번에 바꾸는 한 가지', value=plan.get('variable', ''), max_chars=500)
            left, right = st.columns(2)
            baseline = left.text_area('A · 기준 조건', value=plan.get('baseline', ''), max_chars=3000)
            variant = right.text_area('B · 비교 조건', value=plan.get('variant', ''), max_chars=3000)
            fixed = st.text_area('그대로 유지할 조건', value=plan.get('fixedConditions', ''), max_chars=6000)
            tracks = st.text_area('기준곡 · 구간', value=plan.get('referenceTracks', ''), max_chars=6000)
            protocol = st.text_area('실행 순서', value=plan.get('protocol', 'A → B → A 순서로 같은 곡·같은 음량에서 비교합니다.'), max_chars=6000)
            criteria = st.text_input('평가 기준 · 쉼표로 구분', value=', '.join(plan.get('criteria', ['중앙 음상', '공간감', '자연스러움'])), max_chars=1000)
            effect = st.text_area('예상하는 변화 · 가설', value=plan.get('expectedEffect', ''), max_chars=3000)
            rollback = st.text_area('원래 설정으로 돌아갈 기준', value=plan.get('rollbackRule', ''), max_chars=3000)
            env_id = st.selectbox('실험 당시 청취 환경', [''] + [e['id'] for e in environments], format_func=lambda v: next((e['name'] for e in environments if e['id'] == v), '미지정'))
            create = st.form_submit_button('실험 계획 저장')
        if create:
            criteria_values = [v.strip() for v in criteria.split(',') if v.strip()]
            if not all(v.strip() for v in (title, variable, baseline, variant, tracks)) or not 1 <= len(criteria_values) <= 12:
                st.error('제목·변수·A/B 조건·기준곡과 평가 기준 1~12개를 입력해 주세요.')
            else:
                env = next((e for e in environments if e['id'] == env_id), None)
                store.save('experiment', {'title': title, 'variable': variable, 'baseline': baseline, 'variant': variant,
                    'fixedConditions': fixed, 'referenceTracks': tracks, 'protocol': protocol, 'criteria': criteria_values,
                    'expectedEffect': effect, 'rollbackRule': rollback, 'category': category, 'origin': 'recommendation' if plan else 'custom', 'templateId': plan.get('templateId'),
                    'environmentId': env_id or None, 'environmentSnapshot': json.dumps(env, ensure_ascii=False) if env else None})
                message('실험 계획을 저장했습니다.')
    for experiment in experiments:
        with st.expander(experiment.get('title', '청음 실험')):
            for field, label in [('variable', '변수'), ('baseline', 'A · 기준'), ('variant', 'B · 비교'), ('fixedConditions', '고정 조건'), ('referenceTracks', '기준곡'), ('protocol', '실행 순서'), ('expectedEffect', '가설'), ('rollbackRule', '복귀 기준')]:
                st.markdown('**' + label + '**')
                st.write(experiment.get(field) or '미기록')
            snapshot = parse(experiment.get('environmentSnapshot'))
            if snapshot:
                st.caption('실험 당시 청취 환경 · ' + snapshot.get('name', ''))
                st.write(snapshot.get('details', ''))
            runs = [r for r in store.list('run') if r.get('experimentId') == experiment['id']]
            for run in runs:
                st.markdown('**청음 결과 · ' + run.get('listenedOn', '') + '**')
                st.write('A: ' + run.get('observationsA', ''))
                st.write('B: ' + run.get('observationsB', ''))
                for score in score_rows(run.get('scores'), experiment.get('criteria', [])):
                    st.markdown('**' + score['criterion'] + '**')
                    if score.get('impression'):
                        st.write(score['impression'])
                    if score.get('a') is not None or score.get('b') is not None:
                        st.caption('점수 A ' + str(score.get('a') if score.get('a') is not None else '미평가') + ' · B ' + str(score.get('b') if score.get('b') is not None else '미평가'))
                    if score.get('detail'):
                        st.write(score['detail'])
                st.write(run.get('decision', '') + ' · ' + run.get('conclusion', ''))
                if run.get('originalSnapshot'):
                    st.caption('정정 이유 · ' + run.get('correctionReason', ''))
                    st.write(parse(run['originalSnapshot'], run['originalSnapshot']))
            run_options = [''] + [r['id'] for r in runs if r.get('revision', 1) == 1]
            run_id = st.selectbox('결과 추가 / 한 번 정정', run_options, key='run_pick_' + experiment['id'], format_func=lambda v: next((r.get('listenedOn', '') for r in runs if r['id'] == v), '새 청음 결과'))
            original = next((r for r in runs if r['id'] == run_id), {})
            with st.form('run_' + experiment['id'] + run_id):
                listened = st.text_input('청음 날짜', value=original.get('listenedOn', now()[:10]))
                obs_a = st.text_area('A에서 들은 점', value=original.get('observationsA', ''), max_chars=6000)
                obs_b = st.text_area('B에서 들은 점', value=original.get('observationsB', ''), max_chars=6000)
                scores = []
                for prior in score_rows(original.get('scores'), experiment.get('criteria', [])):
                    criterion = prior['criterion']
                    choices = ['선호 미기록'] + IMPRESSIONS
                    value = prior.get('impression')
                    impression = st.selectbox(criterion + ' 선호', choices, index=choices.index(value) if value in choices else 0)
                    detail = st.text_input(criterion + ' 차이 메모', value=prior.get('detail') or '', max_chars=500)
                    score = dict(prior, impression=None if impression == '선호 미기록' else impression, detail=detail)
                    if run_id:
                        left, right = st.columns(2)
                        for column, side in [(left, 'a'), (right, 'b')]:
                            score[side] = column.selectbox(criterion + ' ' + side.upper() + ' 점수 정정', [None, 1, 2, 3, 4, 5], index=prior.get(side) or 0, format_func=lambda v: '미평가' if v is None else str(v))
                    scores.append(score)
                decisions = list(DECISIONS)
                if original.get('decision') and original['decision'] not in decisions:
                    decisions.append(original['decision'])
                decision = st.selectbox('이번 결론', decisions, index=decisions.index(original.get('decision')) if original.get('decision') in decisions else 0)
                conclusion = st.text_area('결론의 근거', value=original.get('conclusion', ''), max_chars=6000)
                reason = st.text_input('정정 이유', max_chars=1000) if run_id else ''
                add = st.form_submit_button('결과 한 번 정정' if run_id else '청음 결과 저장')
            if add:
                if not obs_a.strip() and not obs_b.strip() and not conclusion.strip() and not any(s.get('impression') or s.get('detail', '').strip() or s.get('a') is not None or s.get('b') is not None for s in scores):
                    st.error('청음 관찰이나 결론을 남겨 주세요.')
                elif run_id and not reason.strip():
                    st.error('정정 이유를 입력해 주세요.')
                else:
                    try:
                        payload = dict(original, experimentId=experiment['id'], listenedOn=listened, observationsA=obs_a, observationsB=obs_b, scores=scores, decision=decision, conclusion=conclusion)
                        if run_id:
                            if all(payload.get(k) == original.get(k) for k in ('listenedOn', 'observationsA', 'observationsB', 'scores', 'decision', 'conclusion')):
                                raise Conflict('바뀐 내용이 없습니다. 원래 기록은 그대로 유지합니다.')
                            payload.update(originalSnapshot=json.dumps(original, ensure_ascii=False), correctionReason=reason)
                        store.save('run', payload, original.get('revision'))
                        message('결과를 저장했습니다. 미평가는 부정 평가가 아닙니다.')
                    except Conflict as error:
                        st.error(str(error))


def backup_page(store):
    st.caption('DATA & BACKUPS')
    st.subheader('전체 백업과 기록 가져오기')
    st.write('감상 원문·사진·수정 이력·청취 환경·청음 실험을 ZIP 한 파일로 내보내고 가져옵니다.')
    st.caption('백업에는 개인 기록이 들어 있습니다. GitHub에 올리지 않고 개인 보관함에 보관해 주세요.')
    if hasattr(store, 'diagnostics'):
        with st.expander('저장 연결 점검'):
            st.caption('연결된 페이지와 앱이 찾은 기록 수를 확인합니다. 토큰과 감상 원문은 표시하지 않습니다.')
            if st.button('저장 연결 상태 확인'):
                st.json(store.diagnostics())
    if st.button('전체 백업 준비'):
        st.session_state['backup_download'] = store.export()
    if st.session_state.get('backup_download'):
        st.download_button('내 기록 ZIP 내려받기', data=st.session_state['backup_download'], file_name='listening-companion-backup-' + now()[:10] + '.zip', mime='application/zip')
    upload = st.file_uploader('기존 기록 ZIP 가져오기', type=['zip'])
    if upload:
        try:
            counts = store.import_backup(upload.getvalue(), dry_run=True)
            st.write({'감상': counts['note'], '사진': counts['photos'], '수정 이력': counts['histories'], '청취 환경': counts['environment'], '실험': counts['experiment'], '실험 결과': counts['run']})
            st.caption('같은 기록은 중복 생성하지 않습니다. 같은 ID의 내용이 다르면 전체 가져오기를 중단합니다.')
            if st.button('이 백업을 내 앱으로 가져오기', type='primary'):
                store.import_backup(upload.getvalue(), dry_run=False)
                message('백업을 가져왔습니다. 원래 사이트의 기록은 삭제하지 않았습니다.')
        except (ValueError, KeyError, OSError) as error:
            st.error('백업을 가져오지 못했습니다. ' + str(error))


def main():
    mode = setting('LC_DEPLOYMENT_MODE', 'cloud')
    url = setting('DATABASE_URL')
    backend = setting('STORAGE_BACKEND', 'postgres').lower()
    notion_token, notion_root = setting('NOTION_TOKEN'), setting('NOTION_PAGE_ID')
    if mode == 'local':
        (ROOT / 'data').mkdir(exist_ok=True)
        if backend != 'notion':
            url = url or 'sqlite:///' + str(ROOT / 'data/listening.sqlite3')
    if backend == 'notion' and (not notion_token or not notion_root):
        st.title('Listening Companion')
        st.info('Notion 저장 연결이 필요합니다. 이 앱의 Secrets에 NOTION_TOKEN과 NOTION_PAGE_ID를 등록하고, Notion 페이지의 연결 메뉴에서 전용 연결을 추가해 주세요.')
        st.stop()
    if backend not in ('postgres', 'notion'):
        st.error('STORAGE_BACKEND는 notion 또는 postgres로 설정해 주세요.')
        st.stop()
    if mode != 'local' and backend != 'notion' and (not url or not url.startswith(('postgres://', 'postgresql://', 'postgresql+psycopg://'))):
        st.title('Listening Companion')
        st.info('감상 기록을 영구 보관할 데이터베이스 연결이 필요합니다. 배포 안내에 따라 이 앱의 Secrets에 DATABASE_URL을 등록해 주세요.')
        st.caption('Streamlit Cloud의 임시 디스크에는 감상을 저장하지 않습니다.')
        st.stop()
    if mode != 'local':
        password_hash = setting('APP_PASSWORD_HASH')
        direct_password = setting('APP_PASSWORD')
        if not password_hash and len(direct_password) < 12:
            st.info('개인 기록 보호를 위한 앱 비밀번호 설정이 필요합니다. 배포 안내를 확인해 주세요.')
            st.stop()
    try:
        store = database(url, backend, notion_token, notion_root)
    except NotionError as error:
        st.error(str(error))
        st.stop()
    except Exception:
        st.error('저장소에 연결하지 못했습니다. 연결 설정을 확인해 주세요. 비밀번호와 주소의 상세 값은 화면에 표시하지 않습니다.')
        st.stop()
    if mode != 'local':
        fingerprint = hashlib.sha256((password_hash or direct_password).encode()).hexdigest()
        if st.session_state.get('authorized') != fingerprint:
            st.title('나만의 감상실에 오신 것을 환영합니다.')
            blocked = store.login_blocked()
            with st.form('login'):
                password = st.text_input('앱 비밀번호', type='password')
                entered = st.form_submit_button('감상실 열기', disabled=blocked > 0)
            if blocked:
                st.info(f'잠시 후 다시 시도해 주세요. 남은 대기 시간: {blocked}초')
            if entered:
                success = valid_password(password, password_hash) if password_hash else valid_secret_password(password, direct_password)
                store.login_result(success)
                if success:
                    st.session_state['authorized'] = fingerprint
                    st.rerun()
                st.error('비밀번호를 확인해 주세요.')
            st.stop()
    st.markdown('<div class="lc-brand">LC<span>LISTENING COMPANION</span><small>나만의 감상실</small></div>', unsafe_allow_html=True)
    pages = ['오늘의 음악', '감상 기록', '청음 실험', '청취 환경', '나의 취향', '백업·가져오기']
    if st.session_state.get('pending_page'):
        st.session_state['page'] = st.session_state.pop('pending_page')
    page = st.radio('감상실', pages, key='page', horizontal=True, label_visibility='collapsed')
    if mode != 'local' and st.button('잠그기'):
        st.session_state.clear()
        st.rerun()
    headings = {'오늘의 음악': '음반을 발견하고, 더 깊이 듣기.', '감상 기록': '좋았던 순간을, 잊지 않도록.', '청음 실험': '나의 소리를 찾아가는 실험.', '청취 환경': '나의 공간과 오디오.', '나의 취향': '들을수록 선명해지는 취향.', '백업·가져오기': '내 기록은 내가 보관하기.'}
    st.markdown('<section class="lc-intro"><span>A SPACE FOR CLOSE LISTENING</span><h1>' + headings[page] + '</h1><p>음반의 매력을 발견하고, 나만의 청취 경험을 쌓아갑니다.</p></section>', unsafe_allow_html=True)
    if st.session_state.get('notice'):
        st.success(st.session_state.pop('notice'))
    api_key, model = setting('OPENAI_API_KEY'), setting('OPENAI_MODEL', 'gpt-4.1-mini')
    try:
        if page == '백업·가져오기':
            backup_page(store)
            return
        notes = store.list('note')
        environments = [e for e in store.list('environment') if not e.get('archived')]
        if page in ('오늘의 음악', '감상 기록', '나의 취향'):
            primary, aside = st.columns([1.65, 1], gap='large')
            with primary, st.container(border=True):
                if page == '오늘의 음악':
                    discover(store, notes, api_key, model)
                elif page == '감상 기록':
                    journal(store, notes, environments, api_key, model, show_records=False)
                else:
                    st.subheader('지금의 취향을 남겨주세요.')
                    with st.form('taste'):
                        content = st.text_area('좋아하는 음악과 녹음의 특징', max_chars=6000)
                        save = st.form_submit_button('나의 취향 저장', type='primary')
                    if save and content.strip():
                        store.save('note', {'kind': '취향', 'content': content})
                        message('취향 원문과 날짜를 저장했습니다.')
            with aside:
                st.markdown('''<aside class="lc-principles"><small>OUR LISTENING NOTES</small><h2>같은 음반,<br>서로 다른 기준.</h2><div><b>01　음악성</b><p>해석, 흐름, 긴장감과 연주의 설득력.</p></div><div><b>02　녹음 품질</b><p>홀톤, 공기감, 악기 질감과 균형.</p></div><div><b>03　내 시스템 재생</b><p>나의 공간과 오디오에서 느끼는 감상.</p></div><p>아직 평가하지 않은 항목은 비워 둡니다. 한 번의 기분으로 평생의 취향을 정하지 않습니다.</p></aside>''', unsafe_allow_html=True)
            recent_notes(store, [n for n in notes if n.get('kind') == '취향'] if page == '나의 취향' else notes)
        elif page == '청취 환경':
            environment_page(store, environments)
        elif page == '청음 실험':
            experiment_page(store, environments)
    except (Conflict, MusicError, NotionError) as error:
        st.error(str(error))
    except Exception:
        st.error('작업을 완료하지 못했습니다. 기록은 저장소에 유지됩니다. 새로고침 후 확인해 주세요.')
    st.markdown('<div class="lc-footer">LISTENING COMPANION　 음악을 듣고, 나를 알아가는 시간.</div>', unsafe_allow_html=True)


if __name__ == '__main__':
    main()
