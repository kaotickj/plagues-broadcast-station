#!/usr/bin/env python3
"""Verify only the pinned RC10 neighbour guard, without changing a release.

This is deliberately separate from linux_installer.verify_release. No generic
integrity rule is relaxed, no executable is launched and no station data or
credential store is opened. CLI policy has no user-supplied hash overrides.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

APP_ID = 'plagues-broadcast-station'
EXECUTABLE = 'PlaguesBroadcastStation'
MANIFEST = '.pbs-release.json'
POLICY_PATH = Path(__file__).with_name('rc10-neighbour-guard-policy.json')
PINNED_POLICY_SHA256 = 'e88fb58c7c23c9a9ec0f5742cfa4b91b478243097c6fda20c0cb5018f24032c4'
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r'[0-9a-f]{64}')


class HotfixError(ValueError):
    """The exact approved release was not verified; do not change pointers."""


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise HotfixError('Duplicate JSON fields are not accepted.')
        value[key] = item
    return value


def _decode(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(HotfixError('Nonfinite JSON is not accepted.')))
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise HotfixError('The verification metadata is invalid.') from error


def _relative(value):
    if not isinstance(value, str) or not value or value in ('.', '..') or '\x00' in value or '\\' in value:
        raise HotfixError('Invalid release inventory path.')
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(p in ('', '.', '..') for p in path.parts):
        raise HotfixError('Invalid release inventory path.')
    return value


@dataclass(frozen=True)
class Policy:
    release: str
    version: str
    source_commit: str
    original_manifest_sha256: str
    binary_sha256: str
    guard_path: str
    original_guard_sha256: str
    approved_guard_sha256: str
    regular_mode: int = 0o644
    executable_mode: int = 0o755
    directory_mode: int = 0o775
    root_directory_mode: int | None = None
    manifest_mode: int = 0o644
    empty_directories: tuple[str, ...] = ()
    schema: int = 1

    def __post_init__(self):
        if self.schema != 1 or type(self.schema) is not int:
            raise HotfixError('Unsupported pinned hotfix policy.')
        if (not isinstance(self.release, str) or Path(self.release).name != self.release
                or not self.release or self.release in ('.', '..')
                or not isinstance(self.version, str) or not self.version
                or not isinstance(self.source_commit, str) or re.fullmatch(r'[0-9a-f]{40}', self.source_commit) is None):
            raise HotfixError('Invalid pinned release identity.')
        for value in (self.original_manifest_sha256, self.binary_sha256,
                      self.original_guard_sha256, self.approved_guard_sha256):
            if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                raise HotfixError('Invalid pinned digest.')
        if self.original_guard_sha256 == self.approved_guard_sha256:
            raise HotfixError('The hotfix must be a distinct, explicitly pinned file.')
        _relative(self.guard_path)
        if self.guard_path in (MANIFEST, EXECUTABLE):
            raise HotfixError('The hotfix cannot replace a manifest or executable.')
        if self.root_directory_mode is None:
            object.__setattr__(self, 'root_directory_mode', self.directory_mode)
        for mode in (self.regular_mode, self.executable_mode, self.directory_mode, self.root_directory_mode, self.manifest_mode):
            if type(mode) is not int or mode < 0 or mode > 0o777:
                raise HotfixError('Invalid pinned mode; special permission bits are refused.')
        if self.regular_mode & 0o111 or not self.executable_mode & 0o111:
            raise HotfixError('Pinned modes must preserve executable classification.')
        if not isinstance(self.empty_directories, tuple) or len(set(self.empty_directories)) != len(self.empty_directories):
            raise HotfixError('Invalid pinned empty-directory set.')
        for name in self.empty_directories:
            _relative(name)

    @property
    def payload(self):
        return asdict(self)


@dataclass(frozen=True)
class HotfixVerification:
    bundle: Path
    policy: Policy
    original_manifest_raw: bytes
    patched_manifest_raw: bytes
    inventory_raw: bytes
    receipt_raw: bytes

    @property
    def manifest(self):
        return _decode(self.original_manifest_raw)

    @property
    def patched_manifest(self):
        return _decode(self.patched_manifest_raw)

    @property
    def inventory(self):
        return _decode(self.inventory_raw)

    @property
    def receipt(self):
        return _decode(self.receipt_raw)


def _absolute(value):
    path = Path(value)
    if '..' in path.parts or '\x00' in str(path):
        raise HotfixError('Verification paths must not traverse parent directories.')
    return Path(os.path.abspath(path))


@contextmanager
def _directory(path, *, create=False):
    """Hold no-follow directory capabilities, never follow a linked ancestor."""
    path = _absolute(path)
    fd = None
    try:
        if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY') or os.open not in os.supports_dir_fd:
            raise HotfixError('Scoped hotfix verification requires Linux no-follow directory operations.')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(path.anchor, flags)
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except (HotfixError, OSError) as error:
        if fd is not None:
            os.close(fd)
        if isinstance(error, HotfixError):
            raise
        raise HotfixError('A verification directory is missing, unreadable or linked. Nothing was activated.') from error
    try:
        yield fd
    finally:
        os.close(fd)


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _stable_directory(path, fd):
    current, opened = path.stat(follow_symlinks=False), os.fstat(fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise HotfixError('The release directory changed during verification.')


def _read(fd, name, *, maximum=None):
    before = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or maximum is not None and not 0 < before.st_size <= maximum:
        raise HotfixError('Expected a bounded regular metadata file; links are refused.')
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        opened = os.fstat(handle)
        if _stamp(opened) != _stamp(before):
            raise HotfixError('A release file changed while opening.')
        result, digest, size = [], hashlib.sha256(), 0
        while True:
            chunk = os.read(handle, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk); size += len(chunk)
            if maximum is not None:
                if size > maximum:
                    raise HotfixError('Verification metadata exceeded its limit.')
                result.append(chunk)
        after = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if size != opened.st_size or _stamp(os.fstat(handle)) != _stamp(opened) or _stamp(after) != _stamp(opened):
            raise HotfixError('A release file changed while reading.')
        return b''.join(result) if maximum is not None else digest.hexdigest(), opened
    finally:
        os.close(handle)


def _load_policy(path):
    path = _absolute(path)
    with _directory(path.parent) as fd:
        raw, _ = _read(fd, path.name, maximum=16000)
    if hashlib.sha256(raw).hexdigest() != PINNED_POLICY_SHA256:
        raise HotfixError('The bundled RC10 policy changed; no custom hashes are accepted.')
    data = _decode(raw)
    if not isinstance(data, dict) or set(data) != set(Policy.__dataclass_fields__):
        raise HotfixError('The bundled RC10 policy has unsupported fields.')
    data['empty_directories'] = tuple(data['empty_directories'])
    return Policy(**data)


def load_default_policy():
    return _load_policy(POLICY_PATH)


DEFAULT_POLICY = load_default_policy()


def _inventory(root_fd, expected, directories, policy):
    actual, seen_directories, observed = {}, {'.'}, {}
    def walk(fd, prefix):
        directory_state = os.fstat(fd)
        observed[prefix[:-1] if prefix else '.'] = _stamp(directory_state)
        expected_mode = policy.directory_mode if prefix else policy.root_directory_mode
        if stat.S_IMODE(directory_state.st_mode) != expected_mode:
            raise HotfixError('A release directory mode differs from the pinned RC10 baseline.')
        for name in sorted(os.listdir(fd)):
            relative = prefix + name
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                raise HotfixError('Symbolic links are not part of this pinned RC10 baseline.')
            if relative == MANIFEST:
                continue
            if stat.S_ISDIR(entry.st_mode):
                if relative not in directories:
                    raise HotfixError('An unknown release directory was found.')
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if (entry.st_dev, entry.st_ino) != (os.fstat(child).st_dev, os.fstat(child).st_ino):
                        raise HotfixError('A release directory changed while opening.')
                    seen_directories.add(relative)
                    walk(child, relative + '/')
                    if _stamp(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _stamp(entry):
                        raise HotfixError('A release directory changed during verification.')
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry.st_mode):
                wanted = expected.get(relative)
                if wanted is None:
                    raise HotfixError('An unknown release file was found.')
                mode = policy.executable_mode if wanted['executable'] else policy.regular_mode
                if stat.S_IMODE(entry.st_mode) != mode:
                    raise HotfixError('A release file mode differs from the pinned RC10 baseline.')
                sha, opened = _read(fd, name)
                actual[relative] = {'sha256': sha, 'executable': bool(opened.st_mode & 0o111)}
                observed[relative] = _stamp(opened)
            else:
                raise HotfixError('A nonregular release entry was found.')
        if _stamp(os.fstat(fd)) != _stamp(directory_state):
            raise HotfixError('A release directory changed during verification.')
    walk(root_fd, '')
    if seen_directories != directories:
        raise HotfixError('A pinned release directory is missing.')
    # A file hashed early must not change unnoticed while another subtree is
    # being read. Recheck all recorded inode/mode/content-change stamps through
    # the held root capability; never reopen a path through a new symlink.
    for relative, stamp in observed.items():
        if relative == '.':
            current = os.fstat(root_fd)
        else:
            parts = PurePosixPath(relative).parts
            parent = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    os.close(parent); parent = child
                current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            finally:
                os.close(parent)
        if _stamp(current) != stamp:
            raise HotfixError('A previously hashed release entry changed during verification.')
    return actual


def verify_rc10_hotfix(bundle, *, policy=DEFAULT_POLICY):
    """Accept exactly the approved file on its exact original RC10 baseline."""
    if not isinstance(policy, Policy):
        raise HotfixError('An explicit immutable hotfix policy is required.')
    if policy is DEFAULT_POLICY and load_default_policy() != DEFAULT_POLICY:
        raise HotfixError('The bundled RC10 policy changed.')
    bundle = _absolute(bundle)
    try:
        with _directory(bundle) as fd:
            raw, info = _read(fd, MANIFEST, maximum=MAX_MANIFEST_BYTES)
            if stat.S_IMODE(info.st_mode) != policy.manifest_mode or hashlib.sha256(raw).hexdigest() != policy.original_manifest_sha256:
                raise HotfixError('The original RC10 manifest or its mode differs from the pinned baseline.')
            manifest = _decode(raw)
            if (not isinstance(manifest, dict) or manifest.get('schema') != 1
                    or manifest.get('app_id') != APP_ID or manifest.get('version') != policy.version
                    or manifest.get('release') != policy.release or manifest.get('source_commit') != policy.source_commit
                    or manifest.get('platform') != 'Linux' or manifest.get('source_dirty') is not False
                    or not isinstance(manifest.get('files'), dict)):
                raise HotfixError('The original RC10 release identity differs from the pinned baseline.')
            expected = manifest['files']
            if len(expected) > 10000 or not expected:
                raise HotfixError('Invalid original RC10 inventory.')
            directories = {'.', *policy.empty_directories}
            for name, entry in expected.items():
                _relative(name)
                if (name == MANIFEST or not isinstance(entry, dict) or set(entry) != {'sha256', 'executable'}
                        or not isinstance(entry['sha256'], str) or _SHA.fullmatch(entry['sha256']) is None
                        or type(entry['executable']) is not bool):
                    raise HotfixError('Unsupported entry in the original RC10 inventory.')
                directories.update(parent.as_posix() for parent in PurePosixPath(name).parents)
            for name in policy.empty_directories:
                directories.update(parent.as_posix() for parent in PurePosixPath(name).parents)
            if hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest() != manifest.get('bundle_sha256'):
                raise HotfixError('The original RC10 inventory checksum is invalid.')
            if expected.get(EXECUTABLE) != {'sha256': policy.binary_sha256, 'executable': True}:
                raise HotfixError('The original executable is not the approved RC10 binary.')
            if expected.get(policy.guard_path) != {'sha256': policy.original_guard_sha256, 'executable': False}:
                raise HotfixError('The original neighbour guard baseline differs.')
            patched = _decode(_json(manifest))
            patched['files'][policy.guard_path]['sha256'] = policy.approved_guard_sha256
            patched['bundle_sha256'] = hashlib.sha256(json.dumps(patched['files'], sort_keys=True).encode()).hexdigest()
            actual = _inventory(fd, patched['files'], directories, policy)
            if actual != patched['files']:
                raise HotfixError('The release differs from RC10 plus the exact approved neighbour guard; unknown differences are refused.')
            # Re-read the manifest and directory binding after hashing all code.
            again, _ = _read(fd, MANIFEST, maximum=MAX_MANIFEST_BYTES)
            if again != raw:
                raise HotfixError('The original manifest changed during verification.')
            _stable_directory(bundle, fd)
        patched_raw = _json(patched)
        receipt = {'schema': 'rc10-approved-hotfix-verification-1', 'bundle': str(bundle),
                   'release': policy.release, 'version': policy.version, 'source_commit': policy.source_commit,
                   'policy_sha256': hashlib.sha256(_json(policy.payload)).hexdigest(),
                   'policy_digest_representation': 'canonical JSON payload',
                   'bundled_policy_file_sha256': PINNED_POLICY_SHA256 if policy is DEFAULT_POLICY else None,
                   'original_manifest_sha256': hashlib.sha256(raw).hexdigest(),
                   'derived_patched_manifest_sha256': hashlib.sha256(patched_raw).hexdigest(),
                   'verified_inventory_sha256': patched['bundle_sha256'],
                   'approved_change': {'path': policy.guard_path, 'original_sha256': policy.original_guard_sha256,
                                       'approved_sha256': policy.approved_guard_sha256},
                   'file_count': len(actual), 'directory_count_including_root': len(directories),
                   'mode_pins': {'regular': policy.regular_mode, 'executable': policy.executable_mode,
                                 'directory': policy.directory_mode, 'root_directory': policy.root_directory_mode, 'manifest': policy.manifest_mode,
                                 'provenance': 'explicit readiness baseline pins; original manifest records executable flags, not full modes'},
                   'original_manifest_unchanged': True, 'unknown_differences': 'refused', 'symlinks': 'refused'}
        return HotfixVerification(bundle, policy, raw, patched_raw, _json(actual), _json(receipt))
    except HotfixError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        raise HotfixError('The pinned RC10 release could not be verified safely. Nothing was activated.') from error


def _write_create_only(fd, name, raw):
    temporary = '.rc10-audit-' + uuid.uuid4().hex + '.tmp'
    try:
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(handle, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            existing, info = _read(fd, name, maximum=MAX_MANIFEST_BYTES)
            if existing != raw or stat.S_IMODE(info.st_mode) != 0o600:
                raise HotfixError('Existing audit evidence differs; no file was overwritten.')
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except OSError:
            pass


def capture_hotfix_evidence(verification, audit_dir):
    """Create-only evidence; receipt is published last after exact byte checks.

    Interrupted writes retain any completed original/derived files, with no
    completion receipt. A matching partial capture can be retried; conflicting
    files, extra entries, links or modes are refused instead of overwritten.
    """
    if not isinstance(verification, HotfixVerification):
        raise HotfixError('Verify the exact RC10 bundle before capturing its audit evidence.')
    audit = _absolute(audit_dir)
    bundle = verification.bundle
    if audit.is_relative_to(bundle) or bundle.is_relative_to(audit):
        raise HotfixError('Audit evidence must be separate from the retained release.')
    fresh = verify_rc10_hotfix(bundle, policy=verification.policy)
    if fresh.original_manifest_raw != verification.original_manifest_raw or fresh.patched_manifest_raw != verification.patched_manifest_raw:
        raise HotfixError('The verified release changed before evidence capture.')
    names = {'original-manifest.json': fresh.original_manifest_raw,
             'patched-manifest.json': fresh.patched_manifest_raw,
             'receipt.json': fresh.receipt_raw}
    try:
        with _directory(audit, create=True) as fd:
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
                raise HotfixError('Audit evidence directory must have mode0700.')
            if set(os.listdir(fd)) - names.keys():
                raise HotfixError('Unexpected audit entries were preserved; choose a fresh audit directory.')
            for name in os.listdir(fd):
                raw, info = _read(fd, name, maximum=MAX_MANIFEST_BYTES)
                if raw != names[name] or stat.S_IMODE(info.st_mode) != 0o600:
                    raise HotfixError('Existing audit evidence differs; no file was overwritten.')
            for name in ('original-manifest.json', 'patched-manifest.json'):
                _write_create_only(fd, name, names[name])
            # Re-verify before publishing the completion receipt, not merely
            # before copying the first file.
            final = verify_rc10_hotfix(bundle, policy=verification.policy)
            if final.original_manifest_raw != fresh.original_manifest_raw or final.patched_manifest_raw != fresh.patched_manifest_raw:
                raise HotfixError('The release changed during audit capture; no receipt was published.')
            for name in ('original-manifest.json', 'patched-manifest.json'):
                raw, _ = _read(fd, name, maximum=MAX_MANIFEST_BYTES)
                if raw != names[name]:
                    raise HotfixError('Audit manifests changed; no receipt was published.')
            _write_create_only(fd, 'receipt.json', names['receipt.json'])
            _stable_directory(audit, fd)
        return fresh
    except HotfixError:
        raise
    except OSError as error:
        raise HotfixError('Audit capture did not finish; retained files were not overwritten. Verify any existing receipt before use.') from error


def capture_verified_hotfix(bundle, audit_dir, *, policy=DEFAULT_POLICY):
    return capture_hotfix_evidence(verify_rc10_hotfix(bundle, policy=policy), audit_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('--audit-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        verified = verify_rc10_hotfix(args.bundle)
        if args.audit_dir is not None:
            verified = capture_hotfix_evidence(verified, args.audit_dir)
        print(json.dumps(verified.receipt, indent=2, sort_keys=True))
        return 0
    except (HotfixError, OSError) as error:
        print('RC10 hotfix verification: ' + str(error), file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
