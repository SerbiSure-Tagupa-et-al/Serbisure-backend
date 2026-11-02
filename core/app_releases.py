"""Public Android release metadata; no user data or database access."""
import re
import threading
from urllib.parse import quote

import requests
from django.core.cache.backends.locmem import LocMemCache


REPOSITORY = 'SerbiSure-Tagupa-et-al/SerbiSure-Frontend'
API_URL = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
_cache = LocMemCache('serbisure-app-releases', {})
_lock = threading.Lock()


class ReleaseUnavailable(Exception):
    pass


def _fetch_release():
    response = requests.get(API_URL, headers={
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'SerbiSure-Version-Check',
    }, timeout=(3, 8), allow_redirects=False)
    if response.status_code == 404:
        return {'latest_version': None, 'release_notes': '', 'apk_url': None,
                'apk_size': None}
    response.raise_for_status()
    release = response.json()
    tag = release.get('tag_name', '')
    match = re.fullmatch(r'v?(\d+)\.(\d+)(?:\.(\d+))?', tag)
    if not match or release.get('draft') or release.get('prerelease'):
        raise ValueError('A stable release is required')
    version = '.'.join(str(int(part or 0)) for part in match.groups())
    name = f'SerbiSure-v{version}.apk'
    # Never select an ARM-only APK or a URL from another repository.
    expected_url = f'https://github.com/{REPOSITORY}/releases/download/{quote(tag, safe="")}/{name}'
    asset = next((item for item in release.get('assets', [])
                  if item.get('name') == name and item.get('state') == 'uploaded'
                  and item.get('browser_download_url') == expected_url), None)
    if not asset or not isinstance(asset.get('size'), int) or asset['size'] <= 0:
        raise ValueError('The universal APK is not ready')
    return {'latest_version': version, 'release_notes': (release.get('body') or '')[:12000],
            'apk_url': expected_url, 'apk_size': asset['size']}


def latest_release():
    """Ten-minute per-process cache; short negative cache avoids retry storms."""
    with _lock:
        cached = _cache.get('latest')
        if cached is None:
            try:
                cached = _fetch_release()
            except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError):
                _cache.set('latest', {'unavailable': True}, 30)
                raise ReleaseUnavailable from None
            _cache.set('latest', cached, 600)
        if cached.get('unavailable'):
            raise ReleaseUnavailable
        return cached
