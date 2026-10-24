"""Cache coherence without Windows ctime's creation-time assumption."""
import ctypes
from types import SimpleNamespace

import pytest

from gnb_player import app as app_module
from gnb_player import file_state
from gnb_player.lyrics import Cue, LyricsDocument, fingerprint


class FrozenMetadataPath:
    """Model a same-size Windows rewrite with restored mtime/creation time."""
    def __init__(self,path):
        self.path=path
        self.state=path.stat()
    def stat(self):return self.state
    def __fspath__(self):return str(self.path)
    def __str__(self):return str(self.path)


def fixture_state(tmp_path):
    audio=tmp_path/'recording.mp3';audio.write_bytes(b'original recording')
    track=SimpleNamespace(path=FrozenMetadataPath(audio))
    document=LyricsDocument([Cue('Original lyric words',1,2)],3,source_sha256=fingerprint(audio))
    window=SimpleNamespace(_lyric_stamps={},lyric_files={},_lyric_locations={},
        _lyric_file_stamps={},_source_checks={},documents={},_invalid_lyrics=set())
    return audio,track,document,window


def test_posix_stamp_needs_no_windows_handle(tmp_path,monkeypatch):
    path=tmp_path/'fixture';path.write_bytes(b'file')
    state=path.stat()
    monkeypatch.setattr(file_state,'_WINDOWS',False)
    monkeypatch.setattr(file_state,'_windows_change_time',lambda _:pytest.fail('Unexpected Windows API call'))
    monkeypatch.setattr(file_state.time,'time_ns',lambda:state.st_ctime_ns+file_state._POSIX_CTIME_SETTLE_NS)
    assert file_state.file_change_stamp(path,state)==(state.st_size,state.st_mtime_ns,state.st_ctime_ns,state.st_ino)


@pytest.mark.parametrize('age',[0,1,1_999_999_999,-1,-10_000_000_000])
def test_posix_recent_or_future_ctime_cannot_reuse_a_content_hash(monkeypatch,age):
    state=SimpleNamespace(st_size=10,st_mtime_ns=100,st_ctime_ns=10_000_000_000,st_ino=7)
    monkeypatch.setattr(file_state,'_WINDOWS',False)
    monkeypatch.setattr(file_state.time,'time_ns',lambda:state.st_ctime_ns+age)
    assert file_state.file_change_stamp('unused',state) is None


@pytest.mark.parametrize('changed',[0,-1,True,None,1.5])
def test_invalid_posix_ctime_disables_metadata_reuse(monkeypatch,changed):
    state=SimpleNamespace(st_size=10,st_mtime_ns=100,st_ctime_ns=changed,st_ino=7)
    monkeypatch.setattr(file_state,'_WINDOWS',False)
    monkeypatch.setattr(file_state.time,'time_ns',lambda:10_000_000_000)
    assert file_state.file_change_stamp('unused',state) is None


def test_same_tick_same_metadata_rewrite_rehashes_and_rejects_old_recording(tmp_path,monkeypatch):
    audio,track,document,window=fixture_state(tmp_path)
    monkeypatch.setattr(file_state,'_WINDOWS',False)
    # The filesystem reports one unchanged ctime for all operations in this
    # tick, as in the actual rapid-write probe. Freeze both metadata and clock.
    monkeypatch.setattr(file_state.time,'time_ns',lambda:track.path.state.st_ctime_ns+10_000_000)
    calls=[]
    def counted(path):calls.append(str(path));return fingerprint(path)
    monkeypatch.setattr(app_module,'fingerprint',counted)
    matches=lambda:app_module.MainWindow._matches_recording(window,track,document)
    assert matches() and matches() and len(calls)==2
    audio.write_bytes(b'x'*track.path.state.st_size)
    assert not matches() and len(calls)==3


def test_settled_posix_audio_reuses_hash_after_one_transition_recheck(tmp_path,monkeypatch):
    _,track,document,window=fixture_state(tmp_path)
    monkeypatch.setattr(file_state,'_WINDOWS',False)
    clock=[track.path.state.st_ctime_ns+1]
    monkeypatch.setattr(file_state.time,'time_ns',lambda:clock[0])
    calls=[]
    def counted(path):calls.append(str(path));return fingerprint(path)
    monkeypatch.setattr(app_module,'fingerprint',counted)
    matches=lambda:app_module.MainWindow._matches_recording(window,track,document)
    assert matches() and len(calls)==1
    clock[0]=track.path.state.st_ctime_ns+file_state._POSIX_CTIME_SETTLE_NS
    assert matches() and len(calls)==2
    assert matches() and matches() and len(calls)==2


