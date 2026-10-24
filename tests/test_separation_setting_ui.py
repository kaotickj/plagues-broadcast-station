"""Operator choice persists per station and reaches new jobs; no inference."""
import json
import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from gnb_player.app import MainWindow
from gnb_player.extraction import build_command
from test_app import window, add_fixture_tracks


@pytest.mark.parametrize('enabled', [False, True])
def test_separation_choice_visible_save_reopen_and_other_station_unchanged(window,tmp_path,enabled):
    app=QApplication.instance()
    window.show()
    window.tabs.setCurrentIndex(next(i for i in range(window.tabs.count()) if window.tabs.tabText(i)=='Lyric models'))
    app.processEvents()
    assert window.separate_vocals.isVisible()
    assert window.separate_vocals.accessibleName()=='Separate vocals before recognition'
    assert window.separate_vocals.isChecked()==window.extract_config.separate_vocals
    if window.separate_vocals.isChecked()!=enabled:
        QTest.mouseClick(window.separate_vocals,Qt.MouseButton.LeftButton,pos=QPoint(10,window.separate_vocals.height()//2))
    assert window.separate_vocals.isChecked() is enabled
    assert window.save_model_settings() and window.save_session()
    reopened=MainWindow(window.data_dir);other=MainWindow(tmp_path/'unrelated-station')
    try:
        assert reopened.extract_config.separate_vocals is enabled
        assert reopened.separate_vocals.isChecked() is enabled
        assert other.extract_config.separate_vocals is True
        # Config synchronization must retain a false restored/setup value.
        reopened._sync_model_fields()
        assert reopened.separate_vocals.isChecked() is enabled
    finally:
        reopened.close();other.close();app.processEvents()


@pytest.mark.parametrize('enabled',[False,True])
def test_extract_button_path_applies_current_choice_to_new_worker(window,tmp_path,monkeypatch,enabled):
    track=add_fixture_tracks(window,tmp_path,1)[0]
    window.playlist.setCurrentRow(0)
    window.separate_vocals.setChecked(enabled)
    # Leave jobs queued; no model process may launch during this GUI test.
    monkeypatch.setattr(window,'_next_extraction',lambda:None)
    window.queue_extraction()
    assert window._extract_queue==[(track,'')]
    assert window.extract_config.separate_vocals is enabled
    command=build_command(track.path,tmp_path/'isolated-output.json',window.extract_config)
    config=json.loads(command[command.index('--config-json')+1])
    assert config['separate_vocals'] is enabled
