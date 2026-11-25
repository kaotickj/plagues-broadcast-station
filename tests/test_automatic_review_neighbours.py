"""Automatic corrections must not damage adjacent original/reviewed timing."""
from copy import deepcopy
import sys
from types import SimpleNamespace
import pytest
from gnb_player.extraction_backend import lyrics_review as review


def cue(text, start, end, score=.95, suspect=False):
    return {'text':text,'start':start,'end':end,'needs_review':suspect,
            'words':[{'text':text,'start':start,'end':end,'score':score}]}


@pytest.fixture(autouse=True)
def backend_alias(monkeypatch):
    # These cases exercise edits, not gap recovery or inference.
    monkeypatch.setitem(sys.modules,'lyrics_pipeline',SimpleNamespace(make_cues=lambda *_: (_ for _ in ()).throw(AssertionError('Unexpected gap recovery'))))


@pytest.mark.parametrize('side',['left','right'])
def test_high_confidence_same_word_cannot_cross_untouched_neighbour(side):
    original={'duration':9,'segments':[cue('left',1,2),cue('middle',3,4,suspect=True),cue('right',5,6)]}
    before=deepcopy(original)
    proposed=cue('middle',1.9,3.9) if side=='left' else cue('middle',3.2,5.1)
    result=review.reconcile(original,proposed['words'],{},lambda *_:1)
    assert result['segments']==before['segments']
    assert result['machine_review']['held_passages']
    assert not result['machine_review']['repairs']
    assert original==before


def test_second_edit_is_bounded_by_already_reviewed_predecessor():
    original={'duration':10,'segments':[cue('first',1,2,suspect=True),cue('second',3,4,suspect=True),cue('last',6,7)]}
    evidence=[*cue('first',1.2,2.8)['words'],*cue('second',2.5,4.1)['words']]
    result=review.reconcile(original,evidence,{},lambda *_:1)
    assert result['segments'][0]['end']==2.8
    assert result['segments'][1]['start']==3
    assert result['segments'][0]['end']<=result['segments'][1]['start']
    assert len(result['machine_review']['repairs'])==1
    assert result['machine_review']['held_passages']


def test_boundary_touching_correction_is_accepted_without_clipping():
    original={'duration':9,'segments':[cue('left',1,2),cue('middle',3,4,suspect=True),cue('right',5,6)]}
    replacement=cue('middle',2,5)
    result=review.reconcile(original,replacement['words'],{},lambda *_:1)
    assert result['segments'][1]['start']==2
    assert result['segments'][1]['end']==5
    assert result['machine_review']['repairs']


def test_neighbour_word_bounds_cannot_be_hidden_by_shorter_cue_bounds():
    left=cue('left',1,2);left['words'][0]['end']=2.8
    right=cue('right',5,6);right['words'][0]['start']=4.6
    assert not review.safe_review_edit([cue('middle',2.4,4)],9,left,right)
    assert not review.safe_review_edit([cue('middle',3,4.8)],9,left,right)


@pytest.mark.parametrize('proposal',[cue('a',3,3),cue('a',3,5.1),cue('different',3,4)])
def test_invalid_whole_proposal_never_gets_clipped_or_text_rewritten(proposal):
    if proposal['text']=='different':proposal['words'][0]['text']='a'
    before=deepcopy(proposal)
    assert not review.safe_review_edit([proposal],9,cue('left',1,2),cue('right',5,6))
    assert proposal==before
