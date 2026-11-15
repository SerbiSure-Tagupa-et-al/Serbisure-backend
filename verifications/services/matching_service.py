import re
from datetime import date
from difflib import SequenceMatcher

from .document_fields import CORRECTIONS_KEY, NAME_FIELDS, NUMBER_KEYS


_NAME_LABEL_TEXT = {
    "first_name": r"(?:first|given) name(?:s|\(s\))?(?:\s*/\s*mga pangalan)?|mga pangalan(?:\s*/\s*(?:first|given) name(?:s|\(s\))?)?",
    "middle_name": r"middle names?(?:\s*/\s*gitnang apelyido)?|gitnang apelyido(?:\s*/\s*middle names?)?",
    "last_name": r"(?:last name|surname)(?:\s*/\s*apelyido)?|apelyido(?:\s*/\s*(?:last name|surname))?",
}
_NAME_LABELS = {
    field: re.compile(rf"^(?:{label})(?:\s*:\s*|\s+|$)(.*)$", re.IGNORECASE)
    for field, label in _NAME_LABEL_TEXT.items()
}
_OTHER_LABEL = re.compile(r"^(?:date of birth|birth ?date|petsa|address|tirahan|sex|document|philsys)\b", re.IGNORECASE)
_PRINTED_NAME = re.compile(r"[^\W\d_]+(?:[ .’'-]+[^\W\d_]+)*\.?", re.UNICODE)
_MISSING_VALUES = {"n/a", "none", "null", "not detected", "not detected in ocr", "not available", "unknown", "unreadable"}

def normalize_string(val: str | None) -> str:
    """Normalize string by uppercasing, trimming, and stripping punctuation."""
    if not val:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9\s]", "", str(val))
    return " ".join(cleaned.upper().split())


def normalize_document_names(extracted_data: dict, raw_text: str | None) -> dict:
    """Use printed name fields when available, leaving reviewer corrections intact."""
    if any(field in (extracted_data.get(CORRECTIONS_KEY) or {}) for field in NAME_FIELDS):
        return extracted_data
    lines = [" ".join(line.split()) for line in (raw_text or "").splitlines() if line.strip()]
    labeled = {}
    for index, line in enumerate(lines):
        for field, pattern in _NAME_LABELS.items():
            match = pattern.fullmatch(line)
            if not match:
                continue
            value = match[1] or (lines[index + 1] if index + 1 < len(lines) else "")
            if (_PRINTED_NAME.fullmatch(value)
                    and value.casefold() not in _MISSING_VALUES
                    and not _OTHER_LABEL.match(value)
                    and not any(label.fullmatch(value) for label in _NAME_LABELS.values())):
                labeled[field] = value
            break

    if "first_name" in labeled and "last_name" in labeled:
        result = {**extracted_data, **labeled}
        if any(result.get(field) != extracted_data.get(field) for field in NAME_FIELDS) or not result.get("full_name"):
            result["full_name"] = " ".join(str(result[field]).strip() for field in NAME_FIELDS if result.get(field))
        return result

    first, last, full = (normalize_string(extracted_data.get(field)) for field in ("first_name", "last_name", "full_name"))
    # An OCR surname followed by more name words may be the second given name.
    if first and last and full.startswith(f"{first} {last} ") and not full.endswith(f" {last}"):
        return {**extracted_data, **dict.fromkeys(NAME_FIELDS)}
    return extracted_data


def missing_identity_warning(extracted_data: dict, document_type: str, raw_text: str | None) -> dict | None:
    """Flag completed OCR with no usable identity fields for manual review."""
    if raw_text is None or document_type == "profile_photo":
        return None
    fields = (*NAME_FIELDS, "full_name", "name", "date_of_birth", *NUMBER_KEYS)
    if document_type == "national_id_back":
        fields += ("blood_type", "date_issued")
    for field in fields:
        value = extracted_data.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text.casefold() not in _MISSING_VALUES and any(char.isalnum() for char in text):
                return None
    return {
        "field": "image_quality",
        "severity": "high",
        "message": (
            "No readable ID details detected by OCR — please check if the uploaded image is valid."
            if any(char.isalnum() for char in raw_text)
            else "No readable text detected by OCR — please check if the uploaded image is valid."
        ),
    }


def calculate_similarity(s1: str | None, s2: str | None) -> float:
    """Compute normalized Levenshtein-like similarity ratio (0.0 to 1.0)."""
    norm1 = normalize_string(s1)
    norm2 = normalize_string(s2)

    if not norm1 and not norm2:
        return 1.0
    if not norm1 or not norm2:
        return 0.0

    # Exact match after normalization
    if norm1 == norm2:
        return 1.0

    # Handle nickname / substring matches (e.g., "MA. THERESA" vs "THERESA")
    if norm1 in norm2 or norm2 in norm1:
        shorter = min(len(norm1), len(norm2))
        longer = max(len(norm1), len(norm2))
        if longer > 0:
            return max(0.85, shorter / longer)

    return SequenceMatcher(None, norm1, norm2).ratio()


