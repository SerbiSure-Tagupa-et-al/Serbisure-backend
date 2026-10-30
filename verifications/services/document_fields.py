"""Keep reviewer-corrected document fields consistent across OCR and display."""
CORRECTIONS_KEY = '_reviewer_corrections'
NAME_FIELDS = ('first_name', 'middle_name', 'last_name')
NUMBER_KEYS = ('clearance_number', 'philsys_number', 'document_number', 'id_number')


def merge_document_fields(extracted, corrections, document_type):
    result = dict(extracted or {})
    result.update(corrections)
    if any(key in corrections for key in NAME_FIELDS):
        result['full_name'] = ' '.join(str(result.get(key) or '').strip() for key in NAME_FIELDS).strip()
        result['full_name'] = ' '.join(result['full_name'].split()) or None
    if 'document_number' in corrections:
        preferred = 'philsys_number' if document_type.startswith('national_id') else 'clearance_number'
        for key in set(NUMBER_KEYS).intersection(result) | {preferred, 'document_number'}:
            result[key] = corrections['document_number']
    return result
