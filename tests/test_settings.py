from pathlib import Path
import json
import pytest
from gnb_player.settings import read_profile,write_profile,load_m3u
from gnb_player.library import Track

def test_portable_profile_roundtrip(tmp_path):
    music=tmp_path/'album'/'song.mp3';music.parent.mkdir();music.write_bytes(b'fixture')
    picture=tmp_path/'picture.png';picture.write_bytes(b'fixture')
    lyrics=tmp_path/'song.json';lyrics.write_text('{}')
    path=tmp_path/'profile.json';track=Track(music,'Moon Over Boston',duration=120)
    write_profile(path,[track],[picture],{str(music):str(lyrics)},{'brand':'GNB'})
    raw=json.loads(path.read_text());assert raw['tracks'][0]['path']=='album/song.mp3'
    result=read_profile(path);assert result['tracks'][0].path==music;assert result['lyrics'][str(music)]==str(lyrics)

def test_malformed_profile_lists_are_rejected(tmp_path):
    path=tmp_path/'bad.json'
    for patch in [{'backgrounds':'wrong'},{'lyrics':[]},{'settings':[]},{'tracks':{}}]:
        path.write_text(json.dumps({'version':1,**patch}))
        with pytest.raises(ValueError):read_profile(path)

def test_m3u_rejects_remote_protocols(tmp_path):
    path=tmp_path/'bad.m3u';path.write_text('#EXTM3U\nhttps://example.invalid/song.mp3\n')
    with pytest.raises(ValueError):load_m3u(path)
def test_starter_pack_does_not_override_saved_session(tmp_path):
    from gnb_player.settings import starter_profile
    root=tmp_path/'app';seed=root/'media-packs/goodneighbor/profile.json'
    seed.parent.mkdir(parents=True);seed.write_text('{}')
    state=tmp_path/'state'
    assert starter_profile(state,[tmp_path/'missing',root])==seed
    state.mkdir();(state/'session.json').write_text('{}')
    assert starter_profile(state,[root]) is None