def detect_discrepancies(
    extracted_data: dict, user_profile, *, document_type: str = "", raw_text: str | None = None,
) -> tuple[float | None, list[dict]]:
    """
    Compare extracted document data against user_profile.
    Returns:
        (ocr_match_score: percentage from 0 to 100, or None if no comparable fields, discrepancies)
    """
    warning = missing_identity_warning(extracted_data, document_type, raw_text)
    discrepancies = [warning] if warning else []
    field_scores: list[float] = []

    # 1. Compare First Name
    doc_first = extracted_data.get("first_name")
    prof_first = getattr(user_profile, "first_name", "")
    if doc_first and prof_first:
        sim = calculate_similarity(prof_first, doc_first)
        field_scores.append(sim)
        if sim < 0.80:
            discrepancies.append({
                "field": "first_name",
                "profile_value": prof_first,
                "document_value": doc_first,
                "similarity": round(sim, 2),
                "severity": "high" if sim < 0.50 else "medium",
                "message": f"First name mismatch: Account '{prof_first}' vs Document '{doc_first}'"
            })
    elif prof_first and not doc_first:
        # Full name fallback if first_name wasn't split by LLM
        doc_full = extracted_data.get("full_name")
        if doc_full:
            sim = calculate_similarity(prof_first, doc_full)
            # If profile first name is contained in the full name
            if normalize_string(prof_first) in normalize_string(doc_full):
                field_scores.append(0.95)
            else:
                field_scores.append(sim)
                if sim < 0.70:
                    discrepancies.append({
                        "field": "first_name",
                        "profile_value": prof_first,
                        "document_value": doc_full,
                        "similarity": round(sim, 2),
                        "severity": "high",
                        "message": f"First name not found in document full name '{doc_full}'"
                    })

    # 2. Compare Last Name
    doc_last = extracted_data.get("last_name")
    prof_last = getattr(user_profile, "last_name", "")
    if doc_last and prof_last:
        sim = calculate_similarity(prof_last, doc_last)
        field_scores.append(sim)
        if sim < 0.80:
            discrepancies.append({
                "field": "last_name",
                "profile_value": prof_last,
                "document_value": doc_last,
                "similarity": round(sim, 2),
                "severity": "high" if sim < 0.50 else "medium",
                "message": f"Last name mismatch: Account '{prof_last}' vs Document '{doc_last}'"
            })
    elif prof_last and not doc_last:
        doc_full = extracted_data.get("full_name")
        if doc_full:
            if normalize_string(prof_last) in normalize_string(doc_full):
                field_scores.append(0.95)
            else:
                sim = calculate_similarity(prof_last, doc_full)
                field_scores.append(sim)
                if sim < 0.70:
                    discrepancies.append({
                        "field": "last_name",
                        "profile_value": prof_last,
                        "document_value": doc_full,
                        "similarity": round(sim, 2),
                        "severity": "high",
                        "message": f"Last name not found in document full name '{doc_full}'"
                    })

    # 3. Compare Middle Name (if present on both)
    doc_middle = extracted_data.get("middle_name")
    prof_middle = getattr(user_profile, "middle_name", "")
    if doc_middle and prof_middle:
        sim = calculate_similarity(prof_middle, doc_middle)
        field_scores.append(sim)
        if sim < 0.75:
            discrepancies.append({
                "field": "middle_name",
                "profile_value": prof_middle,
                "document_value": doc_middle,
                "similarity": round(sim, 2),
                "severity": "low",
                "message": f"Middle name discrepancy: '{prof_middle}' vs '{doc_middle}'"
            })

    # 4. Compare Date of Birth
    doc_dob = extracted_data.get("date_of_birth")
    prof_dob = getattr(user_profile, "date_of_birth", None)
    if doc_dob and prof_dob:
        prof_dob_str = str(prof_dob)
        if prof_dob_str == str(doc_dob):
            field_scores.append(1.0)
        else:
            field_scores.append(0.0)
            discrepancies.append({
                "field": "date_of_birth",
                "profile_value": prof_dob_str,
                "document_value": str(doc_dob),
                "similarity": 0.0,
                "severity": "high",
                "message": f"Birthdate mismatch: Account '{prof_dob_str}' vs Document '{doc_dob}'"
            })

    # 5. Check Document Expiration
    doc_valid_until = extracted_data.get("valid_until")
    if doc_valid_until:
        try:
            exp_date = date.fromisoformat(str(doc_valid_until))
            if exp_date < date.today():
                discrepancies.append({
                    "field": "valid_until",
                    "document_value": str(doc_valid_until),
                    "similarity": 0.0,
                    "severity": "critical",
                    "message": f"Document is EXPIRED (expired on {doc_valid_until})"
                })
        except ValueError:
            pass

    # Canonical persisted score: percentage 0..100. Missing evidence is not 50%.
    if field_scores:
        avg_score = sum(field_scores) / len(field_scores)
    else:
        return None, discrepancies

    return round(avg_score * 100, 2), discrepancies
