"""Bounded private output diagnostics; fake credentials and encoder only."""
import json
import os
from urllib.parse import quote

import pytest
from PySide6.QtWidgets import QApplication

from gnb_player.output_diagnostics import MAX_BYTES, MAX_EVENTS, OutputDiagnostics
from test_broadcast_profile_ui import desk


def test_store_redacts_known_encoded_partial_and_unlisted_credentials(tmp_path):
    path = tmp_path / 'private' / 'output.json'
    secret = 'DJ/secret%+#'
    target = 'rtmps://user:URL-PASS@host.invalid/app/PATH-SECRET?token=QUERY-SECRET'
    message = (f'{target} raw={secret} encoded={quote(secret, safe="")} '
               'partial URL-PASS PATH-SECRET QUERY-SECRET '
               'password=UNLISTED-PASS token=UNLISTED-TOKEN key=UNLISTED-KEY '
               'Bearer UNLISTED-BEARER')
    store = OutputDiagnostics(path, metadata={'version': 'fixture', 'environment': 'META-SECRET'})
    store.append({'event': 'encoder_error', 'message': message,
                  'environment': {'PRIVATE': 'ENV-CANARY'}, 'argv': ['ARGS-CANARY'],
                  'token': 'UNKNOWN-FIELD-SECRET'}, target=target, secrets=(secret,))
    disk = path.read_text()
    copied = store.text(target=target, secrets=(secret,))
    for value in (secret, quote(secret, safe=''), target, 'URL-PASS', 'PATH-SECRET',
                  'QUERY-SECRET', 'UNLISTED-PASS', 'UNLISTED-TOKEN', 'UNLISTED-KEY',
                  'UNLISTED-BEARER', 'META-SECRET', 'ENV-CANARY', 'ARGS-CANARY',
                  'UNKNOWN-FIELD-SECRET'):
        assert value not in disk and value not in copied
    assert 'encoder_error' in disk and 'encoder_error' in copied
    json.loads(disk)
    json.loads(copied)
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600


def test_lifecycle_event_order_and_restart_loading(tmp_path):
    path = tmp_path / 'output.json'
    store = OutputDiagnostics(path, metadata={'version': 'fixture'})
    for event in ('start_request', 'encoder_started', 'encoder_error', 'finished'):
        store.append({'event': event, 'message': 'fixture output lifecycle', 'video_frames': 3})
    assert [row['event'] for row in store.events] == [
        'start_request', 'encoder_started', 'encoder_error', 'finished']
    restored = OutputDiagnostics(path, metadata={'version': 'fixture'})
    assert [row['event'] for row in restored.events] == [row['event'] for row in store.events]
    assert restored.events[-1]['event'] == 'finished'


def test_size_and_event_limits_keep_a_valid_recent_diagnostic_tail(tmp_path):
    path = tmp_path / 'output.json'
    store = OutputDiagnostics(path)
    for index in range(MAX_EVENTS + 10):
        store.append({'event': 'status', 'message': f'entry-{index} ' + 'x' * 5000})
    assert path.stat().st_size <= MAX_BYTES
    assert 0 < len(store.events) <= MAX_EVENTS
    assert f'entry-{MAX_EVENTS + 9}' in store.events[-1]['message']
    json.loads(path.read_text())
    json.loads(store.text())
    restored = OutputDiagnostics(path)
    assert 0 < len(restored.events) <= MAX_EVENTS
    assert restored.events[-1]['message'] == store.events[-1]['message']


@pytest.mark.parametrize('payload', ['not JSON', '[' + 'x' * (MAX_BYTES + 1)])
def test_invalid_or_oversized_prior_file_is_not_loaded(tmp_path, payload):
    path = tmp_path / 'output.json'
    path.write_text(payload)
    store = OutputDiagnostics(path)
    assert list(store.events) == []
    store.append({'event': 'fresh', 'message': 'New diagnostic after invalid old file'})
    assert store.events[-1]['event'] == 'fresh'
    json.loads(store.text())


def test_disk_failure_does_not_lose_current_memory_diagnostics(tmp_path):
    blocked = tmp_path / 'not a directory'
    blocked.write_bytes(b'preserve blocking file')
    store = OutputDiagnostics(blocked / 'output.json')
    store.append({'event': 'encoder_error', 'message': 'Actionable fixture failure'})
    assert store.persistence_error
    assert store.events[-1]['message'] == 'Actionable fixture failure'
    assert 'Actionable fixture failure' in store.text()
    assert blocked.read_bytes() == b'preserve blocking file'


