"""Disposable runtime discovery: preserve venv invocation, reject path escapes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from gnb_player import model_discovery as discovery


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Real Linux venv/symlink contract')


@pytest.fixture
def bounded_discovery(monkeypatch):
    monkeypatch.setattr(discovery.shutil, 'which', lambda name: None)
    monkeypatch.setattr(discovery.sys, 'frozen', True, raising=False)


def make_runtime(folder):
    # No pip, package installation, downloads or imports of heavy packages.
    venv.EnvBuilder(with_pip=False, symlinks=True).create(folder)
    python = folder / 'bin/python'
    assert python.is_symlink() and python.is_file() and os.access(python, os.X_OK)
    return python


def complete(version, chosen):
    version.mkdir(parents=True, exist_ok=True)
    (version / 'installation.complete.json').write_text(json.dumps({'settings': {'python_path': str(chosen)}}))


def test_managed_venv_leaf_symlink_discovered_only_after_completion(tmp_path, bounded_discovery):
    version = tmp_path / 'model-setup/versions/quality-complete'
    python = make_runtime(version / 'runtime')
    assert not python.resolve().is_relative_to(version)
    assert discovery.discover_python_paths(app_data=tmp_path) == []
    complete(version, python)
    assert discovery.discover_python_paths(app_data=tmp_path) == [str(python)]


def test_explicit_runtime_remains_first_and_distinct_venvs_are_not_deduplicated(tmp_path, bounded_discovery):
    first = make_runtime(tmp_path / 'first runtime')
    current = make_runtime(tmp_path / 'selected runtime')
    assert first.resolve() == current.resolve()
    paths = discovery.discover_python_paths(app_data=tmp_path / 'app', settings={'python_path': str(current)},
        extra_paths=[first, current, first.parent / '../bin/python'])
    assert paths == [str(current), str(first)]


def test_real_isolated_probe_reads_venv_metadata_without_importing_fixture_package(tmp_path, monkeypatch):
    # Probe with normal nonfrozen child environment, so the test does not depend
    # on the parent process's installed packages or Qt/PyInstaller state.
    python = make_runtime(tmp_path / 'metadata runtime')
    monkeypatch.setattr(discovery.sys, 'frozen', False, raising=False)
    metadata = json.loads(subprocess.check_output([str(python), '-I', '-c',
        'import json,sys,sysconfig;print(json.dumps({"prefix":sys.prefix,"base":sys.base_prefix,"purelib":sysconfig.get_path("purelib")}))'], text=True))
    assert metadata['prefix'] == str(python.parent.parent) and metadata['base'] != metadata['prefix']
    packages = Path(metadata['purelib'])
    (packages / 'demucs.py').write_text('raise AssertionError("metadata probe must not import this module")\n')
    info = packages / 'demucs-4.0.1.dist-info'; info.mkdir()
    (info / 'METADATA').write_text('Metadata-Version: 2.1\nName: demucs\nVersion: 4.0.1\n')
    result = discovery.probe_python(python)
    assert result.path == str(python) and result.version.startswith('3.')
    assert result.packages['demucs'] == '4.0.1'
    assert not result.compatible  # Other required packages intentionally absent.


@pytest.mark.parametrize('escape_kind', ['absolute', 'dotdot', 'runtime-directory-link', 'bin-directory-link'])
def test_manifest_rejects_outside_entry_or_parent_directory_escape(tmp_path, bounded_discovery, escape_kind):
    app = tmp_path / 'app'; version = app / 'model-setup/versions/quality-fixture'
    outside = make_runtime(tmp_path / 'outside')
    version.mkdir(parents=True)
    if escape_kind == 'absolute':
        chosen = outside
    elif escape_kind == 'dotdot':
        chosen = version / '../../../../outside/bin/python'
    elif escape_kind == 'runtime-directory-link':
        (version / 'runtime').symlink_to(outside.parent.parent, target_is_directory=True)
        chosen = version / 'runtime/bin/python'
    else:
        (version / 'runtime').mkdir()
        (version / 'runtime/bin').symlink_to(outside.parent, target_is_directory=True)
        chosen = version / 'runtime/bin/python'
    complete(version, chosen)
    assert discovery.discover_python_paths(app_data=app) == []


def test_managed_versions_namespace_cannot_symlink_outside_app_data(tmp_path, bounded_discovery):
    app = tmp_path / 'app'; external_versions = tmp_path / 'external-versions'
    version = external_versions / 'quality-fixture'; python = make_runtime(version / 'runtime')
    complete(version, python)
    (app / 'model-setup').mkdir(parents=True)
    (app / 'model-setup/versions').symlink_to(external_versions, target_is_directory=True)
    assert discovery.discover_python_paths(app_data=app) == []


def test_manifest_internal_directory_alias_stays_within_version_and_keeps_invocation(tmp_path, bounded_discovery):
    version = tmp_path / 'model-setup/versions/quality-fixture'
    python = make_runtime(version / 'runtime')
    (version / 'runtime-alias').symlink_to(python.parent.parent, target_is_directory=True)
    invocation = version / 'runtime-alias/bin/python'
    complete(version, invocation)
    assert discovery.discover_python_paths(app_data=tmp_path) == [str(invocation)]


def test_manifest_symlink_cannot_import_completion_from_outside_version(tmp_path, bounded_discovery):
    version = tmp_path / 'app/model-setup/versions/quality-fixture'
    python = make_runtime(version / 'runtime')
    outside = tmp_path / 'forged-completion.json'
    outside.write_text(json.dumps({'settings': {'python_path': str(python)}}))
    (version / 'installation.complete.json').symlink_to(outside)
    assert discovery.discover_python_paths(app_data=tmp_path / 'app') == []


def test_nonexecutable_broken_loop_and_directory_candidates_are_ignored(tmp_path, bounded_discovery):
    nonexecutable = tmp_path / 'not-python'; nonexecutable.write_text('fixture only');nonexecutable.chmod(0o600)
    broken = tmp_path / 'broken-python'; broken.symlink_to(tmp_path / 'missing-target')
    loop = tmp_path / 'loop-python'; loop.symlink_to(loop)
    paths = discovery.discover_python_paths(app_data=tmp_path / 'app',
        extra_paths=[nonexecutable, broken, loop, tmp_path, 'invalid\0python'])
    assert paths == []
