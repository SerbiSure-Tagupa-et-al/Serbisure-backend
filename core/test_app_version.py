from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from . import app_releases


def release_payload():
    return {'tag_name': 'v2.1.2', 'body': 'A better SerbiSure.', 'draft': False, 'prerelease': False,
            'assets': [{'name': 'SerbiSure-v2.1.2.apk', 'state': 'uploaded', 'size': 70000000,
                        'browser_download_url': f'https://github.com/{app_releases.REPOSITORY}/releases/download/v2.1.2/SerbiSure-v2.1.2.apk'}]}


class AppVersionTests(SimpleTestCase):
    def setUp(self):
        app_releases._cache.clear()
        self.addCleanup(app_releases._cache.clear)

    def response(self, payload=None, status=200):
        response = Mock(status_code=status)
        response.json.return_value = payload if payload is not None else release_payload()
        return response

    @patch('core.app_releases.requests.get')
    def test_public_endpoint_returns_universal_asset_and_caches_without_database(self, get):
        payload = release_payload()
        payload['assets'].insert(0, {**payload['assets'][0], 'name': 'SerbiSure-v2.1.2-arm64.apk'})
        get.return_value = self.response(payload)
        for _ in range(2):
            response = self.client.get('/api/v1/app-version/', HTTP_AUTHORIZATION='Bearer expired-token')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['latest_version'], '2.1.2')
            self.assertTrue(response.json()['apk_url'].endswith('/SerbiSure-v2.1.2.apk'))
            self.assertEqual(response['Cache-Control'], 'no-store')
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs['timeout'], (3, 8))

    @patch('core.app_releases.requests.get')
    def test_no_release_is_an_explicit_empty_response(self, get):
        get.return_value = self.response(status=404)
        response = self.client.get('/api/v1/app-version/')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['latest_version'])
        self.assertIsNone(response.json()['apk_url'])

    @patch('core.app_releases.requests.get')
    def test_network_failure_is_short_cached_and_never_leaks_internals(self, get):
        get.side_effect = requests.Timeout('secret internal detail')
        for _ in range(2):
            response = self.client.get('/api/v1/app-version/')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('secret', response.content.decode())
        get.assert_called_once()
        app_releases._cache.clear()
        get.side_effect = None
        get.return_value = self.response()
        self.assertEqual(self.client.get('/api/v1/app-version/').status_code, 200)

    @patch('core.app_releases.requests.get')
    def test_rate_limit_or_upstream_error_does_not_claim_no_update(self, get):
        get.return_value = self.response(status=403)
        get.return_value.raise_for_status.side_effect = requests.HTTPError('rate limited')
        self.assertEqual(self.client.get('/api/v1/app-version/').status_code, 503)

    @patch('core.app_releases.requests.get')
    def test_incomplete_or_untrusted_releases_fail_closed(self, get):
        for scenario in ['missing-apk', 'arm-only', 'untrusted-url', 'draft', 'prerelease', 'bad-version', 'empty-file', 'bad-json']:
            with self.subTest(scenario=scenario):
                app_releases._cache.clear()
                payload = release_payload()
                if scenario == 'missing-apk': payload['assets'] = []
                if scenario == 'arm-only': payload['assets'][0]['name'] = 'SerbiSure-v2.1.2-arm64.apk'
                if scenario == 'untrusted-url': payload['assets'][0]['browser_download_url'] = 'https://example.com/app.apk'
                if scenario in ['draft', 'prerelease']: payload[scenario] = True
                if scenario == 'bad-version': payload['tag_name'] = 'nightly'
                if scenario == 'empty-file': payload['assets'][0]['size'] = 0
                if scenario == 'bad-json': payload = ['not a release']
                get.return_value = self.response(payload)
                self.assertEqual(self.client.get('/api/v1/app-version/').status_code, 503)

    def test_endpoint_is_read_only(self):
        self.assertEqual(self.client.post('/api/v1/app-version/', {}).status_code, 405)
