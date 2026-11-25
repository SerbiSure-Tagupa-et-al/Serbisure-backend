"""Two-layer Philippine document screening (deterministic rules + Groq cross-check).

Detects wrong/unrelated uploads, cross-slot swaps, fake/sample templates,
fabricated clearances missing required structural anchors, expired clearances,
and blurry/unreadable photos — without adding any third-party libraries.
"""

from __future__ import annotations

import re
from datetime import date

from .document_fields import CORRECTIONS_KEY, NAME_FIELDS, NUMBER_KEYS

SCREENING_KEY = '_screening'

DOC_DISPLAY_NAMES = {
    'nbi_clearance': 'NBI Clearance',
    'police_clearance': 'Police Clearance',
    'tesda_nc2': 'TESDA NC II',
    'national_id_front': 'National ID (Front)',
    'national_id_back': 'National ID (Back)',
}

VALID_DETECTED_TYPES = frozenset({
    'nbi_clearance',
    'police_clearance',
    'tesda_nc2',
    'national_id_front',
    'national_id_back',
    'unrelated',
    'unreadable',
})

# Layer 1 — Official Philippine document structural header & anchor patterns.
_NBI_PATTERNS = (
    re.compile(r'national\s+bureau\s+of\s+investigation', re.IGNORECASE),
    re.compile(r'\bnbi\s+clearance\b', re.IGNORECASE),
    re.compile(r'multi[\s-]*purpose\s+clearance', re.IGNORECASE),
    re.compile(r'no\s+(?:derogatory|criminal)\s+record', re.IGNORECASE),
    re.compile(r'\bnbi\s+id\s*(?:no\.?|number)?\b', re.IGNORECASE),
    re.compile(r'department\s+of\s+justice.*\bnbi\b', re.IGNORECASE | re.DOTALL),
)

_NBI_STRONG_AGENCY_PATTERNS = (
    re.compile(r'national\s+bureau\s+of\s+investigation', re.IGNORECASE),
    re.compile(r'department\s+of\s+justice', re.IGNORECASE),
    re.compile(r'multi[\s-]*purpose\s+clearance', re.IGNORECASE),
    re.compile(r'no\s+(?:derogatory|criminal)\s+record', re.IGNORECASE),
)

_POLICE_PATTERNS = (
    re.compile(r'philippine\s+national\s+police', re.IGNORECASE),
    re.compile(r'national\s+police\s+clearance', re.IGNORECASE),
    re.compile(r'\bpnp\s+clearance\b', re.IGNORECASE),
    re.compile(r'\bpolice\s+clearance\b', re.IGNORECASE),
    re.compile(r'directorate\s+for\s+investigation', re.IGNORECASE),
    re.compile(r'police\s+station.*clearance', re.IGNORECASE | re.DOTALL),
)

_POLICE_STRONG_AGENCY_PATTERNS = (
    re.compile(r'philippine\s+national\s+police', re.IGNORECASE),
    re.compile(r'national\s+police\s+clearance', re.IGNORECASE),
    re.compile(r'\bpnp\b', re.IGNORECASE),
    re.compile(r'directorate\s+for\s+investigation', re.IGNORECASE),
)

_NATIONAL_ID_PATTERNS = (
    re.compile(r'pambansang\s+pagkakakilanlan', re.IGNORECASE),
    re.compile(r'philippine\s+identification', re.IGNORECASE),
    re.compile(r'philippine\s+national\s+id', re.IGNORECASE),
    re.compile(r'philippine\s+statistics\s+authority', re.IGNORECASE),
    re.compile(r'\bphilsys\b', re.IGNORECASE),
    re.compile(r'apelyido\s*/\s*last\s+name', re.IGNORECASE),
    re.compile(r'given\s+names?\s*/\s*mga\s+pangalan', re.IGNORECASE),
    re.compile(r'mga\s+pangalan\s*/\s*given\s+names?', re.IGNORECASE),
    re.compile(r'petsa\s+ng\s+kapanganakan', re.IGNORECASE),
    re.compile(r'\b\d{4}-\d{4}-\d{4}-\d{4}\b'),
)

_NATIONAL_ID_BACK_EXTRA_PATTERNS = (
    re.compile(r'\bdate\s+issued\b', re.IGNORECASE),
    re.compile(r'\bblood\s+type\b', re.IGNORECASE),
    re.compile(r'\b(?:pcn|psn)\b', re.IGNORECASE),
    re.compile(r'property\s+of\s+the\s+republic', re.IGNORECASE),
)

