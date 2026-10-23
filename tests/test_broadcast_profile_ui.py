"""Fake credentials and exports only: no streams, native keyring, or user data."""
import json
import os
from urllib.parse import quote

import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND','ffmpeg')
from PySide6.QtWidgets import QApplication, QLineEdit
from gnb_player.app import MainWindow
from gnb_player.broadcast import BroadcastConfig, redact
from gnb_player.broadcast_profile import BroadcastProfileStore
from gnb_player.extraction import ExtractionConfig
from test_broadcast_profile import FakeCredentials, profile


@pytest.fixture
def desk(tmp_path,monkeypatch):
    monkeypatch.setattr('gnb_player.app.discover_config',lambda:ExtractionConfig())
    monkeypatch.setattr('gnb_player.app.config_status',lambda config:{'ready':False,'missing_models':[]})
    # No model discovery, home/cache inspection, real credential classes or output.
    app=QApplication.instance() or QApplication([])
    backend=FakeCredentials(); data=tmp_path/'isolated state with spaces'
    store=BroadcastProfileStore(data,backend_factory=lambda:backend)
    window=MainWindow(data,credential_store=store)
    monkeypatch.setattr(window,'_task',lambda fn,done:done(fn()))
    yield window,backend,store
    window._closing=True
    for task in window.tasks:task.wait(5000)
    window.close();app.processEvents()


def fill(window,value=None):
    value=value or profile()
    window.output_mode.setCurrentIndex(1);window.destination.setText(value.destination);window.stream_key.setText(value.stream_key)
    window.resolution.setCurrentIndex(value.picture_index);window.bitrate.setValue(value.bitrate_kbps)


def test_explicit_save_clear_restore_forget_and_no_auto_start(desk,monkeypatch):
    window,backend,store=desk
    starts=[];monkeypatch.setattr(window.export,'start',lambda config:starts.append(config))
    assert not backend.calls and window.stream_key.echoMode()==QLineEdit.EchoMode.Password
    fill(window);window.save_stream_button.click()
    assert 'Saved securely' in window.stream_profile_status.text()
    window.clear_stream_button.click()
    assert not window.stream_key.text() and store.marker.exists()
    window.restore_stream_button.click()
    assert window.destination.text()==profile().destination and window.stream_key.text()==profile().stream_key
    assert window.resolution.currentIndex()==1 and window.bitrate.value()==6500 and not starts
    window.forget_stream_button.click()
    assert 'Forgot both' in window.stream_profile_status.text()
    assert not store.marker.exists() and not backend.values and not window.stream_key.text() and not window.destination.text()


def test_restart_restores_only_saved_active_profile(desk,monkeypatch):
    window,backend,store=desk
    fill(window);window.save_stream_profile()
    original_task=MainWindow._task
    monkeypatch.setattr(MainWindow,'_task',lambda self,fn,done:done(fn()))
    restored=MainWindow(window.data_dir,credential_store=BroadcastProfileStore(window.data_dir,backend_factory=lambda:backend))
    assert restored.stream_key.text()==profile().stream_key and restored.output_mode.currentIndex()==1
    assert not restored.export.running and 'Restored securely' in restored.stream_profile_status.text()
    restored._closing=True;restored.close()
    monkeypatch.setattr(MainWindow,'_task',original_task)


def test_failed_update_and_forget_keep_honest_status_and_session_fields(desk):
    window,backend,store=desk
    fill(window);window.save_stream_profile();old=store.marker.read_bytes()
    window.stream_key.setText('REPLACEMENT');backend.fail='set';window.save_stream_profile()
    assert 'Saved securely' not in window.stream_profile_status.text()
    assert 'session-only' in window.stream_profile_status.text() and store.marker.read_bytes()==old
    assert window.stream_key.text()=='REPLACEMENT'
    backend.fail='delete';window.forget_stream_profile()
    assert 'Could not fully forget' in window.stream_profile_status.text() and window.stream_key.text()=='REPLACEMENT'
    assert 'SENTINEL' not in window.stream_profile_status.text()
    backend.fail=None;window.restore_stream_profile();assert window.stream_key.text()==profile().stream_key


