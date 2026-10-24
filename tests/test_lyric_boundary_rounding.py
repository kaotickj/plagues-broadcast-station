"""Generated Murkwater boundary regression; fixtures only, no models/audio."""
from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import pytest

from gnb_player.lyrics import LyricsDocument, fingerprint, load_lyrics
from gnb_player.lyric_review import edit_document, document_revision, save_review_document, is_approved
from gnb_player.lyric_timing import normalize_generated_timing, normalize_rounded_boundaries


def generated():
    segments = [{'start': i * 5., 'end': i * 5. + 4., 'text': f'Fixture {i}', 'words': []}
                for i in range(5)]
    segments += [
        {'start': 41.722, 'end': 46.822, 'text': 'Earlier line', 'needs_review': True,
         'words': [{'start': 41.722, 'end': 46.822, 'text': 'Earlier line', 'score': .4, 'uncertain': True}]},
        {'start': 46.820, 'end': 49.960, 'text': 'Following line', 'needs_review': False,
         'words': [{'start': 46.820, 'end': 49.960, 'text': 'Following line', 'score': .98, 'uncertain': True}]}]
    return {'duration': 231.967375, 'segments': segments, 'reviewed': False,
            'quality': {'status': 'review'}, 'machine_review': {'notice': 'Not human approval'},
            'recognition_evidence': {'after_review': {'segments': deepcopy(segments)}}}


def test_exact_generated_rows_normalize_only_next_start_and_word_without_touching_raw_evidence():
    data = generated(); before = deepcopy(data)
    corrections = normalize_generated_timing(data)
    assert corrections == [{'line': 7, 'previous_line': 6, 'old_start': 46.820, 'start': 46.822,
                            'reason': 'centisecond-start/millisecond-end rounding'}]
    assert data['segments'][:6] == before['segments'][:6]
    expected = deepcopy(before['segments'][6]); expected['start'] = 46.822; expected['words'][0]['start'] = 46.822
    assert data['segments'][6] == expected
    for key in ('recognition_evidence', 'quality', 'machine_review', 'reviewed'):
        assert data[key] == before[key]
    assert normalize_generated_timing(data) == []
    assert len(data['timing_boundary_adjustments']) == 1


@pytest.mark.parametrize('start', [46.81, 46.818, 46.821])
def test_genuine_or_non_rounding_overlap_is_rejected_with_line_and_times(start):
    data = generated(); data['segments'][6]['start'] = start
    before = deepcopy(data)
    with pytest.raises(ValueError, match=r'Line 7 starts at .* before line 6 ends at 46.822s'):
        normalize_generated_timing(data)
    assert data == before
    doc = LyricsDocument.from_dict(data)
    with pytest.raises(ValueError, match=r'Line 7 starts at'):
        edit_document(doc, [(c.start, c.end, c.text) for c in doc.segments])


def test_rounding_does_not_collapse_words_or_reorder_invalid_lines():
    data = generated(); data['segments'][6]['words'][0]['end'] = 46.821
    before = deepcopy(data['segments'])
    normalized, changes = normalize_rounded_boundaries(before)
    assert normalized == before and not changes
    with pytest.raises(ValueError, match='Line 7 starts at'):
        normalize_generated_timing(data)
    data = generated(); data['segments'][6]['start'] = 40
    with pytest.raises(ValueError, match='Line 7 starts at'):
        normalize_generated_timing(data)