def test_actual_copy_button_copies_safe_lifecycle_after_finished(desk, monkeypatch):
    window, _, _ = desk
    secret = 'CLIPBOARD-KEY'
    target = 'rtmps://user:CLIPBOARD-PASSWORD@host.invalid/app/CLIPBOARD-PATH?token=CLIPBOARD-QUERY'
    window.output_mode.setCurrentIndex(1)
    window.destination.setText(target)
    window.stream_key.setText(secret)
    starts = []
    monkeypatch.setattr(window.stage, 'render_frame', lambda *args: None)
    monkeypatch.setattr(window.export, 'set_video_frame', lambda *args: None)
    monkeypatch.setattr(window, 'toggle_play', lambda: None)

    def start(config):
        starts.append(config)
        window.export.status.emit('Encoder fixture starting')

    monkeypatch.setattr(window.export, 'start', start)
    window.start_output.click()
    assert len(starts) == 1
    window.export.error.emit(f'Encoder fixture failed {target} key={secret} token=UNLISTED-QUERY')
    window.export.status.emit('Stream stopped')
    window.export.finished.emit()
    QApplication.clipboard().setText('previous clipboard fixture')
    window.copy_output_diagnostics_button.click()
    copied = QApplication.clipboard().text()
    json.loads(copied)
    assert 'Encoder fixture starting' in copied and 'Encoder fixture failed' in copied
    assert 'Stream stopped' in copied
    for value in (secret, target, 'CLIPBOARD-PASSWORD', 'CLIPBOARD-PATH', 'CLIPBOARD-QUERY', 'UNLISTED-QUERY'):
        assert value not in copied
    assert window.output_diagnostics.events
    assert 'Encoder fixture failed' in window.broadcast_label.text()


def test_actual_worker_diagnostic_signal_preserves_safe_exit_evidence_on_disk_and_copy(desk):
    window, _, _ = desk
    secret = 'WORKER-RAW-KEY'
    target = 'rtmps://user:WORKER-PASSWORD@host.invalid/app/WORKER-PATH?token=WORKER-QUERY'
    window.output_mode.setCurrentIndex(1)
    window.destination.setText(target)
    window.stream_key.setText(secret)
    window.export.diagnostic.emit({
        'event': 'encoder_exit', 'returncode': 1, 'reason': 'encoder_failure',
        'timestamp': 1234567890.25, 'next_attempt': 2,
        'stderr_tail': f'Encoder fixture stderr: {target} raw={secret} token=UNLISTED-WORKER-TOKEN',
        'local_feed_errors': [f'feed{index}: {secret} ' + 'x' * 600 for index in range(6)],
        'counters': {'delivery_lag_seconds': 2.277, 'running': False,
                     'endpoint': secret, 'audio_frames': float('inf'), 'video_frames': '3'},
    })
    window.copy_output_diagnostics_button.click()
    copied = QApplication.clipboard().text()
    row = [event for event in json.loads(copied)['events'] if event['event'] == 'encoder_exit'][-1]
    assert row['reason'] == 'encoder_failure' and row['returncode'] == 1
    assert row['timestamp'] == 1234567890.25 and row['next_attempt'] == 2
    assert 'Encoder fixture stderr' in row['stderr_tail']
    assert row['counters'] == {'delivery_lag_seconds': 2.277, 'running': False}
    assert len(row['local_feed_errors']) == 4
    assert all(len(value) <= 400 for value in row['local_feed_errors'])
    assert row['local_feed_errors'][0].startswith('feed2:')
    disk = window.output_diagnostics.path.read_text()
    for value in (secret, target, 'WORKER-PASSWORD', 'WORKER-PATH',
                  'WORKER-QUERY', 'UNLISTED-WORKER-TOKEN'):
        assert value not in copied and value not in disk
    stored = [event for event in json.loads(disk)['events'] if event['event'] == 'encoder_exit'][-1]
    assert stored['stderr_tail'] == row['stderr_tail']
    assert stored['local_feed_errors'] == row['local_feed_errors']
    assert stored['counters'] == row['counters']
