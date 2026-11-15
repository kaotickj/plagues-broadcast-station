"""Exact, recording-scoped spelling suggestions. Never an ASR input or auto-edit."""
from __future__ import annotations

import copy
import hashlib
import json
import re

TRUSTED_SPELLING_KINDS = {'owner_accepted_full_song_reference', 'valid_local_approval'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def unresolved_suggestions(document):
    review = document.metadata.get('vocabulary_review', {})
    return [s for s in review.get('suggestions', []) if s.get('status') == 'pending']


def require_resolved_suggestions(document):
    if unresolved_suggestions(document):
        raise ValueError('Accept or reject the pending name suggestions before alignment or approval. '
                         'They have not changed the words or entered a model prompt.')


def stage_suggestions(document, profile):
    """Explicit profile import; exact known confusions only, no fuzzy/global list.

    A matching title is insufficient: the profile must name this recording's
    SHA-256 and cite an accepted lyric spelling. Catalog-only names are skipped.
    Original segments and recognition provenance are retained independently.
    """
    from .review_core import mark_unreviewed, now
    from .lyric_review import document_revision
    if not re.fullmatch(r'[a-f0-9]{64}', document.source_sha256 or ''):
        raise ValueError('Name suggestions need an exact recording hash.')
    require_resolved_suggestions(document)
    entries = profile.get('entries', [])
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError('Use a vocabulary profile with at most 500 entries.')
    prior = document.metadata.get('vocabulary_review', {})
    prior_decisions = {(s['cue_index'], s['match_text'], s['replacement'])
                       for s in prior.get('suggestions', []) if s.get('status') == 'rejected'}
    suggestions = []
    occupied = set()
    for entry in entries:
        source = entry.get('source', {})
        if (source.get('audio_sha256') != document.source_sha256 or
                source.get('kind') not in TRUSTED_SPELLING_KINDS):
            continue
        canonical = entry.get('canonical', '').strip()
        if not canonical or len(canonical) > 100:
            raise ValueError('A canonical name must contain 1–100 characters.')
        forms = entry.get('observed_confusions', [])
        if not isinstance(forms, list) or len(forms) > 100:
            raise ValueError('Use at most 100 observed confusions per name.')
        for confusion in forms:
            form = confusion.get('form', '').strip()
            if not form or len(form) > 100 or form.casefold() == canonical.casefold():
                continue
            pattern = re.compile(r'(?<!\w)' + re.escape(form) + r'(?!\w)', re.IGNORECASE)
            for index, cue in enumerate(document.segments):
                for match in pattern.finditer(cue.text):
                    key = (index, match.group(), canonical)
                    positions = {(index, n) for n in range(match.start(), match.end())}
                    if key in prior_decisions or positions & occupied:
                        continue
                    occupied.update(positions)
                    suggestions.append({'id': _digest([document.source_sha256, index, cue.text,
                                                        match.span(), canonical]),
                        'status': 'pending', 'cue_index': index, 'cue_text': cue.text,
                        'start_char': match.start(), 'end_char': match.end(),
                        'match_text': match.group(), 'replacement': canonical,
                        'spelling_source': copy.deepcopy(source),
                        'confusion_evidence': copy.deepcopy(confusion),
                        'note': 'Known spelling confusion on this recording; listen before accepting.'})
                    if len(suggestions) > 2000:
                        raise ValueError('Too many suggestions; review a smaller profile.')
    result = copy.deepcopy(document)
    if not suggestions:
        return result
    previous = copy.deepcopy(prior)
    state = {'schema': 'track_name_review_v1', 'source_sha256': document.source_sha256,
        'profile_sha256': _digest(profile), 'created_at': now(),
        'base_document_revision': document_revision(document),
        'original_segments': copy.deepcopy(document.to_dict()['segments']),
        'original_provenance': copy.deepcopy(document.metadata.get('provenance', {})),
        'suggestions': suggestions, 'decisions': [], 'automatic_vocabulary_injection': False}
    if previous:
        state['previous_review'] = previous
    result.metadata['vocabulary_review'] = state
    return mark_unreviewed(result, 'Name suggestions staged separately; accept or reject each before using this draft.')


def decide_suggestion(document, suggestion_id, *, accept):
    """One explicit decision; immutable input, honest timing, reversible history."""
    from .review_core import edit_phrase, mark_unreviewed, now
    from .lyric_review import document_revision
    state = document.metadata.get('vocabulary_review', {})
    if state.get('source_sha256') != document.source_sha256:
        raise ValueError('Name suggestion belongs to another recording.')
    matches = [s for s in state.get('suggestions', []) if s.get('id') == suggestion_id]
    if len(matches) != 1 or matches[0].get('status') != 'pending':
        raise ValueError('Select one pending name suggestion.')
    item = matches[0]
    before = document_revision(document)
    if accept:
        index = item['cue_index']
        if not 0 <= index < len(document.segments) or document.segments[index].text != item['cue_text']:
            raise ValueError('Phrase changed after this suggestion. Reject it and reload suggestions; no text was changed.')
        old = document.segments[index].text
        start, end = item['start_char'], item['end_char']
        if old[start:end] != item['match_text']:
            raise ValueError('Suggestion no longer matches this phrase.')
        new = old[:start] + item['replacement'] + old[end:]
        result = edit_phrase(document, index, new)
    else:
        result = copy.deepcopy(document)
    review = result.metadata['vocabulary_review']
    selected = next(s for s in review['suggestions'] if s['id'] == suggestion_id)
    selected.update(status='accepted' if accept else 'rejected', decided_at=now())
    if accept:
        delta = len(item['replacement']) - (end - start)
        for other in review['suggestions']:
            if other['status'] == 'pending' and other['cue_index'] == index and other['cue_text'] == old:
                if other['start_char'] >= end:
                    other['start_char'] += delta
                    other['end_char'] += delta
                other['cue_text'] = new
    review['decisions'].append({'suggestion_id': suggestion_id, 'decision': selected['status'],
        'at': selected['decided_at'], 'before_document_revision': before,
        'after_text_sha256': _digest([c.text for c in result.segments]),
        'corrected_text': item['replacement'] if accept else None,
        'timing': 'Existing anchors and highlight intervals retained; count changes use grouped support.'})
    return mark_unreviewed(result, 'Name correction accepted; listen before approving.' if accept else
                           'Name suggestion rejected; text and timing unchanged.')
