import json
from unittest.mock import MagicMock, patch
import requests
from urllib3.exceptions import ReadTimeoutError
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from .sentiment import analyze_feedback, SentimentUnavailable


@override_settings(HUGGINGFACE_API_TOKEN='private-test-token',
                   HUGGINGFACE_SENTIMENT_SPACE_URL='https://example.hf.space',
                   HUGGINGFACE_SENTIMENT_MODEL_ID='Rhoii/cebuano-taglish-sentiment',
                   HUGGINGFACE_SENTIMENT_TIMEOUT_SECONDS=40)
class SentimentTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.payload = [{'label': 'Negative', 'confidences': None}, .7271,
                        {'Negative': .7271, 'Positive': .1981, 'Neutral': .0748}]
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        post = self.session.post.return_value.__enter__.return_value
        post.status_code = 200
        post.json.return_value = {'event_id': 'a' * 32}
        self.stream = self.session.get.return_value.__enter__.return_value
        self.stream.status_code = 200
        self.factory = patch('reviews.sentiment.requests.Session', return_value=self.session).start()
        self.addCleanup(patch.stopall)

    def complete(self, payload=None):
        self.stream.iter_lines.return_value = [
            'event: heartbeat', 'data: null', '',
            'event: complete', 'data: ' + json.dumps(self.payload if payload is None else payload), '',
        ]

    def test_authenticated_gradio_handshake_and_real_result(self):
        self.complete()
        result = analyze_feedback('Dili kabalo mo lipyo')
        self.assertEqual(result['label'], 'Negative')
        self.assertEqual(result['confidence'], .7271)
        self.assertEqual(result['probabilities'], self.payload[2])
        self.assertEqual(result['source'], 'huggingface')
        for call in (self.session.post.call_args, self.session.get.call_args):
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer private-test-token')
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertEqual(self.session.post.call_args.kwargs['json'], {'data': ['Dili kabalo mo lipyo']})
        self.assertEqual(analyze_feedback('Dili kabalo mo lipyo'), result)
        self.session.post.assert_called_once()

    def test_error_event_with_http_200_is_not_neutral(self):
        self.stream.iter_lines.return_value = ['event: error', 'data: {"error":"quota exceeded"}', '']
        with self.assertRaises(SentimentUnavailable) as error:
            analyze_feedback('Pangit ug batasan')
        self.assertEqual(error.exception.reason, 'provider_error_event')

    def test_invalid_or_inconsistent_output_is_rejected(self):
        for payload in ([{'label': 'LABEL_0'}, .9, self.payload[2]],
                        [{'label': 'Positive'}, .7271, self.payload[2]],
                        [{'label': 'Negative'}, float('nan'), self.payload[2]],
                        [{'label': 'Negative'}, .7271, {'Negative': .7271}], [None]):
            with self.subTest(payload=payload):
                self.complete(payload)
                with self.assertRaises(SentimentUnavailable):
                    analyze_feedback('Pangit ug batasan')

    def test_missing_token_and_untrusted_destination_fail_closed(self):
        for settings in ({'HUGGINGFACE_API_TOKEN': ''},
                         {'HUGGINGFACE_SENTIMENT_SPACE_URL': 'http://example.hf.space'},
                         {'HUGGINGFACE_SENTIMENT_SPACE_URL': 'https://example.org'}):
            with self.subTest(settings=settings), override_settings(**settings):
                with self.assertRaises(SentimentUnavailable):
                    analyze_feedback('Pangit ug batasan')
        self.session.post.assert_not_called()

    def test_network_exception_does_not_expose_private_token(self):
        self.session.post.side_effect = requests.ConnectionError('private-test-token')
        with self.assertLogs('reviews.sentiment', level='WARNING') as logs:
            with self.assertRaises(SentimentUnavailable) as error:
                analyze_feedback('Pangit ug batasan')
        self.assertNotIn('private-test-token', str(error.exception))
        self.assertNotIn('private-test-token', str(logs.output))

    def test_slow_submission_succeeds_and_stream_uses_only_remaining_budget(self):
        clock = [100.0]

        def submitted():
            clock[0] = 128.0  # Longer than the previous ten-second submission limit.
            return {'event_id': 'a' * 32}

        self.session.post.return_value.__enter__.return_value.json.side_effect = submitted
        self.complete()
        with patch('reviews.sentiment.time.monotonic', side_effect=lambda: clock[0]):
            self.assertEqual(analyze_feedback('Dili kabalo mo lipyo')['label'], 'Negative')
        self.assertEqual(self.session.post.call_args.kwargs['timeout'], (5, 30))
        self.assertEqual(self.session.get.call_args.kwargs['timeout'], (5, 12))

    def test_submit_timeouts_log_the_stage_without_private_exception_messages(self):
        for exception in (requests.ConnectTimeout, requests.ReadTimeout):
            with self.subTest(exception=exception):
                self.session.post.side_effect = exception('private-test-token private-feedback')
                with self.assertLogs('reviews.sentiment', level='WARNING') as logs:
                    with self.assertRaises(SentimentUnavailable) as error:
                        analyze_feedback('Dili kabalo mo lipyo')
                self.assertEqual(error.exception.reason, 'timeout')
                entry = json.loads(logs.records[-1].getMessage())
                self.assertEqual(entry['stage'], 'submit_prediction')
                self.assertEqual(entry['error_type'], exception.__name__)
                self.assertIsNone(entry['http_status'])
                self.assertNotIn('private-test-token', str(logs.output))
                self.assertNotIn('private-feedback', str(logs.output))
        self.session.get.assert_not_called()

    def test_wrapped_stream_timeout_is_retryable_and_not_cached(self):
        self.stream.iter_lines.side_effect = requests.ConnectionError(
            ReadTimeoutError(None, 'private-url', 'private-test-token'))
        with self.assertLogs('reviews.sentiment', level='WARNING') as logs:
            with self.assertRaises(SentimentUnavailable) as error:
                analyze_feedback('Dili kabalo mo lipyo')
        self.assertEqual(error.exception.reason, 'timeout')
        entry = json.loads(logs.records[-1].getMessage())
        self.assertEqual(entry['stage'], 'wait_prediction')
        self.assertEqual(entry['http_status'], 200)
        self.assertEqual(entry['error_type'], 'ConnectionError')
        self.assertNotIn('private-test-token', str(logs.output))
        self.stream.iter_lines.side_effect = None
        self.complete()
        self.assertEqual(analyze_feedback('Dili kabalo mo lipyo')['label'], 'Negative')
        self.assertEqual(self.session.post.call_count, 2)

    def test_http_failures_log_status_and_stage_without_response_body(self):
        for stage, response, statuses in (
            ('submit_prediction', self.session.post.return_value.__enter__.return_value, (401, 429, 503)),
            ('wait_prediction', self.stream, (503,)),
        ):
            for status in statuses:
                with self.subTest(stage=stage, status=status):
                    response.status_code = status
                    with self.assertLogs('reviews.sentiment', level='WARNING') as logs:
                        with self.assertRaises(SentimentUnavailable) as error:
                            analyze_feedback('Dili kabalo mo lipyo')
                    self.assertEqual(error.exception.reason, 'provider_http_error')
                    entry = json.loads(logs.records[-1].getMessage())
                    self.assertEqual((entry['stage'], entry['http_status']), (stage, status))
                    response.status_code = 200

    def test_malformed_json_is_identified_without_logging_its_contents(self):
        self.stream.iter_lines.return_value = ['event: complete', 'data: private-test-token', '']
        with self.assertLogs('reviews.sentiment', level='WARNING') as logs:
            with self.assertRaises(SentimentUnavailable) as error:
                analyze_feedback('Dili kabalo mo lipyo')
        self.assertEqual(error.exception.reason, 'invalid_response')
        self.assertEqual(json.loads(logs.records[-1].getMessage())['stage'], 'parse_prediction')
        self.assertNotIn('private-test-token', str(logs.output))

    def test_exhausted_budget_does_not_start_a_stream(self):
        clock = [100.0]

        def submitted():
            clock[0] = 141.0
            return {'event_id': 'a' * 32}

        self.session.post.return_value.__enter__.return_value.json.side_effect = submitted
        with patch('reviews.sentiment.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(SentimentUnavailable) as error:
                analyze_feedback('Dili kabalo mo lipyo')
        self.assertEqual(error.exception.reason, 'timeout')
        self.session.get.assert_not_called()

    def test_complete_event_at_eof_after_deadline_is_not_saved(self):
        clock = [100.0]

        def lines(**kwargs):
            yield 'event: complete'
            yield 'data: ' + json.dumps(self.payload)
            clock[0] = 141.0

        self.stream.iter_lines.side_effect = lines
        with patch('reviews.sentiment.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(SentimentUnavailable) as error:
                analyze_feedback('Dili kabalo mo lipyo')
        self.assertEqual(error.exception.reason, 'timeout')

    def test_incomplete_stream_and_http_failure_are_not_predictions(self):
        self.stream.iter_lines.return_value = ['event: heartbeat', 'data: null', '']
        with self.assertRaises(SentimentUnavailable):
            analyze_feedback('Pangit ug batasan')
        self.stream.status_code = 503
        with self.assertRaises(SentimentUnavailable):
            analyze_feedback('Pangit ug batasan')