def test_unchanged_audio_crossing_settling_boundary_during_hash_is_still_valid(tmp_path,monkeypatch):
    _,track,document,window=fixture_state(tmp_path)
    monkeypatch.setattr(file_state,'_WINDOWS',False)
    clock=[track.path.state.st_ctime_ns+file_state._POSIX_CTIME_SETTLE_NS-1]
    monkeypatch.setattr(file_state.time,'time_ns',lambda:clock[0])
    def crosses_boundary(path):
        identity=fingerprint(path)
        clock[0]+=1
        return identity
    monkeypatch.setattr(app_module,'fingerprint',crosses_boundary)
    assert app_module.MainWindow._matches_recording(window,track,document)


def test_windows_change_time_rehashes_a_same_metadata_recording(tmp_path,monkeypatch):
    audio,track,document,window=fixture_state(tmp_path)
    changed=[100]
    monkeypatch.setattr(file_state,'_WINDOWS',True)
    monkeypatch.setattr(file_state,'_windows_change_time',lambda _:changed[0])
    calls=[]
    def counted(path):calls.append(str(path));return fingerprint(path)
    monkeypatch.setattr(app_module,'fingerprint',counted)
    matches=lambda:app_module.MainWindow._matches_recording(window,track,document)
    assert matches() and matches() and len(calls)==1
    audio.write_bytes(b'x'*len(audio.read_bytes()));changed[0]+=1
    assert not matches() and len(calls)==2


def test_unavailable_windows_change_time_never_reuses_audio_hash(tmp_path,monkeypatch):
    audio,track,document,window=fixture_state(tmp_path)
    monkeypatch.setattr(file_state,'_WINDOWS',True)
    monkeypatch.setattr(file_state,'_windows_change_time',lambda _:None)
    calls=[]
    def counted(path):calls.append(str(path));return fingerprint(path)
    monkeypatch.setattr(app_module,'fingerprint',counted)
    matches=lambda:app_module.MainWindow._matches_recording(window,track,document)
    assert matches() and matches() and len(calls)==2
    audio.write_bytes(b'x'*len(audio.read_bytes()))
    assert not matches() and len(calls)==3


def test_change_during_hash_cannot_enter_source_cache(tmp_path,monkeypatch):
    _,track,document,window=fixture_state(tmp_path)
    changed=[100]
    monkeypatch.setattr(file_state,'_WINDOWS',True)
    monkeypatch.setattr(file_state,'_windows_change_time',lambda _:changed[0])
    def concurrent_change(path):
        value=fingerprint(path);changed[0]+=1;return value
    monkeypatch.setattr(app_module,'fingerprint',concurrent_change)
    assert not app_module.MainWindow._matches_recording(window,track,document)
    assert not window._source_checks


@pytest.mark.parametrize('available',[True,False])
def test_lyric_file_rewrite_is_detected_without_windows_ctime(tmp_path,monkeypatch,available):
    _,track,document,window=fixture_state(tmp_path)
    lyric=tmp_path/'lyrics.json';lyric.write_bytes(b'original lyric file')
    key=str(track.path);window.lyric_files[key]=str(lyric)
    tokens={str(track.path):100,str(lyric):200}
    before=lyric.stat()
    def windows_stamp(path,state=None):
        # POSIX cannot restore ctime; model Windows' unchanged creation time
        # explicitly so this test depends on ChangeTime/content, not POSIX.
        if str(path)==str(lyric):state=before
        return file_state.file_change_stamp(path,state)
    monkeypatch.setattr(app_module,'file_change_stamp',windows_stamp)
    monkeypatch.setattr(file_state,'_WINDOWS',True)
    monkeypatch.setattr(file_state,'_windows_change_time',lambda path:tokens[str(path)] if available else None)
    app_module.MainWindow._remember_document(window,track,document)
    assert app_module.MainWindow._matches_recording(window,track,document)
    lyric.write_bytes(b'x'*before.st_size)
    # Metadata matching on Windows is deliberately not a reliable fallback.
    import os
    os.utime(lyric,ns=(before.st_atime_ns,before.st_mtime_ns))
    tokens[str(lyric)]+=1
    assert not app_module.MainWindow._matches_recording(window,track,document)


@pytest.mark.parametrize('query_success',[True,False])
def test_windows_metadata_handle_shares_writes_and_is_closed(monkeypatch,query_success):
    calls=[]
    def create(path,access,sharing,security,disposition,flags,template):
        calls.append(('open',path,access,sharing,disposition));return 42
    def query(handle,info_class,info,size):
        assert handle==42 and info_class==0 and size==ctypes.sizeof(file_state._FileBasicInfo)
        info._obj.ChangeTime=123456
        return query_success
    def close(handle):calls.append(('close',handle));return True
    monkeypatch.setattr(file_state,'_windows_api',lambda:(create,query,close))
    assert file_state._windows_change_time('fixture')==(123456 if query_success else None)
    assert calls[0][2:]==(0x0080,7,3)
    assert calls[-1]==('close',42)
