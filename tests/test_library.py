from pathlib import Path
import json
import subprocess
from unittest.mock import patch

import pytest

from gnb_player.library import Track, normalize_title, probe_track, scan_media


def test_compact_titles_and_apa_minor_words():
    assert normalize_title("moonoverboston") == "Moon Over Boston"
    assert normalize_title("backdoorkey") == "Back Door Key"
    assert normalize_title("yourcupiscold") == "Your Cup Is Cold"
    assert normalize_title("a_song_for_the_road") == "A Song for the Road"
    assert normalize_title("the song: a voice of her own") == "The Song: A Voice of Her Own"
    assert normalize_title("wake up and carry on") == "Wake Up and Carry On"
    assert normalize_title("MacCready's blues") == "MacCready's Blues"


def test_track_roundtrip_keeps_original_path_and_metadata(tmp_path):
    track = Track(tmp_path / "original source.mp3", "A Song", "An Artist", "An Album", 52.4)
    assert Track.from_dict(json.loads(json.dumps(track.to_dict()))) == track
    assert track.path.name == "original source.mp3"


def test_scanner_deduplicates_paths_ignores_documents_and_does_not_edit(tmp_path):
    audio = tmp_path / "moonoverboston.mp3"
    audio.write_bytes(b"fixture source")
    (tmp_path / "excluded-document.pdf").write_bytes(b"Do not inspect this document")
    (tmp_path / "cover.jpg").write_bytes(b"image")
    link = tmp_path / "copy.mp3"
    try:
        link.symlink_to(audio)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links.")
    child = tmp_path / "nested"
    child.mkdir()
    video = child / "show.MP4"
    video.write_bytes(b"fixture video")
    with patch("gnb_player.library.probe_track", side_effect=lambda p: Track(p, normalize_title(p.stem))) as probe:
        tracks = scan_media([tmp_path, audio, link])
    assert {t.path for t in tracks} == {audio.resolve(), video.resolve()}
    assert probe.call_count == 2
    assert audio.read_bytes() == b"fixture source"
    assert link.is_symlink()


def test_probe_uses_local_safe_argument_vector_and_format_tags(tmp_path):
    path = tmp_path / "strange ' $(echo nope).mp3"
    path.write_bytes(b"fixture")
    result = subprocess.CompletedProcess([], 0, json.dumps({"format": {"duration": "182.4", "tags": {"TITLE": "moonoverboston", "artist": "Kaotick Jay", "album": "Originals"}}}).encode(), b"")
    with patch("gnb_player.library.subprocess.run", return_value=result) as run:
        track = probe_track(path, ffprobe="/usr/bin/ffprobe")
    assert track.title == "Moon Over Boston"
    assert track.duration == 182.4
    assert track.artist == "Kaotick Jay"
    assert run.call_args.args[0][-1] == str(path.resolve())
    assert "shell" not in run.call_args.kwargs
    assert run.call_args.kwargs["timeout"] == 8


def test_bad_metadata_falls_back_without_blocking_library(tmp_path):
    path = tmp_path / "red_rocket_whiskey.flac"
    path.write_bytes(b"fixture")
    with patch("gnb_player.library.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 8)):
        track = probe_track(path, ffprobe="ffprobe")
    assert track.title == "Red Rocket Whiskey"
    assert track.duration == 0


def test_scan_is_bounded_and_cancellable(tmp_path):
    for index in range(5):
        (tmp_path / f"song{index}.mp3").write_bytes(b"fixture")
    with patch("gnb_player.library.probe_track", side_effect=lambda p: Track(p, p.stem)):
        assert len(scan_media([tmp_path], max_files=2)) == 2
        assert scan_media([tmp_path], cancelled=lambda: True) == []
