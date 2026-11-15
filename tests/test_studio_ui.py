import json,unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox
from PySide6.QtCore import QProcess
from gnb_player.app import MainWindow
from gnb_player import studio_settings as s
from test_studio import StudioTests

class StudioUITests(StudioTests):
    def setUp(self):
        StudioTests.setUp(self)
        self.app=QApplication.instance() or QApplication([])
    def test_isolated_startup_export_import_reload(self):
        backup,_=self.backup();files,_,_=s.prepare_import(backup,self.target);s.import_studio(self.target,files)
        w=MainWindow(self.target)
        try:
            self.assertEqual(w.tabs.tabText(4),'Settings backup');self.assertEqual(w.brand.text(),'TEST')
            self.assertEqual(w.volume.value(),43);self.assertFalse(w.shuffle.isChecked());self.assertEqual(len(w.all_tracks),1)
            self.assertFalse(w.engine.playing);self.assertFalse(w.export.running);self.assertEqual(w.extract_process.state(),QProcess.ProcessState.NotRunning)
            w.brand.setText('Changed in UI');w.volume.setValue(61);w.stream_key.setText('DO_NOT_EXPORT')
            exported=self.root/'ui-backup.json'
            with patch.object(QFileDialog,'getSaveFileName',return_value=(str(exported),'')):w.export_studio_settings()
            data=s.read_json(exported);self.assertEqual(data['files']['session.json']['settings']['brand'],'Changed in UI');self.assertEqual(data['files']['settings.json']['studio_ui']['volume'],61)
            self.assertNotIn('DO_NOT_EXPORT',exported.read_text())
            w.brand.setText('Temporary');w.volume.setValue(13)
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(exported),'')),patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):w.import_studio_settings()
            self.assertEqual(w.brand.text(),'Changed in UI');self.assertEqual(w.volume.value(),61);self.assertEqual(w.stream_key.text(),'')
            self.assertEqual(s.read_json(self.target/'settings.json')['plugin_extra'],self.files['settings.json']['plugin_extra'])
            self.assertTrue(w.save_session());self.assertEqual(s.read_json(self.target/'session.json')['playlist_extra'],'keep');self.assertEqual(s.read_json(self.target/'session.json')['tracks'][0]['custom_tag'],'keep')
            before={p.name:p.read_bytes() for p in self.target.glob('*.json')};exported.write_text('{broken')
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(exported),'')):w.import_studio_settings()
            self.assertEqual(w.brand.text(),'Changed in UI');self.assertEqual(before,{p.name:p.read_bytes() for p in self.target.glob('*.json')})
        finally:w.close()
        w2=MainWindow(self.target)
        try:self.assertEqual(w2.brand.text(),'Changed in UI');self.assertEqual(w2.volume.value(),61);self.assertFalse(w2.engine.playing);self.assertFalse(w2.export.running)
        finally:w2.close()

if __name__=='__main__':
    suite=unittest.TestSuite([StudioUITests('test_isolated_startup_export_import_reload')])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
