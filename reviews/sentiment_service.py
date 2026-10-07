import os
import json
import urllib.request
import logging

logger = logging.getLogger(__name__)

HF_SPACE_URL = os.environ.get(
    'HUGGINGFACE_SENTIMENT_SPACE_URL',
    'https://riasgremory2-serbisure-sentiment-api.hf.space'
).rstrip('/')

HF_TOKEN = os.environ.get('HUGGINGFACE_API_TOKEN', '')


def predict_sentiment_hf(text: str, default_sentiment: str = 'Neutral') -> str:
    """
    Calls the SerbiSure HuggingFace Sentiment API Space (XLM-RoBERTa / FiReCS model)
    to classify unstructured user feedback into Positive, Neutral, or Negative based strictly on comment text.
    """
    if not text or not text.strip():
        return default_sentiment

    trimmed = text.strip()

    try:
        call_url = f"{HF_SPACE_URL}/gradio_api/call/predict"
        headers = {
            'Content-Type': 'application/json',
        }
        if HF_TOKEN:
            headers['Authorization'] = f"Bearer {HF_TOKEN}"
        body = json.dumps({'data': [trimmed]}).encode('utf-8')
        req = urllib.request.Request(call_url, data=body, headers=headers, method='POST')

        with urllib.request.urlopen(req, timeout=12) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            event_id = res_data.get('event_id')

        if not event_id:
            return default_sentiment

        event_url = f"{HF_SPACE_URL}/gradio_api/call/predict/{event_id}"
        req2 = urllib.request.Request(event_url, headers=headers, method='GET')
        with urllib.request.urlopen(req2, timeout=12) as response2:
            raw_text = response2.read().decode('utf-8')
            for line in raw_text.split('\n'):
                if line.startswith('data:'):
                    parsed = json.loads(line[5:].strip())
                    if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict) and 'label' in parsed[0]:
                        raw_label = str(parsed[0]['label']).strip().title()
                        if raw_label in ['Positive', 'Neutral', 'Negative']:
                            return raw_label
    except Exception as e:
        logger.warning(f"[HuggingFace Sentiment] Prediction failed: {e}")

    # Fallback: Analyze strictly from text keywords
    lower = trimmed.lower()
    positive_words = ['great', 'good', 'excellent', 'amazing', 'super', 'satisfied', 'recommend', 'mabait', 'maayo', 'sipag', 'masipag', 'kugihan', 'buotan', 'malinis', 'limpyo']
    negative_words = ['bad', 'poor', 'terrible', 'horrible', 'worst', 'disappointed', 'late', 'rude', 'bastos', 'tamad', 'tapulan', 'hugaw', 'madumi', 'salbahe', 'unprofessional', 'guba']

    pos = sum(1 for w in positive_words if w in lower)
    neg = sum(1 for w in negative_words if w in lower)

    if pos > neg:
        return 'Positive'
    if neg > pos:
        return 'Negative'
    return default_sentiment
