"""Original Listening Companion record shapes and catalogue interactions."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import re
import unicodedata

IMPRESSIONS = ['A가 나음', '비슷함', 'B가 나음', '판단 보류']
DECISIONS = ['판단 보류', 'B 채택', 'A로 복귀', '차이 없음']


def score_rows(value, criteria):
    """Read original array scores and the early migration's dictionary scores."""
    by_name = {row['criterion']: row for row in value if isinstance(row, dict) and row.get('criterion')} if isinstance(value, list) else value if isinstance(value, dict) else {}
    aliases = {'A 선호': 'A가 나음', 'B 선호': 'B가 나음', '차이 없음': '비슷함', '미평가': None}
    rows = []
    for name in criteria:
        old = by_name.get(name)
        row = deepcopy(old) if isinstance(old, dict) else {'impression': aliases.get(old, old) if isinstance(old, str) else None}
        row.setdefault('a', None)
        row.setdefault('b', None)
        row.setdefault('impression', None)
        row.setdefault('detail', '')
        row['criterion'] = name
        rows.append(row)
    return rows


def album_identity(value):
    value = str(value or '')
    return value.removeprefix('itunes-us-') if value.startswith('itunes-us-') else value


def reaction_active(row, now=None):
    action = row.get('action')
    if action in ('clear', None, ''):
        return False
    if action != 'skip':
        return True
    stamp = row.get('updatedAt') or row.get('createdAt')
    if not stamp:
        return False
    try:
        updated = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return (now or datetime.now(timezone.utc)) - updated < timedelta(days=7)
    except (ValueError, TypeError):
        return False


def editorial_selection(recordings, query='', goals=()):
    def normalized(value):
        return ''.join(c for c in unicodedata.normalize('NFD', value).casefold() if not unicodedata.combining(c))
    groups = [g.strip() for g in re.split(r'[,，;；\n]+', query) if g.strip()]
    def matches(recording, group):
        text = normalized(' '.join([recording['work'], recording['artists'], recording['album'], *recording['keywords']]))
        return all(word in text for word in normalized(group).split())
    filtered = [r for r in recordings if not groups or any(matches(r, g) for g in groups)]
    ranked = sorted([{'recording': r, 'matches': [g for g in goals if g in r['tags']]} for r in filtered], key=lambda v: -len(v['matches']))
    exact = [r for r in ranked if not goals or r['matches']]
    return {'items': exact or ranked, 'fallback': bool(ranked and not exact),
            'unmatchedQueries': [g for g in groups if not any(matches(r, g) for r in recordings)],
            'unmatchedGoals': [g for g in goals if not any(g in r['matches'] for r in ranked)]}