def test_busy_native_prompt_disables_start_and_mutation_until_done(desk,monkeypatch):
    window,backend,store=desk
    pending=[];monkeypatch.setattr(window,'_task',lambda fn,done:pending.append((fn,done)))
    fill(window);window.save_stream_profile()
    assert window._profile_busy and not window.start_output.isEnabled() and not window.stream_key.isEnabled()
    assert not window.forget_stream_button.isEnabled()
    window.start_broadcast();assert not window.export.running
    window.clear_stream_fields();assert window.stream_key.text()==profile().stream_key
    fn,done=pending.pop();done(fn())
    assert not window._profile_busy and window.start_output.isEnabled()
    monkeypatch.setattr(type(window.export),'running',property(lambda self:True))
    window._profile_controls();assert not window.save_stream_button.isEnabled()
    before=list(backend.calls);window.forget_stream_profile();assert backend.calls==before
    # Let fixture close without an output-stop dialog.
    monkeypatch.setattr(type(window.export),'running',property(lambda self:False))


def test_portable_exports_settings_and_lyrics_never_receive_credentials(desk,tmp_path):
    window,backend,store=desk
    window.settings_file.write_text(json.dumps({'unrelated':{'preserve':True},'extraction':{}}))
    lyrics=window.data_dir/'lyrics-cache'/'keep.json';lyrics.parent.mkdir();lyrics.write_text('original lyric QC')
    before=(lyrics.read_bytes(),lyrics.stat().st_mtime_ns)
    fill(window);window.save_stream_profile();assert window.save_session()
    from gnb_player.settings import write_profile
    exported=tmp_path/'portable-playlist.json'
    write_profile(exported,window.all_tracks,window.backgrounds,window.lyric_files,window.visual_settings(),window.profile_name)
    public=window.settings_file.read_text()+(window.data_dir/'session.json').read_text()+exported.read_text()+store.marker.read_text()
    for secret in ('PRIVATE-KEY','password','FULL-URL-KEY','QUERY-SECRET','host.invalid'):
        assert secret not in public
    assert json.loads(window.settings_file.read_text())['unrelated']=={'preserve':True}
    assert (lyrics.read_bytes(),lyrics.stat().st_mtime_ns)==before


@pytest.mark.parametrize('key',['X','XY','XYZ','LONG-SECRET','K/ey%+#'])
def test_short_encoded_keys_and_synchronous_start_errors_are_redacted(desk,monkeypatch,key):
    window,_,_=desk
    target='rtmps://private.invalid/app/INLINE-SECRET?token=QUERY-SECRET'
    fill(window,profile(key,target))
    def fail(config):raise RuntimeError(f'failed {config.target} key={key} encoded={quote(key,safe="")} token=QUERY-SECRET')
    monkeypatch.setattr(window.export,'start',fail)
    window.start_broadcast()
    text=window.statusBar().currentMessage()
    assert key not in text and quote(key,safe='') not in text and 'INLINE-SECRET' not in text and 'QUERY-SECRET' not in text
    assert key not in repr(BroadcastConfig(mode='stream',target=target,secrets=(key,)))
    window._broadcast_error(f'{target} key={key}')
    assert key not in window.statusBar().currentMessage()
    window._broadcast_status(f'{target} key={key}')
    assert key not in window.broadcast_label.text()
    window._broadcast_statistics({'url':target,'unknown':key,'video_frames':key,'reconnects':1})
    assert key not in window.broadcast_stats.text() and 'reconnects: 1' in window.broadcast_stats.text()


def test_recording_validation_still_refuses_overwrite(desk,tmp_path,monkeypatch):
    window,_,_=desk
    path=tmp_path/'existing.mp4';path.write_bytes(b'keep')
    window.output_mode.setCurrentIndex(0);window.destination.setText(str(path))
    window.start_broadcast()
    assert path.read_bytes()==b'keep' and not window.export.running
    assert 'already exists' in window.statusBar().currentMessage()
    window.save_stream_profile();assert 'Choose Direct livestream' in window.stream_profile_status.text()


def test_new_url_fragments_are_redacted_after_a_previous_stream_config(desk,monkeypatch):
    window,_,_=desk
    window.export._config=BroadcastConfig(mode='stream',target='rtmps://old.invalid/app/OLD',secrets=('OLD',))
    target='rtmps://newuser:NEW-PASSWORD@new.invalid/application/NEW-INLINE?token=NEW-QUERY'
    fill(window,profile('NEW-KEY',target))
    def fail(config):raise RuntimeError('partial NEW-PASSWORD NEW-INLINE NEW-QUERY NEW-KEY')
    monkeypatch.setattr(window.export,'start',fail)
    window.start_broadcast()
    text=window.statusBar().currentMessage()
    for secret in ('NEW-PASSWORD','NEW-INLINE','NEW-QUERY','NEW-KEY'):assert secret not in text
