"""Content checks for small lyric files independent of timestamp resolution."""
import os
from types import SimpleNamespace

import pytest

from gnb_player import app as app_module, lyrics
from gnb_player.lyrics import Cue, LyricsDocument, fingerprint, lyric_content_revision


def fixture_state(tmp_path):
    audio=tmp_path/'recording.mp3';audio.write_bytes(b'original recording')
    lyric=tmp_path/'recording.lrc';lyric.write_bytes(b'[00:01]Original words\n')
    track=SimpleNamespace(path=audio)
    document=LyricsDocument([Cue('Original words',1,2)],3,source_sha256=fingerprint(audio))
    window=SimpleNamespace(_lyric_stamps={},lyric_files={str(audio):str(lyric)},
        _publication_revisions={},_lyric_locations={},_lyric_file_stamps={},_source_checks={},documents={},_invalid_lyrics=set())
    return lyric,track,document,window


def test_same_metadata_lyric_rewrite_cannot_reuse_a_loaded_document(tmp_path,monkeypatch):
    lyric,track,document,window=fixture_state(tmp_path)
    monkeypatch.setattr(app_module,'file_change_stamp',lambda *args:(100,200,300,400))
    app_module.MainWindow._remember_document(window,track,document)
    assert app_module.MainWindow._matches_recording(window,track,document)
    before=lyric.stat()
    lyric.write_bytes(b'x'*before.st_size)
    os.utime(lyric,ns=(before.st_atime_ns,before.st_mtime_ns))
    assert not app_module.MainWindow._matches_recording(window,track,document)


def test_unreadable_initial_lyric_revision_cannot_be_trusted_later(tmp_path):
    lyric,track,document,window=fixture_state(tmp_path)
    original=lyric.read_bytes();lyric.unlink()
    app_module.MainWindow._remember_document(window,track,document)
    lyric.write_bytes(original)
    assert not app_module.MainWindow._matches_recording(window,track,document)


def test_oversized_replacement_is_bounded_and_kept_off_output(tmp_path,monkeypatch):
    lyric,track,document,window=fixture_state(tmp_path)
    monkeypatch.setattr(lyrics,'MAX_LYRIC_BYTES',32)
    app_module.MainWindow._remember_document(window,track,document)
    assert app_module.MainWindow._matches_recording(window,track,document)
    lyric.write_bytes(b'x'*33)
    with pytest.raises(ValueError,match='smaller than 8 MB'):
        lyric_content_revision(lyric)
    assert not app_module.MainWindow._matches_recording(window,track,document)


def test_readable_subtitle_symlink_keeps_existing_preview_support(tmp_path):
    lyric,track,document,window=fixture_state(tmp_path)
    linked=tmp_path/'linked.lrc';linked.symlink_to(lyric)
    window.lyric_files[str(track.path)]=str(linked)
    assert lyric_content_revision(linked)==lyric_content_revision(lyric)
    app_module.MainWindow._remember_document(window,track,document)
    assert app_module.MainWindow._matches_recording(window,track,document)
