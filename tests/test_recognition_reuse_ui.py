"""Fresh/reuse choice is explicit, persistent, per station and worker-bound."""
import json
import os
from unittest.mock import patch
import pytest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from gnb_player.app import MainWindow
from gnb_player import extract_worker
from gnb_player.extraction import ExtractionConfig, build_command
from test_app import window, add_fixture_tracks


@pytest.mark.parametrize('enabled',[False,True])
def test_cache_choice_persists_and_is_explicitly_applied_to_next_job(window,tmp_path,monkeypatch,enabled):
    app=QApplication.instance();window.show()
    window.tabs.setCurrentIndex(next(i for i in range(window.tabs.count()) if window.tabs.tabText(i)=='Lyric models'))
    app.processEvents()
    assert window.reuse_recognition.isVisible()
    if window.reuse_recognition.isChecked()!=enabled:
        QTest.mouseClick(window.reuse_recognition,Qt.MouseButton.LeftButton,pos=QPoint(10,window.reuse_recognition.height()//2))
    assert window.reuse_recognition.isChecked() is enabled
    assert window.save_model_settings() and window.save_session()
    other=MainWindow(tmp_path/'other');restored=MainWindow(window.data_dir)
    try:
        assert restored.reuse_recognition.isChecked() is enabled
        assert restored.extract_config.reuse_recognition is enabled
        assert other.extract_config.reuse_recognition is True
    finally:restored.close();other.close();app.processEvents()
    track=add_fixture_tracks(window,tmp_path,1)[0];window.playlist.setCurrentRow(0)
    # The operator action now enters a modal assisted review, not the legacy
    # queue. Test routing without opening a real dialog or starting a model.
    review_calls=[]
    monkeypatch.setattr(window,'open_assisted_review',lambda **kwargs:review_calls.append(kwargs))
    window.queue_extraction()
    assert review_calls==[{'supplied_text':None,'prepare':True}]
    # The legacy extraction config still owns this cache preference.
    args=build_command(track.path,tmp_path/'not-executed.json',window.extract_config)
    config=ExtractionConfig.from_dict(json.loads(args[args.index('--config-json')+1]))
    assert config.reuse_recognition is enabled
    with patch.dict(os.environ,{},clear=True):
        extract_worker.prepare_environment(config)
        assert os.environ['GNB_LYRICS_FRESH_RECOGNITION']==('0' if enabled else '1')


def test_old_config_retains_default_and_non_boolean_is_rejected():
    assert ExtractionConfig.from_dict({}).reuse_recognition is True
    assert ExtractionConfig.from_dict({'reuse_recognition':False}).reuse_recognition is False
    with pytest.raises(ValueError):ExtractionConfig.from_dict({'reuse_recognition':'false'})
