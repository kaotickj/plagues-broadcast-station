import ast, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from gnb_player.settings import save_model_preferences
from gnb_player.extraction import ExtractionConfig, resolve_existing_models, LINUX_SHARED_PATHS

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'settings.json'
    def seed(self,values):
        self.path.write_text(json.dumps({'other':{'preserve':True},'extraction':values}))
    def test_save_reload(self):
        before=ExtractionConfig().to_dict(); self.seed(before)
        desired={**before,'gpu_python_path':'/saved/python','vulkan_binary':'/saved/whisper','gpu_lock':'','ollama_model':'gemma4:12b-it-q8_0'}
        result=save_model_preferences(self.path,before,desired)
        self.assertEqual(ExtractionConfig.from_dict(json.loads(self.path.read_text())['extraction']).to_dict(),desired)
        self.assertEqual(result,desired);self.assertEqual(json.loads(self.path.read_text())['other'],{'preserve':True})
    def test_stale_unmodified_fields_preserved(self):
        self.seed({'gpu_python_path':'/new','engine':'large-v3','future':True})
        result=save_model_preferences(self.path,{'gpu_python_path':'/old','engine':'large-v3'},{'gpu_python_path':'/old','engine':'medium.en'})
        self.assertEqual(result,{'gpu_python_path':'/new','engine':'medium.en','future':True})
    def test_conflict_does_not_write(self):
        self.seed({'gpu_python_path':'/new'}); previous=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'another instance'):save_model_preferences(self.path,{'gpu_python_path':'/old'},{'gpu_python_path':'/third'})
        self.assertEqual(self.path.read_bytes(),previous)
    def test_corruption_does_not_write(self):
        self.path.write_text('{broken')
        with self.assertRaises(ValueError):save_model_preferences(self.path,{}, {'python_path':'/new'})
        self.assertEqual(self.path.read_text(),'{broken')
    def test_missing_historical_paths_retained(self):
        config=ExtractionConfig(gpu_python_path=str(LINUX_SHARED_PATHS['gpu_python_path']),vulkan_binary=str(LINUX_SHARED_PATHS['vulkan_binary']))
        with patch('gnb_player.extraction._executable',return_value=False):
            saved,report=resolve_existing_models(config)
        self.assertEqual(saved.gpu_python_path,config.gpu_python_path)
        self.assertEqual(saved.vulkan_binary,config.vulkan_binary)
        self.assertEqual(saved.gpu_lock,'')
    def test_missing_custom_paths_retained(self):
        config=ExtractionConfig(gpu_python_path='/custom/missing',vulkan_binary='/custom/whisper')
        saved,_=resolve_existing_models(config)
        self.assertEqual(saved.gpu_python_path,config.gpu_python_path);self.assertEqual(saved.vulkan_binary,config.vulkan_binary)
    def test_session_save_never_writes_model_settings(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'src/gnb_player/app.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
        node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='save_session')
        from gnb_player.studio_settings import studio_lock, snapshot_session
        from gnb_player.settings import atomic_json
        ns={'studio_lock':studio_lock,'snapshot_session':snapshot_session,'atomic_json':atomic_json};exec(compile(ast.Module(body=[node],type_ignores=[]),'app.py','exec'),ns)
        self.seed({'gpu_python_path':'/new'});before=self.path.read_bytes()
        window=SimpleNamespace(data_dir=Path(self.tmp.name),all_tracks=[],backgrounds=[],lyric_files={},visual_settings=lambda:{},profile_name='test',_message=lambda x:self.fail(x))
        self.assertTrue(ns['save_session'](window));self.assertEqual(self.path.read_bytes(),before)
    def test_save_button_persists_synchronously(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'src/gnb_player/app.py').read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
        node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='save_model_settings')
        ns={'ExtractionConfig':ExtractionConfig,'resolve_existing_models':lambda c:(c,{})};exec(compile(ast.Module(body=[node],type_ignores=[]),'app.py','exec'),ns)
        config=ExtractionConfig();self.seed(config.to_dict())
        w=SimpleNamespace(extract_config=config,model_fields={'gpu_python_path':SimpleNamespace(text=lambda:'/saved/python')},gpu=SimpleNamespace(isChecked=lambda:True),separate_vocals=SimpleNamespace(isChecked=lambda:False),reuse_recognition=SimpleNamespace(isChecked=lambda:True),_edited_model_paths={'gpu_python_path'},_sync_model_fields=lambda:None,update_model_status=lambda:None,_message=lambda x:self.fail(x))
        w._persist_model_settings=lambda:save_model_preferences(self.path,config.to_dict(),w.extract_config.to_dict())
        self.assertTrue(ns['save_model_settings'](w));self.assertEqual(json.loads(self.path.read_text())['extraction']['gpu_python_path'],'/saved/python')
class BackupTests(unittest.TestCase):
    setUp=PersistenceTests.setUp
    seed=PersistenceTests.seed
    def test_backup_roundtrip_and_no_credentials(self):
        from gnb_player.settings import export_model_preferences,import_model_preferences
        values={**ExtractionConfig().to_dict(),'stream_key':'SECRET','password':'SECRET'}
        export_model_preferences(self.path,values)
        self.assertNotIn('SECRET',self.path.read_text())
        self.assertEqual(import_model_preferences(self.path).to_dict(),ExtractionConfig().to_dict())
    def test_bad_backup_preserves_settings(self):
        from gnb_player.settings import export_model_preferences,import_model_preferences
        backup=Path(self.tmp.name)/'backup.json';export_model_preferences(backup,ExtractionConfig().to_dict())
        self.seed({'python_path':'/existing'});before=self.path.read_bytes()
        data=json.loads(backup.read_text());data['extraction']['use_gpu']='yes';backup.write_text(json.dumps(data))
        with self.assertRaises(ValueError):import_model_preferences(backup)
        self.assertEqual(self.path.read_bytes(),before)
        data['extraction']['use_gpu']=True;data['extraction']['secret']='bad';backup.write_text(json.dumps(data))
        with self.assertRaises(ValueError):import_model_preferences(backup)
    def test_credential_url_export_rejected(self):
        from gnb_player.settings import export_model_preferences
        with self.assertRaises(ValueError):export_model_preferences(self.path,{**ExtractionConfig().to_dict(),'ollama_url':'http://user:secret@127.0.0.1:11434'})
        self.assertFalse(self.path.exists())
    def test_oversized_backup_rejected(self):
        from gnb_player.settings import import_model_preferences
        self.path.write_bytes(b' '*131073)
        with self.assertRaises(ValueError):import_model_preferences(self.path)

if __name__=='__main__':unittest.main()
