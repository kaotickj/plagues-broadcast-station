"""Worker-safe validation of explicitly prepared words; no model or UI imports."""
import hashlib
import json


def prepared_text(request):
    sheet = request.get('supplied_sheet')
    if not isinstance(sheet, dict) or sheet.get('schema') != 'supplied-lyric-sheet-1':
        raise ValueError('Missing saved supplied-sheet provenance.')
    if request.get('mode') != 'align' or request.get('alignment_source') != 'supplied_sheet':
        raise ValueError('Prepared sung text is only for supplied alignment, never audio recognition.')
    if sheet.get('source_sha256') != request.get('source_sha256'):
        raise ValueError('Supplied sheet belongs to another recording.')
    expected = hashlib.sha256(json.dumps(sheet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if request.get('supplied_sheet_sha256') != expected:
        raise ValueError('Supplied sheet changed after the request was saved.')
    text = sheet.get('sung_text')
    if not isinstance(text, str) or not text.strip() or len(text) > 100000:
        raise ValueError('Supply nonempty sung text of at most 100,000 characters.')
    if text != request.get('reference_text') or sheet.get('original') != request.get('reference_original'):
        raise ValueError('Alignment text differs from the saved sung preview.')
    # Do not strip brackets again: user-visible preview is the exact input.
    return text
