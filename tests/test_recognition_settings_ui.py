import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication
from gnb_player.app import MainWindow


def test_explicit_recognition_options_save_reopen_and_reject_invalid_language(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path)
    try:
        window.model_fields['recognition_language'].setText('auto')
        window.model_fields['recognition_prompt'].setText('Explicit user context')
        assert window.save_model_settings()
        assert window.save_session()
        restored = MainWindow(tmp_path)
        try:
            assert restored.extract_config.recognition_language == 'auto'
            assert restored.model_fields['recognition_prompt'].text() == 'Explicit user context'
            restored.model_fields['recognition_language'].setText('not-a-language')
            assert not restored.save_model_settings()
            assert restored.extract_config.recognition_language == 'auto'
        finally:
            restored.close()
    finally:
        window.close()
        app.processEvents()
