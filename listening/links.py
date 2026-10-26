"""Validate album links without making requests to user-supplied URLs."""
import re
from urllib.parse import urlsplit

class MusicError(ValueError):
    pass

def manual_album(url, title, artists):
    if not all(isinstance(v, str) for v in (url, title, artists)) or len(url) > 2000:
        raise MusicError('음반 주소와 정보를 확인해 주세요.')
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        if parsed.hostname == 'music.apple.com':
            match = re.fullmatch(r'/([A-Za-z]{2})/album/(?:[^/]+/)?([1-9][0-9]{0,15})/?', parsed.path)
            if not match:
                raise ValueError()
            provider, record_id = 'Apple Music', match[2]
            target = f'https://music.apple.com/{match[1].lower()}/album/{record_id}'
        elif parsed.hostname in ('tidal.com', 'www.tidal.com', 'listen.tidal.com'):
            match = re.fullmatch(r'/(?:browse/)?album/([1-9][0-9]{0,15})/?', parsed.path)
            if not match:
                raise ValueError()
            provider, record_id = 'TIDAL', match[1]
            target = f'https://tidal.com/browse/album/{record_id}'
        else:
            raise ValueError()
    except (ValueError, AttributeError):
        raise MusicError('Apple Music 또는 TIDAL의 https 앨범 공유 주소를 입력해 주세요.') from None
    if not title.strip() or not artists.strip() or max(len(title), len(artists)) > 500:
        raise MusicError('음반명과 아티스트를 500자 이내로 입력해 주세요.')
    return {'id': f'manual-{provider.lower().replace(" ", "")}-{record_id}', 'title': title.strip(),
            'artists': artists.strip(), 'url': target, 'provider': provider, 'userProvided': True}