_TESDA_PATTERNS = (
    re.compile(r'\btesda\b', re.IGNORECASE),
    re.compile(r'technical\s+education\s+and\s+skills\s+development', re.IGNORECASE),
    re.compile(r'national\s+certificate', re.IGNORECASE),
    re.compile(r'\bnc\s*(?:ii|2)\b', re.IGNORECASE),
    re.compile(r'certificate\s+no\.?\s*[:#]?\s*[A-Z0-9-]{4,}', re.IGNORECASE),
)

_FAKE_TEMPLATE_PATTERN = re.compile(
    r'\b(?:sample|specimen|fake|dummy|template|test\s+only|for\s+testing\s+only|'
    r'not\s+valid|not\s+official|unofficial|mock\s+document|placeholder)\b',
    re.IGNORECASE,
)

_UNRELATED_COMMON_PATTERN = re.compile(
    r'\b(?:official\s+receipt|sales\s+invoice|vat\s+reg|subtotal|grand\s+total|'
    r'cash\s+tendered|change\s+due|supermarket|grocery|convenience\s+store|'
    r'driver\'?s?\s+license|land\s+transportation\s+office|student\s+id|'
    r'school\s+id|company\s+id|postal\s+id|voter\'?s?\s+id|philhealth|'
    r'sss\s+number|tin\s+id|barangay\s+clearance)\b',
    re.IGNORECASE,
)

_PLAUSIBLE_NUMBER_IN_TEXT = re.compile(
    r'\b(?:[A-Z]{1,4}\d{5,}[A-Z0-9-]*|[A-Z0-9]{2,6}-[A-Z0-9-]{4,}|\d{7,})\b',
    re.IGNORECASE,
)

_PLAUSIBLE_DATE_IN_TEXT = re.compile(
    r'(?:\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|'
    r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b)',
    re.IGNORECASE,
)

_MISSING_VALUES = {
    'n/a', 'none', 'null', 'not detected', 'not detected in ocr',
    'not available', 'unknown', 'unreadable',
}

_BLOCKING_FIELDS = frozenset({
    'document_type_mismatch',
    'document_integrity',
    'image_quality',
    'valid_until',
})


def _has_value(val) -> bool:
    if isinstance(val, bool) or val is None:
        return False
    text = str(val).strip()
    return bool(text) and text.casefold() not in _MISSING_VALUES and any(ch.isalnum() for ch in text)


def _matches_any(text: str, patterns: tuple[re.Pattern, ...]) -> bool:
    return any(p.search(text) for p in patterns)


def _detect_rule_based_types(raw_text: str) -> list[str]:
    matched = []
    if _matches_any(raw_text, _NBI_PATTERNS):
        matched.append('nbi_clearance')
    if _matches_any(raw_text, _POLICE_PATTERNS):
        matched.append('police_clearance')
    if _matches_any(raw_text, _NATIONAL_ID_PATTERNS):
        matched.append('national_id_front')
    if _matches_any(raw_text, _TESDA_PATTERNS):
        matched.append('tesda_nc2')
    return matched


def _is_expected_type_matched(document_type: str, raw_text: str, extracted_data: dict) -> bool:
    if document_type == 'nbi_clearance':
        return _matches_any(raw_text, _NBI_PATTERNS)
    if document_type == 'police_clearance':
        return _matches_any(raw_text, _POLICE_PATTERNS)
    if document_type == 'national_id_front':
        return _matches_any(raw_text, _NATIONAL_ID_PATTERNS)
    if document_type == 'national_id_back':
        if _matches_any(raw_text, _NATIONAL_ID_PATTERNS) or _matches_any(raw_text, _NATIONAL_ID_BACK_EXTRA_PATTERNS):
            return True
        # PhilSys back scans often yield only QR/blood_type/date_issued/PSN without full headers.
        return any(_has_value(extracted_data.get(k)) for k in ('blood_type', 'date_issued', 'philsys_number'))
    if document_type == 'tesda_nc2':
        if _matches_any(raw_text, _TESDA_PATTERNS):
            return True
        return _has_value(extracted_data.get('certificate_number')) and _has_value(extracted_data.get('qualification_title'))
    return True


