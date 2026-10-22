from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('seed_goodneighbor', Path(__file__).resolve().parents[1]/'tools/seed_goodneighbor.py')
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)


def test_payload_mismatch_rejects_even_identical_title(tmp_path):
    copy = tmp_path/'same-title.mp3';copy.write_bytes(b'actual copy')
    entry = {'audio_hash': 'SHA256='+'a'*64, 'document': {'song_id':'d'*32,'title':'Same Title','source_sha256':'b'*64,'segments':[]}}
    before = deepcopy(entry)
    with pytest.raises(ValueError, match='payload mismatch'):
        seed.prepare_imported_lyrics(entry,copy,'SHA256='+'c'*64,'Same Title')
    assert entry == before


def test_verified_import_updates_copy_fingerprint_and_preserves_provenance(tmp_path):
    copy = tmp_path/'copy.mp3';copy.write_bytes(b'copied recording')
    words=[{'text':'Blues','start':1,'end':2}]
    entry={'audio_hash':'SHA256='+'a'*64,'source_name':'original.mp3','publication_method':'human','document':{'song_id':'d'*32,'title':'Blues','source_sha256':'b'*64,'duration':3,'segments':[{'text':'Blues','start':1,'end':2,'words':words}]}}
    before=deepcopy(entry)
    result=seed.prepare_imported_lyrics(entry,copy,entry['audio_hash'],'Blues')
    assert result['source_sha256']==seed.file_hash(copy)
    assert result['provenance']['original_source_sha256']=='b'*64
    assert result['provenance']['verified_audio_packet_hash']==entry['audio_hash']
    assert result['segments']==entry['document']['segments']
    assert entry==before


def test_profile_paths_survive_parent_change(tmp_path):
    pack=tmp_path/'media-packs/station';pack.mkdir(parents=True)
    user_parent=tmp_path/'user-data';user_parent.mkdir()
    profile={'version':1,'name':'My Station','tracks':[{'path':'music/song.mp3','title':'Song','duration':3}],'backgrounds':['backgrounds/art.webp'],'lyrics':{'music/song.mp3':'lyrics/song.json'},'lyrics_ready':['music/song.mp3'],'settings':{'brand':'My Station'}}
    copied=seed.relative_profile(profile,pack,user_parent)
    assert not Path(copied['tracks'][0]['path']).is_absolute()
    assert (user_parent/copied['tracks'][0]['path']).resolve()==pack/'music/song.mp3'
    assert (user_parent/copied['backgrounds'][0]).resolve()==pack/'backgrounds/art.webp'
    key=next(iter(copied['lyrics']))
    assert key==copied['tracks'][0]['path']==copied['lyrics_ready'][0]
    assert (user_parent/copied['lyrics'][key]).resolve()==pack/'lyrics/song.json'
    assert profile['tracks'][0]['path']=='music/song.mp3'


def test_copy_is_idempotent_and_keeps_source_and_existing_edits(tmp_path):
    source=tmp_path/'source.mp3';source.write_bytes(b'original bytes')
    target=tmp_path/'pack/music/copy.mp3'
    digest,copied=seed.safe_copy(source,target)
    assert copied and digest==seed.file_hash(source)
    assert seed.safe_copy(source,target)==(digest,False)
    target.write_bytes(b'user edited copy')
    with pytest.raises(ValueError,match='existing pack file differs'):
        seed.safe_copy(source,target)
    assert source.read_bytes()==b'original bytes'
    assert target.read_bytes()==b'user edited copy'


def test_selected_collection_uses_relative_sources_without_lyric_snapshot(tmp_path,monkeypatch):
    collection=tmp_path/'collection';collection.mkdir()
    source=collection/'original.wav';source.write_bytes(b'original recording')
    packet='SHA256='+'a'*64
    plan=collection/'albums.json'
    plan.write_text(json.dumps({'albums':[{'title':'First Album','tracks':[{
        'source':'original.wav','title':'A Song','duration':3,'audio_hash':packet,'slug':'../a-song',
    }]}]}),encoding='utf-8')
    monkeypatch.setattr(seed,'packet_hash',lambda _:packet)
    pack=tmp_path/'selected-pack'
    report=seed.make_pack(plan,None,pack,None,None,name='My Station')
    profile=json.loads((pack/'profile.json').read_text(encoding='utf-8'))
    copied=pack/profile['tracks'][0]['path']
    assert copied.parent==pack/'music' and copied.suffix=='.wav'
    assert copied.read_bytes()==source.read_bytes()==b'original recording'
    assert profile['name']=='My Station' and profile['lyrics']=={} and profile['backgrounds']==[]
    assert report['music_tracks']==1 and not report['originals_modified']


def test_cli_requires_explicit_collection_and_output(capsys):
    with pytest.raises(SystemExit) as result:
        seed.main([])
    assert result.value.code==2
    message=capsys.readouterr().err
    assert '--plan' in message and '--pack' in message
