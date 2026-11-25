"""Derived-stem reuse binds actual separator content and execution provenance.

Only disposable model-byte fixtures and a two-second generated sine are used.
No real model is opened, deserialized, constructed or run.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

from gnb_player.extraction_backend import lyrics_cache as cache


class _VocalFixtures:
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.folder=self.root/'derived'
        self.environment=patch.dict(os.environ,{'GNB_LYRICS_CACHE_DIR':str(self.folder)})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.separator=self.root/'separator';self.separator.mkdir()
        (self.separator/'spec.json').write_text('{"args":[],"kwargs":{"sources":["vocals"]}}')
        (self.separator/'model.safetensors').write_bytes(b'fixture-weights-A')
        self.source=self.root/'source.audio';self.source.write_bytes(b'original recording bytes')
        self.runtime={'python':'fixture-python','versions':{'torch':'fixture-1','demucs':'fixture-2'}}
        self.options={'shifts':0,'split':True,'overlap':.25,'decode_rate':44100,'decode_channels':2}

    def identify(self,**overrides):
        return cache.separator_identity(self.separator,backend=overrides.get('backend','cpu'),
            runtime=overrides.get('runtime',self.runtime),options=overrides.get('options',self.options))


class VocalIdentityTests(_VocalFixtures,unittest.TestCase):
    def test_content_runtime_backend_options_and_format_are_recorded_without_loading_model(self):
        identity=self.identify()
        self.assertEqual(identity['model']['weights']['sha256'],hashlib.sha256(b'fixture-weights-A').hexdigest())
        self.assertEqual(identity['model']['spec']['sha256'],hashlib.sha256((self.separator/'spec.json').read_bytes()).hexdigest())
        self.assertEqual(identity['runtime'],self.runtime)
        self.assertEqual(identity['backend'],'cpu')
        self.assertEqual(identity['options'],self.options)
        self.assertEqual(identity['sample_format'],cache.VOCAL_FORMAT)

    def test_identity_is_detached_from_mutable_caller_metadata(self):
        identity=self.identify();before=deepcopy(identity)
        self.runtime['versions']['torch']='changed';self.options['overlap']=.5
        self.assertEqual(identity,before)

    def test_same_content_and_context_have_same_paths(self):
        self.assertEqual(cache.vocal_paths(self.source,self.identify()),cache.vocal_paths(self.source,self.identify()))

    def test_same_path_same_size_model_repair_invalidates_identity_and_cache_key(self):
        old=self.identify();old_paths=cache.vocal_paths(self.source,old)
        path=self.separator/'model.safetensors';before=path.stat()
        path.write_bytes(b'fixture-weights-B');os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
        new=self.identify()
        self.assertNotEqual(old['model']['weights']['sha256'],new['model']['weights']['sha256'])
        self.assertNotEqual(old_paths,cache.vocal_paths(self.source,new))

    def test_changed_spec_invalidates_identity(self):
        old=self.identify();p=self.separator/'spec.json';p.write_text(p.read_text().replace('vocals','singer'))
        self.assertNotEqual(old,self.identify())

    def test_runtime_backend_and_options_each_invalidate_key(self):
        original=cache.vocal_paths(self.source,self.identify())
        contexts=[{'backend':'rocm'},{'runtime':{'python':'other','versions':{'torch':'different'}}},
                  {'options':{**self.options,'overlap':.5}}]
        for context in contexts:
            with self.subTest(context=context):
                self.assertNotEqual(original,cache.vocal_paths(self.source,self.identify(**context)))

    def test_source_content_change_invalidates_key(self):
        identity=self.identify();old=cache.vocal_paths(self.source,identity)
        self.source.write_bytes(b'new recording bytes')
        self.assertNotEqual(old,cache.vocal_paths(self.source,identity))

    def test_missing_identity_skips_without_touching_cache_or_reading_source(self):
        missing=self.root/'does-not-exist'
        self.assertIsNone(cache.vocal_paths(missing))
        self.assertFalse(cache.restore(missing,self.root/'target.wav'))
        self.assertFalse(cache.store(missing,self.root/'input.wav'))
        self.assertFalse(self.folder.exists())

    def test_incomplete_nonfinite_and_wrong_sample_format_identities_are_refused(self):
        good=self.identify();bad=[None,{},dict(good,schema='legacy'),dict(good,runtime={}),
             dict(good,options={'invalid':float('nan')}),dict(good,sample_format={**cache.VOCAL_FORMAT,'sample_rate':44100})]
        missing_hash=deepcopy(good);missing_hash['model']['weights'].pop('sha256');bad.append(missing_hash)
        for identity in bad:
            with self.subTest(identity=identity):self.assertIsNone(cache.vocal_paths(self.root/'not-a-source',identity))
        self.assertFalse(self.folder.exists())

    def test_fingerprint_failure_cannot_fabricate_an_identity(self):
        (self.separator/'model.safetensors').unlink()
        with self.assertRaises((OSError,ValueError)):self.identify()


class VocalRoundtripTests(_VocalFixtures,unittest.TestCase):
    def setUp(self):
        super().setUp()
        try:
            import soundfile
        except ImportError:
            self.skipTest('Existing soundfile runtime unavailable; no dependencies installed.')
        self.sf=soundfile
        self.wav=self.root/'sine.wav'
        with wave.open(str(self.wav),'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
            output.writeframes(b''.join(struct.pack('<h',round(8000*math.sin(2*math.pi*220*n/16000))) for n in range(32000)))

    def save(self,identity=None):
        identity=self.identify() if identity is None else identity
        self.assertTrue(cache.store(self.source,self.wav,identity,current_identity=self.identify(),source_sha256=cache.digest(self.source)))
        return identity,cache.vocal_paths(self.source,identity)

    def test_actual_flac_roundtrip_is_lossless_private_and_provenance_bound(self):
        identity,(audio,meta)=self.save();target=self.root/'restored.wav'
        self.assertTrue(cache.restore(self.source,target,identity))
        a,sr=self.sf.read(self.wav,dtype='int16');b,restored_sr=self.sf.read(target,dtype='int16')
        self.assertTrue((a==b).all());self.assertEqual(sr,restored_sr)
        self.assertEqual(audio.stat().st_mode&0o777,0o600);self.assertEqual(meta.stat().st_mode&0o777,0o600)
        data=json.loads(meta.read_text())
        self.assertEqual(data['identity'],identity);self.assertEqual(data['version'],cache.VOCAL_SCHEMA)
        self.assertEqual(data['source_sha256'],cache.digest(self.source));self.assertEqual(data['sha256'],cache.digest(audio))

    def test_changed_model_spec_runtime_options_backend_and_source_do_not_restore(self):
        identity,_=self.save()
        variants=[self.identify(backend='rocm'),self.identify(runtime={'version':'different'}),
                  self.identify(options={**self.options,'shifts':1})]
        (self.separator/'spec.json').write_text('{"changed":"specification"}')
        variants.append(self.identify())
        (self.separator/'model.safetensors').write_bytes(b'fixture-weights-B')
        variants.append(self.identify())
        for new in variants:
            with self.subTest(identity=new):self.assertFalse(cache.restore(self.source,self.root/'target.wav',new))
        self.source.write_bytes(b'different recording')
        self.assertFalse(cache.restore(self.source,self.root/'target.wav',identity))

    def test_model_change_during_separation_or_missing_after_identity_writes_nothing(self):
        before=self.identify()
        self.assertFalse(cache.store(self.source,self.wav,before,source_sha256=cache.digest(self.source)))
        (self.separator/'model.safetensors').write_bytes(b'fixture-weights-B')
        self.assertFalse(cache.store(self.source,self.wav,before,current_identity=self.identify(),source_sha256=cache.digest(self.source)))
        self.assertFalse(self.folder.exists())

    def test_source_changed_during_separation_cannot_bind_old_stem_to_new_source_key(self):
        identity=self.identify();original_sha=cache.digest(self.source)
        self.source.write_bytes(b'changed while separation was running')
        self.assertFalse(cache.store(self.source,self.wav,identity,current_identity=identity,source_sha256=original_sha))
        self.assertFalse(self.folder.exists())
        new_paths=cache.vocal_paths(self.source,identity)
        self.assertFalse(any(path.exists() for path in new_paths))

    def test_missing_invalid_or_mismatched_pre_separation_source_digest_skips_store(self):
        identity=self.identify()
        for expected in (None,'',False,'0'*63,'z'*64,'0'*64):
            with self.subTest(expected=expected):
                self.assertFalse(cache.store(self.source,self.wav,identity,current_identity=identity,source_sha256=expected))
        self.assertFalse(self.folder.exists())

    def test_legacy_cache_is_not_reused_modified_or_deleted_on_lookup(self):
        self.folder.mkdir()
        legacy_key=hashlib.sha256(('htdemucs-vocals-16k-pcm16-v1:'+cache.digest(self.source)).encode()).hexdigest()
        audio=self.folder/f'vocals-{legacy_key}.flac';meta=audio.with_suffix('.json')
        audio.write_bytes(b'legacy fixture');meta.write_text(json.dumps({'sha256':cache.digest(audio),'version':'htdemucs-vocals-16k-pcm16-v1'}))
        before={p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (audio,meta)}
        self.assertFalse(cache.restore(self.source,self.root/'target.wav',self.identify()))
        self.assertFalse(cache.restore(self.source,self.root/'target.wav'))
        self.assertEqual(before,{p:(p.read_bytes(),p.stat().st_mtime_ns) for p in (audio,meta)})

    def test_sidecar_provenance_version_source_format_and_audio_digest_are_checked(self):
        identity,(audio,meta)=self.save();original=json.loads(meta.read_text())
        variants=[dict(original,version='legacy'),dict(original,source_sha256='0'*64),dict(original,sha256='0'*64),
                  dict(original,sample_format={'sample_rate':44100}),dict(original,identity=self.identify(backend='rocm'))]
        target=self.root/'preserved-target.wav';target.write_bytes(b'keep existing output')
        for data in variants:
            meta.write_text(json.dumps(data))
            self.assertFalse(cache.restore(self.source,target,identity));self.assertEqual(target.read_bytes(),b'keep existing output')

    def test_corrupt_and_oversized_sidecars_are_cache_misses(self):
        identity,(_,meta)=self.save()
        for raw in ('[]','null','{',' '* (cache.VOCAL_METADATA_LIMIT+1)):
            meta.write_text(raw);self.assertFalse(cache.restore(self.source,self.root/'target.wav',identity))

    def test_audio_digest_change_is_a_miss_and_does_not_repair_or_delete_cache(self):
        identity,(audio,meta)=self.save();changed=audio.read_bytes()+b'changed';audio.write_bytes(changed)
        self.assertFalse(cache.restore(self.source,self.root/'target.wav',identity))
        self.assertEqual(audio.read_bytes(),changed);self.assertTrue(meta.exists())

    def test_source_changed_during_restore_preserves_target(self):
        identity,_=self.save();reader=self.sf.read
        def changed(*args,**kwargs):
            result=reader(*args,**kwargs);self.source.write_bytes(b'changed source');return result
        target=self.root/'target.wav';target.write_bytes(b'preserve')
        with patch.object(self.sf,'read',side_effect=changed):self.assertFalse(cache.restore(self.source,target,identity))
        self.assertEqual(target.read_bytes(),b'preserve')

    def test_source_changed_during_store_is_not_committed(self):
        identity=self.identify();reader=self.sf.read
        def changed(*args,**kwargs):
            result=reader(*args,**kwargs);self.source.write_bytes(b'changed source');return result
        with patch.object(self.sf,'read',side_effect=changed):
            self.assertFalse(cache.store(self.source,self.wav,identity,current_identity=identity,source_sha256=cache.digest(self.source)))
        self.assertFalse(list(self.folder.glob('vocals-*')))

    def test_symlinked_audio_or_sidecar_is_not_followed_or_overwritten(self):
        identity,(audio,meta)=self.save();outside=self.root/'outside';outside.write_bytes(b'preserve private file')
        audio.unlink();audio.symlink_to(outside)
        self.assertFalse(cache.restore(self.source,self.root/'target.wav',identity))
        self.assertFalse(cache.store(self.source,self.wav,identity,current_identity=identity,source_sha256=cache.digest(self.source)))
        self.assertEqual(outside.read_bytes(),b'preserve private file')


if __name__=='__main__':unittest.main()
