import base64,copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from gnb_player import studio_settings as s
from gnb_player.extraction import ExtractionConfig
from gnb_player.settings import atomic_json

class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source';self.target=self.root/'target';self.source.mkdir();self.target.mkdir()
        self.files={
            'settings.json':{'extraction':ExtractionConfig(gpu_python_path='/missing/python',vulkan_binary='/missing/whisper',gpu_lock='').to_dict(),'plugin_extra':{'theme':'amber','nested':[1,True]},'studio_ui':{'volume':43,'shuffle':False,'repeat':True,'offset':1.2,'output_mode':0,'broadcast_preview':True,'recording_destination':'/missing/recording.mp4'}},
            'session.json':{'version':1,'name':'Station','tracks':[{'path':'song.wav','title':'Title','artist':'Artist','album':'Album','duration':10,'custom_tag':'keep'}],'backgrounds':['art.png'],'lyrics':{'song.wav':'song.lyrics.json'},'settings':{'brand':'TEST','tagline':'Hello','interval_seconds':12,'motion':True,'waveform':False,'lyrics':True,'lyrics_ready_only':False,'waveform_color':'#abcdef','custom_visual':'keep'},'playlist_extra':'keep'},
            'broadcast-settings.json':{'schema':1,'destination':'rtmp://example.test/live','picture_index':1,'bitrate_kbps':28500,'remember':True,'broadcast_extra':'keep'},
            'lyrics-review-runtime.json':{'backend':'heart_ctc','align_python':'/missing/review/python','timeout_seconds':480,'custom_review':True}}
        for name,data in self.files.items():atomic_json(self.source/name,data)
    def backup(self):
        path=self.root/'backup.json';data=s.make_backup(self.source);s.export_studio(path,data,self.source);return path,data
    def test_full_roundtrip_unknown_fields_and_paths(self):
        path,data=self.backup();prepared,missing,_=s.prepare_import(path,self.target);result=s.import_studio(self.target,prepared)
        self.assertEqual(result,prepared);self.assertEqual(s.read_json(self.target/'settings.json')['plugin_extra'],self.files['settings.json']['plugin_extra'])
        self.assertEqual(result['session.json']['tracks'][0]['custom_tag'],'keep');self.assertEqual(result['session.json']['tracks'][0]['path'],str(self.target/'song.wav'))
        self.assertEqual(result['broadcast-settings.json']['broadcast_extra'],'keep');self.assertFalse(result['broadcast-settings.json']['remember'])
        self.assertTrue(result['lyrics-review-runtime.json']['custom_review']);self.assertIn('/missing/python',missing)
        second=s.make_backup(self.target)
        self.assertEqual(second['files'],result)
    def test_source_settings_unchanged_by_export(self):
        before={p.name:p.read_bytes() for p in self.source.glob('*.json')};self.backup()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.source.glob('*.json')})
    def test_credentials_and_embedded_urls_are_excluded(self):
        config=self.files['settings.json'];config['extension']={'api_key':'SECRET','value':'https://a.test/path?token=SECRET','safe':'keep','nested':{'password':'SECRET'}}
        atomic_json(self.source/'settings.json',config)
        atomic_json(self.source/'broadcast-profile.json',{'credential_revision':'SECRET'})
        stream=self.files['broadcast-settings.json'];stream['destination']='rtmp://example.test/live/SECRET';atomic_json(self.source/'broadcast-settings.json',stream)
        _,data=self.backup();encoded=json.dumps(data)
        self.assertNotIn('SECRET',encoded);self.assertNotIn('broadcast-profile.json',data['files']);self.assertEqual(data['files']['broadcast-settings.json']['destination'],'')
        self.assertEqual(data['files']['settings.json']['extension']['safe'],'keep')
    def test_existing_unrelated_and_secret_fields_preserved(self):
        atomic_json(self.target/'settings.json',{'old_extension':{'keep':7},'api_key':'LOCAL_SECRET'})
        path,_=self.backup();prepared,_,_=s.prepare_import(path,self.target);s.import_studio(self.target,prepared)
        result=s.read_json(self.target/'settings.json');self.assertEqual(result['old_extension'],{'keep':7});self.assertEqual(result['api_key'],'LOCAL_SECRET')
    def test_complete_validation_precedes_writes(self):
        path,data=self.backup();data['files']['session.json']['tracks'][0]['duration']='bad';path.write_text(json.dumps(data))
        atomic_json(self.target/'sentinel.json',{'keep':True})
        before=list(self.target.iterdir())
        with self.assertRaises(ValueError):s.prepare_import(path,self.target)
        self.assertEqual(before,list(self.target.iterdir()))
    def test_mid_commit_failure_rolls_back_bytes(self):
        for name,data in self.files.items():atomic_json(self.target/name,data)
        before={p.name:p.read_bytes() for p in self.target.glob('*.json')}
        path,_=self.backup();prepared,_,_=s.prepare_import(path,self.target)
        real=s.atomic_json
        def fail(path,data):
            if Path(path)==self.target/'session.json':raise OSError('disk failure')
            return real(path,data)
        with patch.object(s,'atomic_json',side_effect=fail):
            with self.assertRaises(OSError):s.import_studio(self.target,prepared)
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.target.glob('*.json')});self.assertFalse((self.target/'.studio-import-journal.json').exists())
    def test_interrupted_transaction_recovers(self):
        before={name:base64.b64encode(json.dumps(data).encode()).decode() for name,data in self.files.items()}
        atomic_json(self.target/'.studio-import-journal.json',before);atomic_json(self.target/'settings.json',{'half':'written'})
        s.recover_import(self.target)
        for name,value in self.files.items():self.assertEqual(s.read_json(self.target/name),value)
        self.assertFalse((self.target/'.studio-import-journal.json').exists())
    def test_foreign_home_mapping_and_missing_path_retention(self):
        path,data=self.backup();data['source']={'home':'C:\\Users\\Jay','data_dir':'C:\\Users\\Jay\\AppData\\Station','platform':'nt'}
        data['files']['settings.json']['extraction']['gpu_python_path']='C:\\Users\\Jay\\runtimes\\python.exe'
        data['files']['session.json']['tracks'][0]['path']='C:\\Users\\Jay\\Music\\song.wav';path.write_text(json.dumps(data))
        files,missing,_=s.prepare_import(path,self.target,home_root=self.root/'new-home')
        self.assertEqual(files['settings.json']['extraction']['gpu_python_path'],str(self.root/'new-home/runtimes/python.exe'))
        self.assertEqual(files['session.json']['tracks'][0]['path'],str(self.root/'new-home/Music/song.wav'));self.assertTrue(missing)
    def test_untrusted_secret_backup_rejected(self):
        path,data=self.backup();data['files']['settings.json']['api_key']='DO_NOT_IMPORT';path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):s.prepare_import(path,self.target)
    def test_no_live_file_export_overwrite(self):
        data=s.make_backup(self.source);before=(self.source/'settings.json').read_bytes()
        with self.assertRaises(ValueError):s.export_studio(self.source/'settings.json',data,self.source)
        self.assertEqual(before,(self.source/'settings.json').read_bytes())
    def test_profile_save_preserves_extensions_without_credentials(self):
        from gnb_player.broadcast_profile import BroadcastProfileStore,StreamProfile
        atomic_json(self.target/'broadcast-settings.json',{'schema':1,'destination':'','picture_index':0,'bitrate_kbps':4500,'remember':False,'extension':{'keep':1}})
        store=BroadcastProfileStore(self.target,backend_factory=lambda: self.fail('Keyring must not be accessed'))
        self.assertEqual(store.load_settings().profile.destination,'')
        store.save_settings(StreamProfile('rtmp://example.test/live','',1,28500),remember=False)
        self.assertEqual(s.read_json(self.target/'broadcast-settings.json')['extension'],{'keep':1})

    def test_duplicate_fields_rejected(self):
        p=self.root/'bad.json';p.write_text('{"kind":"a","kind":"b"}')
        with self.assertRaises(ValueError):s.prepare_import(p,self.target)

if __name__=='__main__':unittest.main()