def _extract_ai_screening_hints(extracted_data: dict) -> tuple[str | None, str | None]:
    stored_screening = extracted_data.get(SCREENING_KEY) if isinstance(extracted_data.get(SCREENING_KEY), dict) else {}
    raw_detected = (
        extracted_data.get('detected_document_type')
        or stored_screening.get('ai_detected_document_type')
    )
    detected = None
    if isinstance(raw_detected, str):
        norm = raw_detected.strip().lower().replace(' ', '_').replace('-', '_')
        if norm in VALID_DETECTED_TYPES:
            detected = norm

    raw_suspicion = (
        extracted_data.get('suspicion_reason')
        or stored_screening.get('ai_suspicion_reason')
    )
    suspicion = raw_suspicion.strip() if isinstance(raw_suspicion, str) and _has_value(raw_suspicion) else None
    return detected, suspicion


def _is_conflicting_ai_type(expected_type: str, ai_detected: str | None) -> bool:
    if not ai_detected:
        return False
    if ai_detected == expected_type:
        return False
    # Front and back of National ID are closely related; do not hard-fail back if AI labels front.
    if expected_type == 'national_id_back' and ai_detected == 'national_id_front':
        return False
    return True


def _format_expiry_human(valid_until_str: str | None) -> str:
    if not valid_until_str:
        return ''
    try:
        parsed = date.fromisoformat(str(valid_until_str)[:10])
        return parsed.strftime('%b %d, %Y')
    except ValueError:
        return str(valid_until_str)


def _clearance_missing_official_structure(document_type: str, raw_text: str, extracted_data: dict) -> str | None:
    """Detect fabricated NBI/Police clearance stubs lacking core official fields."""
    if document_type not in ('nbi_clearance', 'police_clearance'):
        return None
    has_doc_num = (
        any(_has_value(extracted_data.get(k)) for k in NUMBER_KEYS)
        or bool(_PLAUSIBLE_NUMBER_IN_TEXT.search(raw_text))
    )
    has_dates = (
        _has_value(extracted_data.get('date_issued'))
        or _has_value(extracted_data.get('valid_until'))
        or bool(_PLAUSIBLE_DATE_IN_TEXT.search(raw_text))
    )
    strong_patterns = _NBI_STRONG_AGENCY_PATTERNS if document_type == 'nbi_clearance' else _POLICE_STRONG_AGENCY_PATTERNS
    has_strong_agency = _matches_any(raw_text, strong_patterns)

    if not has_doc_num and not has_dates:
        return (
            f"Missing required official {DOC_DISPLAY_NAMES[document_type]} details "
            "(no clearance number or issue/validity dates found)."
        )
    if not has_strong_agency and not has_doc_num:
        return (
            f"Missing official agency header and clearance number expected on a valid "
            f"{DOC_DISPLAY_NAMES[document_type]}."
        )
    return None


