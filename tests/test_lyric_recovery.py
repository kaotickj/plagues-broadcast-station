"""Disposable recognition evidence and a copy-only dialog; no models/audio."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QPushButton, QWidget

from gnb_player import lyric_recovery as recovery


SHA = 'a' * 64
OTHER_SHA = 'b' * 64


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def payload():
    words = [
        {'text': ' 雨', 'start': 1, 'end': 2, 'score': .99},
        {'text': ' ©', 'start': 5, 'end': 6, 'score': .2},
        {'text': ' Thank', 'start': 28, 'end': 30, 'score': .12},
        {'text': ' you.', 'start': 30, 'end': 30, 'score': .99},
    ]
    return {
        'version': 'extraction-failure-1', 'source_sha256': SHA,
        'created_at': 1792853347, 'status': 'failed',
        'message': 'Implausible alignment — timing unverified. <b>literal text</b>',
        'recognition_evidence': {
            'version': 'raw-recognition-1',
            'primary': {'backend': 'faster-whisper', 'engine': 'fixture', 'segments': [
                {'text': ' 雨 © Thank you.', 'start': 1, 'end': 30,
                 'words': deepcopy(words), 'no_speech_prob': .77},
            ]},
            'pre_alignment': [
                {'text': '雨© Thank you.', 'start': 1, 'end': 30,
                 'words': [{'text': '雨©', 'start': 1, 'end': 2, 'score': .99},
                           *deepcopy(words[2:])]},
            ],
            'alignment': {'status': 'rejected', 'reason': 'word stretched over 12 seconds'},
        },
    }


def artifact(tmp_path, data=None, *, sha=SHA, attempt='c' * 32):
    directory = tmp_path / 'failures'
    directory.mkdir(exist_ok=True)
    path = directory / (sha + '-' + attempt + '.json')
    path.write_text(json.dumps(data if data is not None else payload(), ensure_ascii=False), encoding='utf-8')
    return path


def load(path, *, sha=SHA, directory=None):
    return recovery.load_failure_candidate(
        path, failures_dir=directory or path.parent, expected_source_sha256=sha)


def test_loader_preserves_exact_evidence_zero_duration_and_distinct_counts(tmp_path):
    path = artifact(tmp_path)
    original = path.read_bytes()
    candidate = load(path)
    assert candidate.raw_payload == original
    assert candidate.payload == json.loads(original)
    assert candidate.evidence == json.loads(original)['recognition_evidence']
    assert candidate.text == ' 雨 © Thank you.'
    assert candidate.raw_word_count == 4
    assert candidate.pre_alignment_word_count == 3
    assert candidate.raw_text_record_count == 1
    assert candidate.zero_duration_word_count == 1
    assert 'timing unverified' in candidate.summary
    assert 'without guessed timing' in candidate.summary
    assert '30' in candidate.diagnostics_text and 'you.' in candidate.diagnostics_text
    assert candidate.evidence['primary']['segments'][0]['words'][-1]['start'] == 30
    assert candidate.evidence['primary']['segments'][0]['words'][-1]['end'] == 30
    # Accessors cannot be used to mutate later views of the original evidence.
    candidate.evidence['primary']['segments'][0]['words'].clear()
    candidate.payload['message'] = 'changed copy'
    assert candidate.payload == json.loads(original)
    with pytest.raises(FrozenInstanceError):
        candidate.text = 'promoted'
    assert not hasattr(candidate, 'segments') and not hasattr(candidate, 'save')
    assert path.read_bytes() == original


def test_loader_accepts_measured_long_durations_as_unverified_diagnostics(tmp_path):
    data = payload()
    data['recognition_evidence']['primary']['segments'][0]['words'][0].update(start=0, end=19.1)
    candidate = load(artifact(tmp_path, data))
    assert candidate.evidence['primary']['segments'][0]['words'][0]['end'] == 19.1
    assert 'timing unverified' in candidate.summary


def test_vulkan_decoder_rows_remain_visible_without_text_blacklisting(tmp_path):
    data = payload()
    rows = [
        {'text': '', 'offsets': {'from': 0, 'to': 0}, 'tokens': [{'text': '[_BEG_]', 'p': .9}]},
        {'text': ' Thank', 'offsets': {'from': 1000, 'to': 20110},
         'tokens': [{'text': ' Thank', 'p': .2}]},
        {'text': ' you.♪', 'offsets': {'from': 20110, 'to': 30000},
         'tokens': [{'text': ' you.', 'p': .99}]},
    ]
    data['recognition_evidence']['primary'] = {
        'backend': 'whisper.cpp', 'windows': [{'key': 'full', 'start': 0, 'end': 30,
                                            'raw': {'transcription': rows}}]}
    candidate = load(artifact(tmp_path, data))
    assert candidate.raw_word_count == 0  # This backend has decoder rows, not word objects.
    assert candidate.raw_text_record_count == 2
    assert candidate.text == ' Thank you.♪'
    assert candidate.evidence['primary']['windows'][0]['raw']['transcription'] == rows


@pytest.mark.parametrize('sha', [OTHER_SHA, '', 'invalid', SHA.upper(), None])
def test_exact_expected_fingerprint_rejects_foreign_or_invalid_recording(tmp_path, sha):
    path = artifact(tmp_path)
    with pytest.raises(recovery.RecoveryError):
        load(path, sha=sha)


def test_stale_payload_fingerprint_is_rejected_even_with_current_filename(tmp_path):
    data = payload()
    data['source_sha256'] = OTHER_SHA
    with pytest.raises(recovery.RecoveryError, match='different recording'):
        load(artifact(tmp_path, data))


def test_same_recording_old_attempt_can_be_reopened_after_relaunch(tmp_path):
    path = artifact(tmp_path)
    first = load(path)
    del first
    assert load(path).raw_payload == path.read_bytes()


def test_path_escape_nested_paths_and_arbitrary_filenames_are_rejected(tmp_path):
    path = artifact(tmp_path)
    outside = tmp_path / path.name
    outside.write_bytes(path.read_bytes())
    nested = path.parent / 'nested'
    nested.mkdir()
    inner = nested / path.name
    inner.write_bytes(path.read_bytes())
    bad_name = path.parent / 'failure.json'
    bad_name.write_bytes(path.read_bytes())
    for bad in [outside, inner, bad_name, path.parent / '..' / path.name]:
        with pytest.raises(recovery.RecoveryError):
            load(bad, directory=path.parent)


@pytest.mark.parametrize('kind', ['file', 'directory', 'ancestor'])
def test_symlink_paths_are_rejected(tmp_path, kind):
    path = artifact(tmp_path)
    if kind == 'file':
        target = tmp_path / 'target.json'
        path.rename(target)
        path.symlink_to(target)
    elif kind == 'directory':
        real = tmp_path / 'real'
        path.parent.rename(real)
        (tmp_path / 'failures').symlink_to(real, target_is_directory=True)
    else:
        alias = tmp_path / 'alias'
        alias.symlink_to(tmp_path, target_is_directory=True)
        path = alias / 'failures' / path.name
    with pytest.raises(recovery.RecoveryError):
        load(path)


def test_missing_oversized_and_nonregular_artifacts_are_rejected(tmp_path, monkeypatch):
    path = artifact(tmp_path)
    monkeypatch.setattr(recovery, 'MAX_RECOVERY_BYTES', len(path.read_bytes()) - 1)
    with pytest.raises(recovery.RecoveryError):
        load(path)
    path.unlink()
    with pytest.raises(recovery.RecoveryError):
        load(path)
    path.mkdir()
    with pytest.raises(recovery.RecoveryError):
        load(path)


@pytest.mark.parametrize('content', [
    b'broken', b'[]', b'{}', b'\xff',
    b'{"version":"extraction-failure-1","version":"extraction-failure-1"}',
    b'{"number":NaN}', b'{"number":Infinity}', b'{"number":1e999}',
    b'{"text":"\\ud800"}',
])
def test_invalid_encoding_json_duplicates_and_nonfinite_values_are_rejected(tmp_path, content):
    path = artifact(tmp_path)
    path.write_bytes(content)
    with pytest.raises(recovery.RecoveryError):
        load(path)


@pytest.mark.parametrize('change', [
    lambda d: d.update(status='success'),
    lambda d: d.update(version='broadcast-player-lyrics-1'),
    lambda d: d.update(created_at=True),
    lambda d: d.update(message=None),
    lambda d: d.update(recognition_evidence=[]),
    lambda d: d['recognition_evidence'].update(primary=[]),
    lambda d: d['recognition_evidence']['primary'].update(segments={}),
    lambda d: d['recognition_evidence']['primary']['segments'][0].update(words='broken'),
    lambda d: d['recognition_evidence']['primary']['segments'][0]['words'][0].update(start=True),
    lambda d: d['recognition_evidence']['primary']['segments'][0]['words'][0].update(end=-1),
    lambda d: d['recognition_evidence']['primary']['segments'][0]['words'][0].update(score=2),
    lambda d: d['recognition_evidence'].update(pre_alignment=None),
])
def test_malformed_failure_schema_or_word_measurements_are_rejected(tmp_path, change):
    data = payload()
    change(data)
    with pytest.raises(recovery.RecoveryError):
        load(artifact(tmp_path, data))


def test_nonfinite_number_in_unknown_diagnostics_is_also_rejected(tmp_path):
    data = payload()
    data['recognition_evidence']['diagnostics'] = {'unknown': float('nan')}
    with pytest.raises(recovery.RecoveryError):
        load(artifact(tmp_path, data))


def test_loader_and_dialog_write_no_files_or_parent_editor_state(qt_app, tmp_path, monkeypatch):
    path = artifact(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    parent = QWidget()
    parent.editor = QPlainTextEdit(parent)
    parent.editor.setPlainText('Unsaved manual correction — 雨')
    parent.documents = {'current': object()}
    parent._lyric_buffers = {'current': {'manual': ['unsaved correction']}}
    parent.stage = Mock()
    original_documents = parent.documents.copy()
    original_buffers = deepcopy(parent._lyric_buffers)
    original_open = recovery.os.open

    def read_only_open(name, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(recovery.os, 'open', read_only_open)
    candidate = load(path)
    dialog = recovery.RecoveryDialog(candidate, parent)
    try:
        assert dialog.text.isReadOnly() and dialog.diagnostics.isReadOnly()
        assert not dialog.isModal()
        assert dialog.text.toPlainText() == candidate.text
        assert dialog.diagnostics.toPlainText() == candidate.diagnostics_text
        assert dialog.findChild(QLabel, 'lyric_recovery_summary').textFormat() == Qt.TextFormat.PlainText
        buttons = {button.text() for button in dialog.findChildren(QPushButton)}
        assert buttons == {'Copy recognition text', 'Copy diagnostics', 'Close'}
        dialog.findChild(QPushButton, 'lyric_recovery_copy_text').click()
        assert qt_app.clipboard().text() == candidate.text
        dialog.findChild(QPushButton, 'lyric_recovery_copy_diagnostics').click()
        assert qt_app.clipboard().text() == candidate.diagnostics_text
        assert parent.editor.toPlainText() == 'Unsaved manual correction — 雨'
        assert parent.documents == original_documents and parent._lyric_buffers == original_buffers
        parent.stage.set_lyrics.assert_not_called()
        assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    finally:
        dialog.close()
        parent.close()


@pytest.mark.parametrize('action', ['close', 'escape'])
def test_child_close_keeps_main_window_and_manual_buffer_open(qt_app, tmp_path, action):
    parent = QWidget()
    parent.show()
    dialog = recovery.RecoveryDialog(load(artifact(tmp_path)), parent)
    dialog.show()
    qt_app.processEvents()
    assert parent.isVisible() and dialog.isVisible()
    if action == 'close':
        dialog.findChild(QPushButton, 'lyric_recovery_close').click()
    else:
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    qt_app.processEvents()
    assert parent.isVisible() and not dialog.isVisible()
    dialog.close()
    parent.close()


def test_actual_latest_cpu_failure_is_read_only_and_keeps_409_raw_vs_408_pre_words():
    # Optional local evidence check: no user's lyrics/audio are copied into Git.
    # The same checked-in synthetic tests run when this private file is absent.
    path = Path('/home/kaotickj/Documents/Codex/builds/goodneighbor-broadcast-player/'
                'user-data-plague-radio/lyrics-cache/failures/'
                '7af7d12beed49681f9d88327c25a5e3c852f3de7ea358267ec5fb47e9191bbc8-'
                '3a686bb1dab84b4587891f1b75ad2f29.json')
    if not path.is_file():
        pytest.skip('Private saved CPU failure is not present on this machine.')
    before = path.read_bytes()
    expected = json.loads(before)
    sha = expected['source_sha256']
    candidate = load(path, sha=sha)
    assert candidate.raw_payload == before and candidate.payload == expected
    assert candidate.raw_word_count == 409
    assert candidate.raw_text_record_count == 69
    assert candidate.pre_alignment_word_count == 408
    assert candidate.zero_duration_word_count == 2
    raw_words = [word for segment in candidate.evidence['primary']['segments'] for word in segment['words']]
    assert raw_words[-1]['text'] == ' you.'
    assert raw_words[-1]['start'] == raw_words[-1]['end'] == 479.9
    assert any(word['text'] == ' ©' for word in raw_words)
    assert 'Emily' in candidate.text and candidate.text.endswith(' Thank you.')
    assert 'timing unverified' in candidate.summary
    assert path.read_bytes() == before
