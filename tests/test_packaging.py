from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def read_spec(self):
        captured={}
        def analysis(*args,**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(pure=[],scripts=args[0],binaries=[],datas=list(kwargs['datas']))
        namespace={'SPECPATH':str(ROOT),'Analysis':analysis,'PYZ':lambda *a,**kw:None,
                   'EXE':lambda *a,**kw:None,'COLLECT':lambda *a,**kw:None}
        exec(compile((ROOT/'PlaguesBroadcastStation.spec').read_text(),str(ROOT/'PlaguesBroadcastStation.spec'),'exec'),namespace)
        return captured

    def test_external_worker_sources_are_data_files_without_model_weights(self):
        spec=self.read_spec();data=spec['datas']
        names={Path(source).name for source,destination in data}
        for name in ['extract_worker.py','extraction.py','model_install.py','model_install_worker.py','model_discovery.py',
                     'lyrics_pipeline.py','lyrics_acceleration.py','lyrics_gpu_helper.py','lyrics_review.py','lyrics_quality.py']:
            self.assertIn(name,names)
        self.assertTrue(all(Path(source).exists() for source,destination in data))
        self.assertFalse(any(any(part in source for part in ['media-packs','/private/','/models/']) for source,destination in data))

    def test_gui_bundle_keeps_multimedia_and_excludes_unrelated_heavy_modules(self):
        spec=self.read_spec()
        self.assertIn('PySide6.QtMultimedia',spec['hiddenimports'])
        self.assertNotIn('PySide6.QtMultimedia',spec['excludes'])
        self.assertIn('PySide6.QtWebEngineCore',spec['excludes'])
        self.assertIn('torch',spec['excludes'])
        self.assertEqual([destination for source,destination in spec['binaries']],['bin','bin'])
        self.assertIn('THIRD_PARTY_NOTICES.md',{Path(source).name for source,destination in spec['datas']})

    def test_entrypoint_can_bootstrap_from_another_working_directory(self):
        with patch.dict(sys.modules,{'gnb_player.app':SimpleNamespace(main=lambda argv=None:73)}):
            with self.assertRaises(SystemExit) as result:runpy.run_path(str(ROOT/'launch.py'),run_name='__main__')
        self.assertEqual(result.exception.code,73)

    def test_worker_dispatch_does_not_import_the_gui(self):
        from gnb_player.__main__ import main
        received=[]
        worker=SimpleNamespace(main=lambda argv:received.append(argv) or 74)
        with patch.dict(sys.modules,{'gnb_player.model_install_worker':worker,'gnb_player.app':None}):
            self.assertEqual(main(['--model-install-worker','--result-file','result.json']),74)
        self.assertEqual(received,[['--result-file','result.json']])

    def test_source_installer_rejects_downloads_without_explicit_install_action(self):
        import json
        with tempfile.TemporaryDirectory(prefix='station no downloads ') as temporary:
            root=Path(temporary);events=root/'setup events.jsonl';result=root/'setup result.json'
            process=subprocess.run([sys.executable,str(ROOT/'launch.py'),'--model-install-worker',
                                    '--events-file',str(events),'--result-file',str(result)],
                                   cwd=temporary,capture_output=True,text=True,errors='replace',timeout=20)
            self.assertEqual(process.returncode,1,process.stderr)
            reports=[json.loads(line) for line in events.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(reports[-1]['type'],'error')
            self.assertIn('explicit Install action',reports[-1]['message'])
            self.assertFalse(result.exists())

    @unittest.skipUnless(os.name!='nt' and shutil.which('bash'),'POSIX launcher')
    def test_linux_launcher_quotes_paths_and_preserves_app_session_selection(self):
        with tempfile.TemporaryDirectory(prefix='station with spaces ') as folder:
            root=Path(folder);shutil.copy2(ROOT/'run-linux.sh',root/'run-linux.sh');(root/'_internal').mkdir()
            binary=root/'PlaguesBroadcastStation';binary.write_text('#!/bin/sh\n: > "$PBS_TEST_CAPTURE"\nfor pbs_argument in "$@"; do printf \'%s\\n\' "$pbs_argument" >> "$PBS_TEST_CAPTURE"; done\n');binary.chmod(0o755)
            profile=root/'media-packs/goodneighbor/profile.json';profile.parent.mkdir(parents=True);profile.write_text('{}')
            capture=root/'arguments.txt';env={**os.environ,'PBS_TEST_CAPTURE':str(capture)}
            subprocess.run(['bash',str(root/'run-linux.sh')],check=True,env=env)
            self.assertEqual(capture.read_text(), '')
            subprocess.run(['bash',str(root/'run-linux.sh'),'--profile','a chosen profile.json'],check=True,env=env)
            self.assertEqual(capture.read_text().splitlines(),['--profile','a chosen profile.json'])


if __name__=='__main__':unittest.main()