def evaluate_document_screening(
    *,
    document_type: str,
    extracted_data: dict | None,
    raw_text: str | None,
    discrepancies: list[dict] | None = None,
    valid_until: date | str | None = None,
    today: date | None = None,
) -> tuple[dict, list[dict]]:
    """Evaluate 4-point document authenticity/quality screening.

    Returns (screening_summary, additional_discrepancies).
    """
    if document_type == 'profile_photo' or not document_type:
        return {}, []

    data = dict(extracted_data or {})
    text = (raw_text or '').strip()
    existing_discs = list(discrepancies or [])
    ref_today = today or date.today()
    doc_label = DOC_DISPLAY_NAMES.get(document_type, document_type.replace('_', ' ').title())

    ai_detected, ai_suspicion = _extract_ai_screening_hints(data)
    rule_types = _detect_rule_based_types(text) if text else []
    other_rule_types = [
        t for t in rule_types
        if t != document_type and not (document_type == 'national_id_back' and t == 'national_id_front')
    ]

    added_discrepancies: list[dict] = []
    blocking_reasons: list[str] = []

    # --- 1. Image Clarity ---
    existing_quality_flag = next((d for d in existing_discs if d.get('field') == 'image_quality'), None)
    alnum_count = sum(1 for ch in text if ch.isalnum())
    has_any_extracted_field = any(
        _has_value(data.get(k))
        for k in (*NAME_FIELDS, 'full_name', 'name', 'date_of_birth', *NUMBER_KEYS, 'blood_type', 'date_issued', 'valid_until')
    )

    image_clarity = 'pass'
    if existing_quality_flag is not None or ai_detected == 'unreadable':
        image_clarity = 'fail'
    elif raw_text is not None and alnum_count < 10 and not has_any_extracted_field and document_type != 'national_id_back':
        image_clarity = 'fail'

    if image_clarity == 'fail':
        quality_msg = (
            existing_quality_flag.get('message')
            if existing_quality_flag and existing_quality_flag.get('message')
            else (
                'Photo is too blurry or unreadable — no usable text could be extracted.'
                if alnum_count < 10
                else 'Photo is too blurry or unreadable — no usable identity details were detected.'
            )
        )
        if existing_quality_flag is None:
            added_discrepancies.append({
                'field': 'image_quality',
                'severity': 'high',
                'message': quality_msg,
            })
        blocking_reasons.append(quality_msg)

    # --- 2. Document Type Match & Integrity (Layer 1 + Layer 2) ---
    document_type_match = 'pass'
    detected_type_out: str | None = ai_detected or (rule_types[0] if rule_types else None)
    suspicious_flag = False
    wrong_doc_flag = False

    if image_clarity == 'fail':
        # When an image has unreadable/empty text, keep clarity as primary failure unless an unrelated cue exists.
        if ai_detected == 'unrelated' or (text and _UNRELATED_COMMON_PATTERN.search(text)):
            document_type_match = 'fail'
            wrong_doc_flag = True
            detected_type_out = 'unrelated'
    else:
        # Check fake/sample watermark words first
        fake_match = _FAKE_TEMPLATE_PATTERN.search(text) if text else None
        if fake_match:
            document_type_match = 'fail'
            suspicious_flag = True
            msg = (
                f"Suspicious or sample document detected (contains '{fake_match.group(0).upper()}'). "
                f"Please verify authenticity before approval."
            )
            added_discrepancies.append({
                'field': 'document_integrity',
                'severity': 'critical',
                'message': msg,
            })
            blocking_reasons.append(msg)

        # Check wrong-slot swap (e.g., Police Clearance uploaded into NBI Clearance slot)
        has_reviewer_corrections = bool(isinstance(data.get(CORRECTIONS_KEY), dict) and data.get(CORRECTIONS_KEY))
        expected_matched = (
            True if (has_reviewer_corrections and not other_rule_types)
            else (_is_expected_type_matched(document_type, text, data) if text or data else False)
        )

        if not fake_match and other_rule_types and not expected_matched:
            other_label = DOC_DISPLAY_NAMES.get(other_rule_types[0], other_rule_types[0])
            detected_type_out = other_rule_types[0]
            document_type_match = 'fail'
            wrong_doc_flag = True
            msg = f"Wrong document detected: this appears to be a {other_label}, not a valid {doc_label}."
            added_discrepancies.append({
                'field': 'document_type_mismatch',
                'severity': 'critical',
                'document_value': other_label,
                'profile_value': doc_label,
                'message': msg,
            })
            blocking_reasons.append(msg)
        elif not fake_match and _is_conflicting_ai_type(document_type, ai_detected) and not expected_matched:
            detected_type_out = ai_detected
            document_type_match = 'fail'
            if ai_detected == 'unrelated':
                wrong_doc_flag = True
                msg = f"Wrong document detected: this image does not appear to be a valid {doc_label}."
            else:
                wrong_doc_flag = True
                other_label = DOC_DISPLAY_NAMES.get(ai_detected or '', str(ai_detected))
                msg = f"Wrong document detected: this appears to be a {other_label}, not a valid {doc_label}."
            added_discrepancies.append({
                'field': 'document_type_mismatch',
                'severity': 'critical',
                'document_value': ai_detected,
                'profile_value': doc_label,
                'message': msg,
            })
            blocking_reasons.append(msg)
        elif not fake_match and raw_text is not None and text and not expected_matched:
            # Readable text present, yet zero official headers/anchors for the expected Philippine document!
            document_type_match = 'fail'
            wrong_doc_flag = True
            detected_type_out = 'unrelated'
            unrelated_hint = _UNRELATED_COMMON_PATTERN.search(text)
            if unrelated_hint:
                msg = (
                    f"Wrong document detected: this appears to be an unrelated document "
                    f"('{unrelated_hint.group(0)}'), not a valid {doc_label}."
                )
            else:
                msg = (
                    f"Wrong document detected: this image does not contain official {doc_label} "
                    f"headers or identifiers."
                )
            added_discrepancies.append({
                'field': 'document_type_mismatch',
                'severity': 'critical',
                'document_value': 'Unrelated / Unrecognized',
                'profile_value': doc_label,
                'message': msg,
            })
            blocking_reasons.append(msg)
        elif not fake_match and expected_matched and raw_text is not None and text:
            # Expected header found — verify clearance structural completeness (catches fabricated stubs)
            structural_issue = _clearance_missing_official_structure(document_type, text, data)
            if structural_issue:
                document_type_match = 'fail'
                suspicious_flag = True
                added_discrepancies.append({
                    'field': 'document_integrity',
                    'severity': 'critical',
                    'message': structural_issue,
                })
                blocking_reasons.append(structural_issue)
            elif ai_suspicion:
                document_type_match = 'warn'
                suspicious_flag = True
                msg = f"Automated screening flagged potential issue: {ai_suspicion}"
                added_discrepancies.append({
                    'field': 'document_integrity',
                    'severity': 'high',
                    'message': msg,
                })
                blocking_reasons.append(msg)

        if not detected_type_out and expected_matched:
            detected_type_out = document_type

    # --- 3. Expiration Check ---
    not_expired = 'na'
    expired_date_str = None
    if document_type not in ('national_id_front', 'national_id_back'):
        exp_raw = valid_until or data.get('valid_until')
        exp_date_obj = None
        if isinstance(exp_raw, date):
            exp_date_obj = exp_raw
            expired_date_str = exp_raw.isoformat()
        elif _has_value(exp_raw):
            try:
                exp_date_obj = date.fromisoformat(str(exp_raw)[:10])
                expired_date_str = exp_date_obj.isoformat()
            except ValueError:
                exp_date_obj = None

        if exp_date_obj is not None:
            if exp_date_obj < ref_today:
                not_expired = 'fail'
                exp_msg = f"Document is EXPIRED (expired on {expired_date_str})"
                if not any(d.get('field') == 'valid_until' for d in existing_discs):
                    added_discrepancies.append({
                        'field': 'valid_until',
                        'document_value': expired_date_str,
                        'similarity': 0.0,
                        'severity': 'critical',
                        'message': exp_msg,
                    })
                blocking_reasons.append(exp_msg)
            else:
                not_expired = 'pass'
        elif any(d.get('field') == 'valid_until' for d in existing_discs):
            not_expired = 'fail'
            existing_exp = next(d for d in existing_discs if d.get('field') == 'valid_until')
            expired_date_str = existing_exp.get('document_value')
            blocking_reasons.append(existing_exp.get('message') or 'Document is expired.')

    # --- 4. Name Match ---
    all_discs = existing_discs + added_discrepancies
    name_discs = [d for d in all_discs if d.get('field') in (*NAME_FIELDS, 'full_name', 'name')]
    if document_type == 'national_id_back':
        name_match = 'na'
    elif image_clarity == 'fail' and not any(_has_value(data.get(k)) for k in (*NAME_FIELDS, 'full_name')):
        name_match = 'fail'
    elif any(d.get('severity') in ('high', 'critical') for d in name_discs):
        name_match = 'fail'
    elif name_discs:
        name_match = 'warn'
    elif any(_has_value(data.get(k)) for k in (*NAME_FIELDS, 'full_name')):
        name_match = 'pass'
    else:
        name_match = 'warn'

    # --- Primary Issue & User-Facing Plain-English Alert ---
    primary_issue: str | None = None
    user_alert: dict | None = None
    suggested_rejection: str | None = None

    if wrong_doc_flag:
        primary_issue = 'wrong_document'
        other_label = (
            DOC_DISPLAY_NAMES.get(detected_type_out, None)
            if detected_type_out and detected_type_out != document_type and detected_type_out not in ('unrelated', 'unreadable')
            else None
        )
        if other_label:
            user_msg = (
                f"Wrong document detected. This looks like a {other_label}, not a {doc_label}. "
                f"Please upload a valid {doc_label}."
            )
        else:
            user_msg = (
                f"Wrong document detected. This image does not look like a {doc_label}. "
                f"Please upload a valid {doc_label}."
            )
        user_alert = {
            'level': 'error',
            'code': 'wrong_document',
            'title': 'Wrong document detected',
            'message': user_msg,
            'action_label': 'Re-upload Document',
        }
        suggested_rejection = blocking_reasons[0] if blocking_reasons else user_msg
    elif image_clarity == 'fail':
        primary_issue = 'unreadable'
        user_msg = (
            'Photo is too blurry or unreadable. Please take a clearer photo in good lighting.'
        )
        user_alert = {
            'level': 'error',
            'code': 'unreadable',
            'title': 'Photo is too blurry or unreadable',
            'message': user_msg,
            'action_label': 'Retake / Re-upload',
        }
        suggested_rejection = (
            f"Uploaded {doc_label} image is too blurry or unreadable. Please upload a clear, well-lit photo."
        )
    elif suspicious_flag:
        primary_issue = 'suspicious'
        user_msg = (
            f"Some required official details are missing or could not be verified on this {doc_label}. "
            f"Please upload a clear photo of the original document."
        )
        user_alert = {
            'level': 'error',
            'code': 'suspicious',
            'title': 'Document details incomplete or flagged',
            'message': user_msg,
            'action_label': 'Re-upload Document',
        }
        suggested_rejection = blocking_reasons[0] if blocking_reasons else user_msg
    elif not_expired == 'fail':
        primary_issue = 'expired'
        pretty_date = _format_expiry_human(expired_date_str)
        date_suffix = f" on {pretty_date}" if pretty_date else ''
        user_msg = f"This {doc_label} expired{date_suffix}. Please upload a renewed document."
        user_alert = {
            'level': 'warning',
            'code': 'expired',
            'title': 'Expired document detected',
            'message': user_msg,
            'action_label': 'Upload Renewed Document',
        }
        suggested_rejection = f"{doc_label} is expired (expired{date_suffix}). Please upload a renewed copy."
    elif name_match == 'fail':
        primary_issue = 'name_mismatch'

    has_blocking_flag = bool(wrong_doc_flag or image_clarity == 'fail' or suspicious_flag or not_expired == 'fail')

    summary = {
        'checks': {
            'document_type_match': document_type_match,
            'name_match': name_match,
            'not_expired': not_expired,
            'image_clarity': image_clarity,
        },
        'detected_document_type': detected_type_out,
        'ai_detected_document_type': ai_detected,
        'ai_suspicion_reason': ai_suspicion,
        'primary_issue': primary_issue,
        'has_blocking_flag': has_blocking_flag,
        'blocking_reasons': blocking_reasons,
        'suggested_rejection_reason': suggested_rejection,
        'user_alert': user_alert,
    }
    return summary, added_discrepancies


