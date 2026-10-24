"""Review selection and original-mix routing; no models or user files."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock,patch
import sys
import numpy as np
import pytest
from gnb_player.extraction_backend import lyrics_review as review
from gnb_player.extraction_backend.lyrics_quality import assess


def document(score=.3165305):
    words=[{'text':t,'start':i*.4+.1,'end':i*.4+.4,'score':s}
           for i,(t,s) in enumerate([('boyish',score),('comes',.873146),('drifting',.960532)])]
    return {'duration':3,'segments':[{'text':'boyish comes drifting','start':.1,'end':1.2,
            'words':words,'needs_review':False}], 'warnings':[],
            'metrics':{'recognition_confidence':.95,'low_confidence_fraction':.015},
            'pipeline':{'vocals_separated':False,'recognizer':'large-v3'}}


def test_observed_weak_word_is_reviewed_even_with_strong_song_average():
    data=document();before=deepcopy(data)
    assert review.suspect_cue(data,0)
    assert 'word_confidence' in {r['code'] for r in assess(data)['reasons']}
    assert data==before


def test_strong_words_do_not_schedule_extra_review():
    assert not review.suspect_cue(document(.99),0)


@pytest.mark.parametrize('existing_wav',[False,True])
def test_original_mix_review_never_separates_or_restores_stem(tmp_path,existing_wav):
    source=tmp_path/'original.mp3';source.write_bytes(b'source')
    wav=tmp_path/'recognition.wav'
    if existing_wav:wav.write_bytes(b'primary full mix')
    before=document();before['warnings']=['One passage has uncertain recognition.'];requests=[];decodes=[]
    def recognize(audio,sr,windows,engine,progress,evidence=None):
        requests.append((engine,windows))
        return {key:[] for key,_,_ in windows}
    forbidden=Mock(side_effect=AssertionError('Separator must remain disabled'))
    modules={'lyrics_pipeline':SimpleNamespace(decode=lambda s,t:decodes.append((s,t)),
               separate_vocals=forbidden,make_cues=Mock()),
             'lyrics_quality':SimpleNamespace(assess=assess),
             'lyrics_cache':SimpleNamespace(restore=forbidden,store=forbidden),
             'soundfile':SimpleNamespace(read=lambda *a,**kw:(np.ones(300,dtype=np.float32)*.1,100))}
    with patch.dict(sys.modules,modules),patch.object(review,'recognize',side_effect=recognize):
        after=review.review(source,tmp_path,before,lambda *a:None,preserve_text=True)
    assert decodes==([] if existing_wav else [(source,wav)])
    forbidden.assert_not_called()
    assert requests[0][0]=='medium.en'
    assert any(engine=='large-v3' and any(key==0 for key,_,_ in windows) for engine,windows in requests)
    assert after['segments']==before['segments'] and after['machine_review']['held_passages']
    assert after['warnings']==before['warnings']
    assert not after['machine_review']['repairs']
    assert not (tmp_path/'review-vocals.wav').exists()
