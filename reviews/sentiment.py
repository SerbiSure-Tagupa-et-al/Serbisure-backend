"""Server-only Hugging Face inference. Failures never become invented sentiment labels."""
import hashlib
import json
import logging
import math
import re
import time
from urllib.parse import urlsplit
import requests
from urllib3.exceptions import ReadTimeoutError
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)
LABELS = {'positive': 'Positive', 'negative': 'Negative', 'neutral': 'Neutral'}


class SentimentUnavailable(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Feedback analysis is temporarily unavailable. Please try again.')


def _score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise SentimentUnavailable('invalid_prediction')
    return float(value)


def _prediction(payload, space_url):
    if not isinstance(payload, list) or len(payload) != 3:
        raise SentimentUnavailable('invalid_prediction')
    raw_label = payload[0].get('label') if isinstance(payload[0], dict) else payload[0]
    label = LABELS.get(str(raw_label).strip().lower())
    if label is None or not isinstance(payload[2], dict):
        raise SentimentUnavailable('invalid_prediction')
    confidence = _score(payload[1])
    probabilities = {}
    for key, value in payload[2].items():
        name = LABELS.get(str(key).lower())
        if name is None or name in probabilities:
            raise SentimentUnavailable('invalid_prediction')
        probabilities[name] = _score(value)
    if (set(probabilities) != set(LABELS.values()) or abs(sum(probabilities.values()) - 1) > .01
            or abs(probabilities[label] - confidence) > .001 or confidence + .001 < max(probabilities.values())):
        raise SentimentUnavailable('invalid_prediction')
    return {'label': label, 'confidence': confidence, 'probabilities': probabilities,
            'source': 'huggingface', 'model': settings.HUGGINGFACE_SENTIMENT_MODEL_ID,
            'space_url': space_url, 'analyzed_at': timezone.now().isoformat()}

def _log_sentiment_call(started, status, reason="", *, stage="", http_status=None, error_type=""):
    level = logging.INFO if status == "success" else logging.WARNING
    logger.log(level, json.dumps({
        "message": "sentiment_api",
        "provider": "huggingface",
        "status": status,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
        "reason": reason,
        "stage": stage,
        "http_status": http_status,
        "error_type": error_type,
    }))

def analyze_feedback(text):
    text = text.strip()
    token = settings.HUGGINGFACE_API_TOKEN
    url = settings.HUGGINGFACE_SENTIMENT_SPACE_URL.rstrip('/')
    parts = urlsplit(url)
    if not token:
        raise SentimentUnavailable('missing_configuration')
    if parts.scheme != 'https' or not (parts.hostname or '').endswith('.hf.space') or parts.username or parts.password or parts.query or parts.fragment:
        raise SentimentUnavailable('invalid_configuration')
    if not 10 <= len(text) <= 1000:
        raise SentimentUnavailable('invalid_feedback')
    digest = hashlib.sha256((url + settings.HUGGINGFACE_SENTIMENT_MODEL_ID + '\0' + text).encode()).hexdigest()
    key = 'review_sentiment_v1_' + digest
    existing = cache.get(key)
    if existing:
        return existing
    started = time.monotonic()
    deadline = started + settings.HUGGINGFACE_SENTIMENT_TIMEOUT_SECONDS
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    stage, http_status = 'submit_prediction', None
    try:
        with requests.Session() as session:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SentimentUnavailable('timeout')
            with session.post(url + '/gradio_api/call/predict', headers=headers, json={'data': [text]},
                              timeout=(min(5, remaining), min(remaining, 30)), allow_redirects=False) as response:
                http_status = response.status_code
                if http_status != 200:
                    raise SentimentUnavailable('provider_http_error')
                event_id = response.json().get('event_id')
                if not isinstance(event_id, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', event_id):
                    raise SentimentUnavailable('invalid_event')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SentimentUnavailable('timeout')
            stage, http_status = 'wait_prediction', None
            # Keep a short idle timeout for a stalled stream; heartbeats keep it alive.
            # The 40-second budget plus this idle wait fits the mobile's 60-second timeout.
            with session.get(url + '/gradio_api/call/predict/' + event_id, headers=headers, stream=True,
                             timeout=(min(5, remaining), min(remaining, 15)), allow_redirects=False) as response:
                http_status = response.status_code
                if http_status != 200:
                    raise SentimentUnavailable('provider_http_error')
                event, data, size = '', [], 0
                for line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if time.monotonic() >= deadline:
                        raise SentimentUnavailable('timeout')
                    if isinstance(line, bytes):
                        line = line.decode('utf-8')
                    size += len(line)
                    if size > 131072:
                        raise SentimentUnavailable('invalid_stream')
                    if not line:
                        if event == 'error':
                            # Never log the provider payload, which may contain private information.
                            raise SentimentUnavailable('provider_error_event')
                        if event == 'complete':
                            stage = 'parse_prediction'
                            result = _prediction(json.loads('\n'.join(data)), url)
                            _log_sentiment_call(started, "success", stage=stage, http_status=http_status)
                            cache.set(key, result, timeout=300)
                            return result
                        event, data = '', []
                    elif line.startswith('event:'):
                        event = line[6:].strip()
                    elif line.startswith('data:'):
                        data.append(line[5:].lstrip())
                if time.monotonic() >= deadline:
                    raise SentimentUnavailable('timeout')
                if event == 'error':
                    raise SentimentUnavailable('provider_error_event')
                if event == 'complete':
                    stage = 'parse_prediction'
                    result = _prediction(json.loads('\n'.join(data)), url)
                    _log_sentiment_call(started, "success", stage=stage, http_status=http_status)
                    cache.set(key, result, timeout=300)
                    return result
                raise SentimentUnavailable('incomplete_stream')
    except SentimentUnavailable as error:
        _log_sentiment_call(started, "failed", error.reason, stage=stage, http_status=http_status)
        raise
    except (requests.RequestException, ValueError, TypeError, AttributeError, UnicodeError) as error:
        # Requests wraps streaming read timeouts in ConnectionError.
        if isinstance(error, requests.Timeout) or (error.args and isinstance(error.args[0], ReadTimeoutError)):
            reason = 'timeout'
        elif isinstance(error, requests.ConnectionError):
            reason = 'connection_error'
        elif isinstance(error, (ValueError, TypeError, AttributeError, UnicodeError)):
            reason = 'invalid_response'
        else:
            reason = 'provider_unavailable'
        _log_sentiment_call(started, "failed", reason, stage=stage, http_status=http_status,
                            error_type=type(error).__name__)
        raise SentimentUnavailable(reason) from None