def screening_for_document(document) -> dict | None:
    """Return canonical screening summary for any tbl_documents instance (including legacy rows)."""
    if document is None or document.document_type == 'profile_photo':
        return None
    extracted = document.extracted_data if isinstance(document.extracted_data, dict) else {}
    has_ocr_evidence = bool(
        document.ocr_processed_at is not None
        or (document.ocr_raw_text or '').strip()
        or extracted
        or document.ocr_discrepancies
    )
    if not has_ocr_evidence:
        return None

    # Purged legacy media with no saved OCR text cannot be re-evaluated reliably.
    if getattr(document, 'media_purged_at', None) and not (document.ocr_raw_text or '').strip() and not extracted.get(SCREENING_KEY):
        return None

    raw_text = document.ocr_raw_text
    if raw_text is None and document.ocr_processed_at and not getattr(document, 'media_purged_at', None):
        raw_text = ''

    # Re-evaluate deterministically so existing Pending docs get Layer 1 rules automatically
    # while preserving Layer 2 AI hints stored under extracted_data['_screening'].
    summary, _ = evaluate_document_screening(
        document_type=document.document_type,
        extracted_data=extracted,
        raw_text=raw_text,
        discrepancies=document.ocr_discrepancies or [],
        valid_until=document.valid_until,
    )
    return summary


def document_has_blocking_screening_flag(document) -> bool:
    """True when an automated screening red/amber blocking flag is active on this document."""
    summary = screening_for_document(document)
    if summary is None:
        return False
    return bool(summary.get('has_blocking_flag'))


def document_has_stored_blocking_flag(document) -> bool:
    """True when our screening pipeline explicitly stored a blocking flag on this document."""
    if document is None or document.document_type == 'profile_photo':
        return False
    extracted = document.extracted_data if isinstance(document.extracted_data, dict) else {}
    stored = extracted.get(SCREENING_KEY)
    return bool(isinstance(stored, dict) and stored.get('has_blocking_flag'))
