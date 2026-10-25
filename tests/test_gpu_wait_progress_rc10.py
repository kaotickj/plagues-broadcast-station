"""Per-phase progress and bounded rejection diagnostics; no GPU/model inference."""
from contextlib import contextmanager,nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gnb_player.extraction_backend import lyrics_acceleration as gpu


class GpuWaitProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.lock=self.root/'private-fixture.lock';self.lock.touch()
        self.env=patch.dict(os.environ,{'GNB_LYRICS_GPU':'1','GNB_LYRICS_PHASE_GPU':'1',
            'GNB_LYRICS_GPU_LOCK':str(self.lock),'GNB_LYRICS_GPU_WAIT':'1','GNB_LYRICS_GPU_WAIT_SECONDS':'10'},clear=True)
        self.env.start();self.addCleanup(self.env.stop)
        self.capacity=patch.object(gpu,'capacity',return_value={'available':True,'busy':False})
        self.capacity.start();self.addCleanup(self.capacity.stop)

    def test_permission_failure_reports_actionable_fallback_without_wait_or_secret(self):
        events=[]
        with patch.object(gpu.os,'open',side_effect=PermissionError('TOKEN=synthetic-private-secret')), \
                patch.object(gpu.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'coordination is unavailable') as caught:
                with gpu.gpu_phase(lambda *x:events.append(x),'transcribing'):self.fail('Cannot enter GPU phase')
        self.assertIn('Check access to the configured lock file',str(caught.exception))
        self.assertIn('using CPU',events[-1][2]);sleep.assert_not_called()
        self.assertNotIn('synthetic-private-secret',str(events)+str(caught.exception))
        self.assertNotIn('GNB_LYRICS_GPU_RESERVED',os.environ)
        self.assertNotIn('GNB_LYRICS_GPU_BLOCKED',os.environ)

    def test_missing_lock_is_unavailable_not_contention(self):
        with patch.object(gpu.os,'open',side_effect=FileNotFoundError('private-path')):
            with gpu.reservation() as state:
                self.assertFalse(state['available']);self.assertFalse(state['busy'])
                self.assertTrue(state['coordination_unavailable'])
                self.assertNotIn('private-path',state['message'])

    @unittest.skipIf(gpu.fcntl is None,'POSIX private fixture locks')
    def test_contention_is_busy_and_closes_only_its_owned_descriptor(self):
        real_open=os.open;opened=[];original_mode=self.lock.stat().st_mode&0o777
        def open_owned(*args):
            fd=real_open(*args);opened.append(fd);return fd
        with patch.object(gpu.os,'open',side_effect=open_owned), \
                patch.object(gpu.fcntl,'flock',side_effect=BlockingIOError()):
            with gpu.reservation() as state:
                self.assertTrue(state['available']);self.assertTrue(state['busy'])
                self.assertIn('another job holds',state['message'])
                self.assertEqual(os.environ.get('GNB_LYRICS_GPU_BLOCKED'),'1')
                os.fstat(opened[0])
        with self.assertRaises(OSError):os.fstat(opened[0])
        self.assertEqual(self.lock.stat().st_mode&0o777,original_mode)

    def test_missing_lock_api_is_actionable_without_wait(self):
        with patch.object(gpu,'fcntl',None):
            with gpu.reservation() as state:
                self.assertFalse(state['available']);self.assertFalse(state['busy'])
                self.assertIn('coordination is unavailable',state['message'])

    def test_wait_progress_counts_elapsed_and_remaining_then_clears_on_acquisition(self):
        events=[];states=iter([{'available':True,'busy':True,'message':'Another job holds GPU.'},
                              {'available':True,'busy':False}])
        @contextmanager
        def reservation():yield next(states)
        with patch.object(gpu,'reservation',reservation),patch.object(gpu.time,'monotonic',side_effect=[0,1,2,3,4]), \
                patch.object(gpu.time,'sleep') as sleep:
            with gpu.gpu_phase(lambda *x:events.append(x),'aligning'):events.append(('body',0,'fixture'))
        self.assertEqual(events[0][0],'waiting');self.assertIn('Waited 1s',events[0][2])
        self.assertIn('9s until this attempt is deferred',events[0][2])
        self.assertNotIn('CPU fallback in',events[0][2])
        self.assertEqual(events[1][0],'aligning');self.assertIn('GPU slot acquired',events[1][2])
        self.assertEqual(events[2][0],'body');sleep.assert_called_once()

    def test_requested_wait_timeout_still_defers_without_cpu_execution(self):
        events=[]
        @contextmanager
        def reservation():yield {'available':True,'busy':True}
        with patch.object(gpu,'reservation',reservation),patch.object(gpu.time,'monotonic',side_effect=[0,10]), \
                patch.object(gpu.time,'sleep') as sleep:
            with self.assertRaises(gpu.GpuCapacityDeferred):
                with gpu.gpu_phase(lambda *x:events.append(x),'transcribing'):self.fail('GPU remains busy')
        self.assertIn('remains deferred',events[-1][2]);self.assertIn('10s',events[-1][2])
        self.assertFalse(any('using CPU' in e[2] for e in events));sleep.assert_not_called()

    def test_explicit_cpu_choice_still_skips_wait(self):
        events=[]
        @contextmanager
        def reservation():yield {'available':True,'busy':True}
        with patch.dict(os.environ,{'GNB_LYRICS_GPU_WAIT':'0'}),patch.object(gpu,'reservation',reservation), \
                patch.object(gpu.time,'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError,'CPU fallback explicitly selected'):
                with gpu.gpu_phase(lambda *x:events.append(x),'transcribing'):self.fail('Busy GPU')
        self.assertIn('explicitly selected',events[-1][2]);sleep.assert_not_called()

    @unittest.skipIf(gpu.fcntl is None,'POSIX private fixture locks')
    def test_private_phase_lease_releases_on_error_and_preserves_prior_environment(self):
        with patch.dict(os.environ,{'GNB_LYRICS_GPU_BLOCKED':'prior-value'}):
            with self.assertRaisesRegex(RuntimeError,'fixture interruption'):
                with gpu.gpu_phase(lambda *x:None,'aligning'):
                    with self.lock.open('r+') as other,self.assertRaises(BlockingIOError):
                        gpu.fcntl.flock(other,gpu.fcntl.LOCK_EX|gpu.fcntl.LOCK_NB)
                    raise RuntimeError('fixture interruption')
            self.assertEqual(os.environ['GNB_LYRICS_GPU_BLOCKED'],'prior-value')
        with self.lock.open('r+') as other:gpu.fcntl.flock(other,gpu.fcntl.LOCK_EX|gpu.fcntl.LOCK_NB)
        self.assertNotIn('GNB_LYRICS_GPU_RESERVED',os.environ)

    def test_gpu_helper_announces_actual_backend_before_mock_process(self):
        executable=self.root/'existing-python';executable.touch();target=self.root/'result.json';target.write_text('[]')
        events=[]
        process=SimpleNamespace(stdout=[],wait=lambda **k:0,kill=lambda:None)
        timer=SimpleNamespace(start=lambda:None,cancel=lambda:None)
        def start(*args,**kwargs):
            self.assertIn('ROCm GPU word-alignment runtime',events[-1][2]);return process
        with patch.object(gpu,'GPU_PYTHON',executable),patch.object(gpu.subprocess,'Popen',side_effect=start), \
                patch.object(gpu.threading,'Timer',return_value=timer):
            gpu._helper_unlocked('align',self.root/'input.wav',target,self.root,lambda *x:events.append(x))
        self.assertEqual(events[0][0],'aligning')

    def test_one_window_independent_vulkan_review_announces_review_not_primary(self):
        executable=self.root/'whisper-cli';executable.touch();model=self.root/'fixture-ggml';model.touch();events=[]
        def dispatch(args,**kwargs):
            self.assertEqual(events[-1][0],'reviewing');self.assertIn('Vulkan',events[-1][2])
            output=Path(args[args.index('-of')+1]).with_suffix('.json')
            output.write_text(json.dumps({'transcription':[{'text':'fixture','offsets':{'from':0,'to':500},'tokens':[{'p':.9}]}]}))
            return SimpleNamespace(returncode=0,stderr=b'')
        with patch.object(gpu,'VULKAN',executable),patch.object(gpu,'GGML',model), \
                patch.dict(sys.modules,{'soundfile':SimpleNamespace(write=lambda *a,**k:None)}), \
                patch.object(gpu,'gpu_phase',side_effect=lambda *a:nullcontext()),patch.object(gpu.subprocess,'run',side_effect=dispatch):
            result=gpu.recognize_windows([0.0]*160,160,[('sample',0,1)],self.root,
                lambda *x:events.append(x),independent=True)
        self.assertEqual(result['sample'][0]['text'],'fixture')
        self.assertTrue(all(event[0]=='reviewing' for event in events))


def word(text,start,end,score=.8):return {'text':text,'start':start,'end':end,'score':score}


class AlignmentRejectionDiagnosticTests(unittest.TestCase):
    def test_observed_calling_raw_point84_seconds_and_ctc_stretch_remain_rejected(self):
        original=[word('first',0,1),word('calling',2,2.84),word('next',18,19)]
        aligned=[word('first',0,1),word('calling',2,17,.02),word('next',18,19)]
        before=deepcopy((original,aligned))
        with self.assertRaisesRegex(gpu.AlignmentRejected,'more than 12 seconds') as caught:
            gpu.validate_alignment(original,aligned,30)
        diagnostic=caught.exception.diagnostic
        self.assertEqual(diagnostic['word_index'],1);self.assertEqual(diagnostic['word_number'],2)
        row=diagnostic['words'][1]
        self.assertAlmostEqual(row['original']['end']-row['original']['start'],.84)
        self.assertEqual((row['aligned']['start'],row['aligned']['end'],row['aligned']['score']),(2,17,.02))
        self.assertEqual((original,aligned),before)

    def test_sensitive_text_metadata_reason_and_nonfinite_values_never_enter_diagnostic(self):
        secret='https://name:synthetic-password@example.test/live?token=synthetic-token'
        old=[dict(word(secret,0,1),path='/private/secret',environment={'API_KEY':secret}) for _ in range(9)]
        new=[dict(word(secret,float('nan'),float('inf')),score=True,alignment_score=10**10000) for _ in range(9)]
        diagnostic=gpu.alignment_rejection_diagnostic(old,new,float('nan'),secret,index=4)
        encoded=json.dumps(diagnostic,allow_nan=False)
        self.assertNotIn('synthetic',encoded);self.assertNotIn('/private',encoded);self.assertNotIn('API_KEY',encoded)
        self.assertNotIn('text',diagnostic['words'][0]['original'])
        self.assertEqual(len(diagnostic['words']),5);self.assertEqual(diagnostic['reason'],'alignment rejected')
        self.assertLess(len(encoded),4000)
        self.assertIsNone(diagnostic['duration'])
        self.assertTrue(all(row['aligned']['start'] is None and row['aligned']['end'] is None and row['aligned']['score'] is None and row['aligned']['alignment_score'] is None for row in diagnostic['words']))

    def test_malformed_neighbours_are_recorded_as_missing_numeric_evidence(self):
        old=[None,word('safe',1,2),object()];new=[42,word('safe',1,20),{'text':object()}]
        diagnostic=gpu.alignment_rejection_diagnostic(old,new,30,'a word was stretched over more than 12 seconds at word 2',1)
        self.assertIsNone(diagnostic['words'][0]['original']);self.assertIsNone(diagnostic['words'][2]['original'])
        json.dumps(diagnostic,allow_nan=False)

    def test_global_rejections_do_not_claim_a_specific_word(self):
        words=[word('fixture',i,i+.5) for i in range(8)]
        with self.assertRaises(gpu.AlignmentRejected) as caught:gpu.validate_alignment(words,[],10)
        detail=caught.exception.diagnostic
        self.assertIsNone(detail['word_index']);self.assertIsNone(detail['word_number'])
        self.assertEqual(detail['context_kind'],'opening-sample-global-failure')
        self.assertLessEqual(len(detail['words']),5)

    def test_failure_to_collect_optional_diagnostic_never_changes_rejection_type(self):
        original=[word('fixture',1,2)];aligned=[word('fixture',1,20)]
        with patch.object(gpu,'alignment_rejection_diagnostic',side_effect=ValueError('synthetic-secret')):
            with self.assertRaises(gpu.AlignmentRejected) as caught:gpu.validate_alignment(original,aligned,30)
        self.assertEqual(caught.exception.diagnostic['status'],'unavailable')
        self.assertNotIn('synthetic-secret',str(caught.exception))

    def test_huge_integer_or_malformed_raw_word_still_yields_actionable_rejection(self):
        with self.assertRaises(gpu.AlignmentRejected):gpu.validate_alignment([None],[word('fixture',1,2)],30)
        with self.assertRaises(gpu.AlignmentRejected):gpu.validate_alignment([word('fixture',1,2)],[word('fixture',1,2)],10**10000)

    def test_sustained_word_long_instrumental_gap_and_existing_rounding_tolerance_unchanged(self):
        original=[word('first',1,2),word('second',180,181)]
        gpu.validate_alignment(original,[word('first',1,10.5),word('second',180,181)],200)
        gpu.validate_alignment(original,[word('first',1,1.5),word('second',1.46,2)],3)

    def test_cpu_start_message_and_rejected_alignment_backend_are_preserved(self):
        events=[];original=[word('calling',1,1.84)];aligned=[word('calling',1,20)]
        with tempfile.TemporaryDirectory() as temporary,patch.object(gpu,'enabled',return_value=False), \
                patch.dict(sys.modules,{'soundfile':SimpleNamespace(write=lambda *a,**k:None),
                                       'lyrics_pipeline':SimpleNamespace(align_reference=lambda *a:aligned)}):
            with self.assertRaises(gpu.AlignmentRejected) as caught:
                gpu.realign([0.0]*300,10,original,Path(temporary),lambda *x:events.append(x))
        self.assertIn('CPU word-alignment model',events[0][2]);self.assertEqual(caught.exception.diagnostic['backend'],'cpu')


if __name__=='__main__':unittest.main()
