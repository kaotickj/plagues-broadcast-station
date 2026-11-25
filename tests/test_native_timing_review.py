"""Regression for zero-duration model words becoming an approved subtitle line."""
import copy,json
import pytest
from gnb_player.lyrics import LyricsDocument, parse_lrc
from gnb_player.lyric_review import approve_document,is_approved,unresolved_holds
from gnb_player.review_core import review_provenance_label,review_markers

def native(word):
    return {'duration':60,'source_sha256':'a'*64,
            'provenance':{'mode':'align','alignment_source':'supplied'},
            'segments':[{'start':25.6,'end':27.36,'text':'own','words':[word]}]}

def test_all_zero_words_cannot_silently_become_approved_line_timing():
    # Saved Qwen prediction: own26.88–26.88. No replacement duration is guessed.
    raw=native({'text':'own','start':26.88,'end':26.88});original=copy.deepcopy(raw)
    doc=LyricsDocument.from_dict(raw)
    assert raw==original and doc.segments[0].text=='own'
    assert doc.active_word(26.88) is None and not doc.segments[0].words
    issue=doc.segments[0].metadata['native_timing_issues'][0]
    assert issue['raw_word']==raw['segments'][0]['words'][0]
    assert issue['replacement_interval'] is None and unresolved_holds(doc)
    with pytest.raises(ValueError,match='Unresolved native'):approve_document(doc,'a'*64)
    assert not is_approved(doc)

def test_native_evidence_survives_round_trip_without_duplication():
    doc=LyricsDocument.from_dict(native({'text':'own','start':26.88,'end':26.88}))
    saved=doc.to_dict();restored=LyricsDocument.from_dict(json.loads(json.dumps(saved,allow_nan=False)))
    assert restored.to_dict()==saved and unresolved_holds(restored)
    assert any('Unresolved native' in label for _,label in review_markers(restored))
    assert '1 unresolved' in review_provenance_label(restored)

@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,None,True])
def test_invalid_numeric_evidence_is_serializable_and_never_a_timestamp(value):
    doc=LyricsDocument.from_dict(native({'text':'own','start':value,'end':26.88}))
    json.dumps(doc.to_dict(),allow_nan=False)
    assert unresolved_holds(doc) and doc.active_word(26) is None
    with pytest.raises(ValueError):approve_document(doc,'a'*64)

def test_genuine_line_only_subtitle_remains_supported():
    doc=parse_lrc('[00:25.60]ordinary subtitle\n[00:27.36]next line',duration=60)
    doc.source_sha256='a'*64
    assert not unresolved_holds(doc) and is_approved(approve_document(doc,'a'*64))

def test_valid_grouped_support_survives_unchanged():
    word={'text':'own','start':25.6,'end':27.36,'grouped_text':True,'highlight_intervals':[[25.6,26],[27,27.36]]}
    doc=LyricsDocument.from_dict(native(word))
    assert doc.to_dict()['segments'][0]['words'][0]==word
    assert doc.active_word(26.5) is None and not unresolved_holds(doc)

@pytest.mark.parametrize('mode,label',[('align','this job did not recognize'),('extract','model transcription'),(None,'unrecorded origin')])
def test_text_provenance_survives_reload_without_approval(mode,label):
    data=native({'text':'own','start':26,'end':26.5});data['provenance']['mode']=mode
    doc=LyricsDocument.from_dict(data);restored=LyricsDocument.from_dict(doc.to_dict())
    assert label in review_provenance_label(restored) and not is_approved(restored)
