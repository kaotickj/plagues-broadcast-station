"""Explicit import provenance and extraction compare-and-swap preservation."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import sys
import pytest

from gnb_player import lyrics, extract_worker as worker
from gnb_player.extraction import ExtractionConfig
from gnb_player.lyrics import (LyricsDocument, Cue, load_lyrics, load_imported_lyrics,
                              save_lyrics, fingerprint, lyric_target_revision,
                              MAX_IMPORT_PROVENANCE_BYTES, MAX_LYRIC_BYTES)
from gnb_player.lyric_review import (edit_document, retarget_review, save_review_document,
                                    document_revision, is_approved)


def imported_payload():
    return {'song_id':'d'*32,'origin_id':'origin-123','title':'Original title',
            'source_sha256':'','duration':10,'reviewed':True,
            'publication':{'method':'human','published_at':12345},
            'machine_review':{'version':'website-review-3','held_passages':[{'at':1,'reason':'Listen here'}]},
            'provenance':{'recording_id':'d'*32,'original_source_sha256':'b'*64,
                          'verified_audio_packet_hash':'SHA256='+'c'*64},
            'unknown_future_field':{'preserve':['café','夜']},
            'quality':{'status':'pass','reasons':[]},
            'segments':[{'text':'Original lyric','start':1,'end':3,
                         'unknown_line_id':'line-1','words':[]}]}


def test_explicit_import_edit_save_export_preserves_origin_snapshot_and_demotes_legacy_approval(tmp_path):
    incoming=tmp_path/'import.json';raw=imported_payload();payload=json.dumps(raw,ensure_ascii=False).encode();incoming.write_bytes(payload)
    doc=load_imported_lyrics(incoming);snapshot=deepcopy(doc.metadata['import_provenance'])
    assert snapshot['payload_sha256']==sha256(payload).hexdigest()
    assert snapshot['claims_are_unverified'] is True
    assert snapshot['original_fields']['source_sha256']==''
    assert snapshot['original_fields']['publication']==raw['publication']
    assert snapshot['original_fields']['reviewed'] is True
    source=tmp_path/'recording.mp3';source.write_bytes(b'operator selected recording')
    doc.source_sha256=fingerprint(source);doc.title='Local title'
    bound=retarget_review(doc)
    edited=edit_document(bound,[(1,3,'Corrected café 夜')])
    target=tmp_path/'cache.json';saved=save_review_document(edited,target,source,None)
    exported=tmp_path/'export.json';save_lyrics(saved,exported)
    for restored in (load_lyrics(target),load_lyrics(exported),load_imported_lyrics(exported)):
        data=restored.to_dict()
        assert data['song_id']==raw['song_id'] and data['origin_id']==raw['origin_id']
        assert data['provenance']==raw['provenance'] and data['machine_review']==raw['machine_review']
        assert data['unknown_future_field']==raw['unknown_future_field']
        assert data['source_sha256']==fingerprint(source)
        assert data['import_provenance']==snapshot
        assert data['reviewed'] is False and 'publication' not in data
        assert data['human_review']['status']=='draft' and not is_approved(restored)
        assert restored.segments[0].text=='Corrected café 夜'
    assert incoming.read_bytes()==payload


def test_import_hash_and_parsed_document_come_from_same_bounded_read(tmp_path,monkeypatch):
    p=tmp_path/'changing.json';original=imported_payload();payload=json.dumps(original).encode();p.write_bytes(payload)
    read=lyrics._read_lyric_bytes
    def changed_after_read(file):
        result=read(file);replacement=deepcopy(original);replacement['song_id']='e'*32
        file.write_text(json.dumps(replacement));return result
    monkeypatch.setattr(lyrics,'_read_lyric_bytes',changed_after_read)
    doc=load_imported_lyrics(p)
    assert doc.metadata['song_id']=='d'*32
    assert doc.metadata['import_provenance']['payload_sha256']==sha256(payload).hexdigest()


def test_oversized_snapshot_field_is_explicitly_hashed_without_losing_live_metadata(tmp_path):
    raw=imported_payload();raw['huge_origin_evidence']={'data':'z'*(400*1024)}
    p=tmp_path/'large.json';p.write_text(json.dumps(raw));doc=load_imported_lyrics(p)
    snapshot=doc.metadata['import_provenance']
    assert len(json.dumps(snapshot,ensure_ascii=False).encode())<=MAX_IMPORT_PROVENANCE_BYTES
    assert snapshot['original_fields']['song_id']==raw['song_id']
    assert snapshot['omitted_field_count']==1
    assert snapshot['omitted_fields'][0]['field']=='huge_origin_evidence'
    assert len(snapshot['omitted_fields'][0]['sha256'])==64
    assert doc.metadata['huge_origin_evidence']==raw['huge_origin_evidence']
    destination=tmp_path/'saved.json';save_lyrics(doc,destination)
    assert load_imported_lyrics(destination).metadata['import_provenance']==snapshot


def test_oversized_saved_payload_is_refused_without_replacing_reopenable_file(tmp_path):
    p=tmp_path/'lyrics.json';doc=LyricsDocument([Cue('Keep',1,2)],3);save_lyrics(doc,p);before=p.read_bytes()
    doc.metadata['oversized']='z'*MAX_LYRIC_BYTES
    with pytest.raises(ValueError,match='8 MB'):
        save_lyrics(doc,p)
    assert p.read_bytes()==before and load_lyrics(p).segments[0].text=='Keep'


@pytest.mark.parametrize('change',['metadata','created','deleted'])
def test_extraction_cas_rejects_changed_created_and_deleted_targets(tmp_path,change):
    p=tmp_path/'lyrics.json'
    if change!='created':p.write_text(json.dumps(imported_payload()))
    expected=lyric_target_revision(p)
    if change=='metadata':
        raw=imported_payload();raw['origin_id']='new metadata';p.write_text(json.dumps(raw))
    elif change=='created':p.write_text('{"new":"human file"}')
    else:p.unlink()
    before=p.read_bytes() if p.exists() else None
    with pytest.raises(ValueError,match='changed during extraction'):
        worker.atomic_result(p,{'segments':[]},expected)
    assert (p.read_bytes() if p.exists() else None)==before
    assert not (tmp_path/'history').exists()


def test_same_recording_refresh_keeps_origin_mapping_but_never_live_approval(tmp_path):
    p=tmp_path/'lyrics.json';raw=imported_payload();raw['source_sha256']='a'*64;p.write_text(json.dumps(raw))
    imported=load_imported_lyrics(p);save_lyrics(imported,p);before=p.read_bytes();expected=lyric_target_revision(p)
    fresh={'source_sha256':'a'*64,'segments':[],'human_review':{'status':'approved'},'reviewed':True,'publication':{'method':'human'}}
    worker.atomic_result(p,fresh,expected)
    out=json.loads(p.read_text())
    assert out['song_id']==raw['song_id'] and out['provenance']==raw['provenance']
    assert out['import_provenance']==imported.metadata['import_provenance']
    assert out['reviewed'] is False and 'publication' not in out and 'human_review' not in out
    assert before in [h.read_bytes() for h in (tmp_path/'history').glob('*.json')]
    worker.atomic_result(p,{'source_sha256':'b'*64,'segments':[]},lyric_target_revision(p))
    assert 'provenance' not in json.loads(p.read_text())


def test_worker_inference_cannot_replace_newer_human_approved_revision(tmp_path):
    source=tmp_path/'recording.mp3';source.write_bytes(b'unchanged audio');stamp=source.stat();identity=fingerprint(source)
    target=tmp_path/'lyrics.json';original=LyricsDocument([Cue('Original',1,3)],10,source_sha256=identity);save_lyrics(original,target)
    human_bytes=[]
    def inference(*args,**kwargs):
        current=load_lyrics(target);edited=edit_document(current,[(1,3,'Human correction')])
        save_review_document(edited,target,source,document_revision(current),approve=True)
        human_bytes.append(target.read_bytes())
        return {'duration':10,'segments':[{'start':1,'end':3,'text':'Stale model result','words':[]}]}
    modules={'lyrics_acceleration':SimpleNamespace(reset_gpu_timings=lambda:None,gpu_timings=lambda:[]),
             'lyrics_pipeline':SimpleNamespace(extract=inference),'lyrics_review':SimpleNamespace(review=lambda *a,**kw:a[2]),
             'lyrics_quality':SimpleNamespace(assess=lambda d:{'status':'pass'})}
    cfg=ExtractionConfig(cache_dir=str(tmp_path/'cache'),auto_review=False)
    with patch.object(worker,'validate_source',return_value=(stamp.st_size,stamp.st_mtime_ns,identity,10)),patch.object(worker,'require_runtime'),patch.dict(sys.modules,modules):
        with pytest.raises(ValueError,match='changed during extraction'):
            worker.run(source,target,cfg)
    assert target.read_bytes()==human_bytes[0]
    assert is_approved(load_lyrics(target),identity)
    assert len(list((tmp_path/'history').glob('*.json')))==1
