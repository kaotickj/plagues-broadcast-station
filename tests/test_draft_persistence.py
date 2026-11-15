"""The actual edited owner draft: save, approve, close, and reopen the station."""
import copy
import json
from pathlib import Path
import pytest
from PySide6.QtWidgets import QMessageBox
from test_qwen_review_flow import source
from test_draft_preview_preference import station, settle
from gnb_player.library import Track
from gnb_player.lyrics import LyricsDocument, fingerprint, load_lyrics
from gnb_player.review_core import ReviewStore, edit_phrase
from gnb_player.review_partial import partial_tokens, recover_stale_text_edit_mapping
from gnb_player.lyric_review import is_approved

EVIDENCE=Path(__file__).resolve().parents[2]/'evidence'


def raw_case(source):
    raw=json.loads((EVIDENCE/'owner-latest-saved-draft.json').read_text())
    raw['source_sha256']=fingerprint(source)
    return raw


def rendered(raw):
    return [(r['text'],r['start'],r['end'],r['words']) for r in raw['segments']]


def test_exact_owner_bug_is_reproduced_and_recoverable_without_timing_changes(source,tmp_path):
    raw=raw_case(source)
    with pytest.raises(ValueError,match='source token identity'):LyricsDocument.from_dict(raw)
    store=ReviewStore(tmp_path/'data',source);store.draft_path.write_text(json.dumps(raw))
    original=store.draft_path.read_bytes();doc=store.load()
    assert rendered(doc.to_dict())==rendered(raw)
    assert store.draft_path.read_bytes()==original and not is_approved(doc)
    assert doc.metadata['saved_draft_recoveries'][0]['cues'][0]['cue_index']==17
    store.save_draft(doc)
    assert rendered(ReviewStore(tmp_path/'data',source).load().to_dict())==rendered(raw)
    assert any(x.read_bytes()==original for x in (store.root/'history').iterdir())


@pytest.mark.parametrize('text',['He remembers pre-war summers.','He recalls those old summers.','She remembers pre-war summers.'])
def test_new_partial_text_edits_remain_readable_without_invented_word_times(source,tmp_path,text):
    raw=raw_case(source);bad=raw['segments'][17]
    before=next(e['before'] for e in raw['edits'] if e['after_text']=='He remembers pre-war summers.')
    row={**bad,**copy.deepcopy(before)}
    raw['segments']=[row];raw['edits']=[]
    doc=LyricsDocument.from_dict(raw);corrected=edit_phrase(doc,0,text)
    reloaded=LyricsDocument.from_dict(corrected.to_dict())
    assert reloaded.to_dict()==corrected.to_dict()
    assert corrected.segments[0].text==text
    if text=='He remembers pre-war summers.':
        assert [w.text for w in corrected.segments[0].words]==['remembers','pre-war','summers.']
        assert [(w.start,w.end) for w in corrected.segments[0].words]==[(w.start,w.end) for w in doc.segments[0].words]
        assert partial_tokens(corrected.segments[0])[0]==('He',None)
    else:
        group=corrected.segments[0].words[0]
        assert group.metadata['native_constituents']==[w.to_dict() for w in doc.segments[0].words]
        assert group.metadata['individual_word_boundaries'] is None
        assert 'partial_timing' not in corrected.segments[0].metadata
    store=ReviewStore(tmp_path/'data',source);store.save_draft(corrected)
    assert store.load().to_dict()==json.loads(json.dumps(corrected.to_dict()))


@pytest.mark.parametrize('change',['no_history','moved_time','missing_constituent','changed_interval'])
def test_recovery_does_not_relax_arbitrary_invalid_metadata(source,change):
    raw=raw_case(source);row=raw['segments'][17]
    if change=='no_history':raw['edits']=[]
    elif change=='moved_time':row['start']-=1
    elif change=='missing_constituent':row['words'][0]['native_constituents'].pop()
    else:row['words'][0]['highlight_intervals'][0][0]+=.1
    with pytest.raises(ValueError):recover_stale_text_edit_mapping(raw)


def test_unreadable_saved_draft_cannot_be_overwritten_by_empty_fallback(source,tmp_path):
    store=ReviewStore(tmp_path/'data',source);store.draft_path.write_text('{broken')
    before=store.draft_path.read_bytes()
    with pytest.raises(ValueError):store.load()
    empty=LyricsDocument([],5,source_sha256=store.digest)
    with pytest.raises(ValueError,match='could not be read'):store.save_draft(empty)
    assert store.draft_path.read_bytes()==before


def test_save_preflight_rejects_unreadable_partial_before_replacing_prior_file(source,tmp_path):
    store=ReviewStore(tmp_path/'data',source);good=LyricsDocument.from_dict(recover_stale_text_edit_mapping(raw_case(source)))
    store.save_draft(good);before=store.draft_path.read_bytes()
    corrupt=copy.deepcopy(good);corrupt.segments[2].metadata['partial_timing']['tokens'][0]['text']='BROKEN'
    with pytest.raises(ValueError):store.save_draft(corrupt)
    assert store.draft_path.read_bytes()==before


def test_owner_approval_survives_track_switch_and_full_station_restart(station,source,tmp_path,monkeypatch):
    from gnb_player.review_dialog import LyricsReviewDialog
    monkeypatch.setattr('gnb_player.review_dialog.embedded_lyrics',lambda _: {'kind':'no_embedded_lyrics','text':'','tags':{},'trusted':False})
    calls=[];monkeypatch.setattr(QMessageBox,'question',lambda *a:(calls.append(a[2]),QMessageBox.StandardButton.Ok)[1])
    w=station('roundtrip');raw=raw_case(source);track=Track(source,raw['title'],duration=raw['duration'])
    other=source.with_name('other.wav');other.write_bytes(source.read_bytes()+b'different');track2=Track(other,'Other track',duration=5)
    w.all_tracks=[track,track2];w._apply_queue();settle(w)
    store=ReviewStore(w.data_dir,source);store.draft_path.write_text(json.dumps(raw))
    d=LyricsReviewDialog(track,w.data_dir,w.extract_config.cache_dir,parent=w)
    d.approved.connect(lambda path,doc:w._lyrics_ready(track,path,doc))
    assert rendered(d.document.to_dict())==rendered(raw)
    d.confirm.setChecked(True);d.approve_button.click()
    assert calls==['Save and use these lyrics and timings as they are?']
    assert is_approved(d.document),d.status.text()
    target=d.store.output_target
    d.dirty=False;d.timer.stop();d.close()
    for row in (1,0):w.playlist.setCurrentRow(row);settle(w)
    assert rendered(w._find_document(track).to_dict())==rendered(raw)
    settle(w);assert w.close()
    reopened=station('roundtrip');reopened.playlist.setCurrentRow(0);settle(reopened)
    assert reopened.lyric_files[str(source)]==str(target)
    document=reopened._find_document(track)
    assert rendered(document.to_dict())==rendered(raw) and is_approved(document)
    again=LyricsReviewDialog(track,reopened.data_dir,reopened.extract_config.cache_dir,document,reopened)
    assert rendered(again.document.to_dict())==rendered(raw) and is_approved(again.document)
    again.dirty=False;again.timer.stop();again.close()
    assert not reopened.engine.playing and not reopened.export.running