def test_legacy_generated_draft_can_edit_save_reopen_with_words_and_review_preserved(tmp_path):
    source = tmp_path / 'fixture.wav'; source.write_bytes(b'isolated recording identity')
    data = generated(); data['source_sha256'] = fingerprint(source)
    target = tmp_path / 'lyrics.json'; target.write_text(json.dumps(data))
    doc = load_lyrics(target); original = target.read_bytes()
    rows = [(c.start, c.end, c.text) for c in doc.segments]
    rows[0] = (rows[0][0], rows[0][1], '雨 — corrected locally')
    edited = edit_document(doc, rows)
    assert target.read_bytes() == original and doc.segments[6].start == 46.820
    saved = save_review_document(edited, target, source, document_revision(doc))
    restored = load_lyrics(target)
    assert restored.to_dict() == saved.to_dict()
    assert restored.segments[0].text == '雨 — corrected locally'
    assert restored.segments[6].start == restored.segments[6].words[0].start == 46.822
    assert restored.segments[5] == doc.segments[5]
    assert restored.segments[6].metadata == doc.segments[6].metadata
    assert restored.segments[6].words[0].metadata == doc.segments[6].words[0].metadata
    assert restored.metadata['recognition_evidence'] == data['recognition_evidence']
    assert restored.metadata['machine_review'] == data['machine_review']
    assert restored.human_review['status'] == 'draft' and not is_approved(restored)
    assert original in [p.read_bytes() for p in (tmp_path / 'history').glob('*.json')]


def test_edited_text_never_inherits_original_word_labels():
    doc = LyricsDocument.from_dict(generated())
    rows = [(c.start, c.end, c.text) for c in doc.segments]
    rows[6] = (rows[6][0], rows[6][1], 'My corrected words')
    edited = edit_document(doc, rows)
    assert edited.segments[6].text == 'My corrected words' and not edited.segments[6].words
    assert 6 in edited.human_review['alignment_needed']


@pytest.mark.parametrize('genuine_overlap', [False, True])
def test_worker_normalizes_after_review_before_commit_or_preserves_prior_and_raw_failure(tmp_path, monkeypatch, genuine_overlap):
    from gnb_player import extract_worker as worker
    from gnb_player.extraction import ExtractionConfig
    source=tmp_path/'recording.wav';source.write_bytes(b'fixture source')
    target=tmp_path/'lyrics.json';target.write_text('{"protected":"prior revision"}')
    original=target.read_bytes();stat=source.stat();reviewed=generated()
    if genuine_overlap:reviewed['segments'][6]['start']=46.810
    raw=deepcopy(reviewed)
    modules={
        'lyrics_acceleration':SimpleNamespace(reset_gpu_timings=lambda:None,gpu_timings=lambda:[]),
        'lyrics_pipeline':SimpleNamespace(extract=lambda *a,**kw:generated()),
        'lyrics_review':SimpleNamespace(review=lambda *a,**kw:deepcopy(reviewed)),
        'lyrics_quality':SimpleNamespace(assess=lambda data:{'status':'review'}),
        'lyrics_recognition':SimpleNamespace(transcript_snapshot=lambda data:{'segments':deepcopy(data['segments'])})}
    for key,value in modules.items():monkeypatch.setitem(sys.modules,key,value)
    monkeypatch.setattr(worker,'validate_source',lambda *a:(stat.st_size,stat.st_mtime_ns,fingerprint(source),231.967375))
    monkeypatch.setattr(worker,'require_runtime',lambda *a:None)
    monkeypatch.setattr(worker,'prepare_environment',lambda *a:None)
    config=ExtractionConfig(cache_dir=str(tmp_path/'cache'),auto_review=True)
    if genuine_overlap:
        with pytest.raises(ValueError,match=r'Line 7 starts at .* Existing lyrics were kept. Diagnostics:'):
            worker.run(source,target,config)
        assert target.read_bytes()==original
        failures=list((tmp_path/'cache'/'failures').glob('*.json'))
        assert len(failures)==1
        failure=json.loads(failures[0].read_text())
        assert failure['recognition_evidence']['after_review']['segments']==raw['segments']
        assert not (tmp_path/'history').exists()
    else:
        worker.run(source,target,config)
        saved=json.loads(target.read_text())
        assert saved['segments'][6]['start']==saved['segments'][6]['words'][0]['start']==46.822
        assert saved['recognition_evidence']['after_review']['segments']==raw['segments']
        assert saved['quality']['status']=='review' and not saved['reviewed']
        assert original in [p.read_bytes() for p in (tmp_path/'history').glob('*.json')]
    assert source.read_bytes()==b'fixture source'
