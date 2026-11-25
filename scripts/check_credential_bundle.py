#!/usr/bin/env python3
"""Inspect packaged native credential code without loading/probing a keyring."""
import argparse
import json
from pathlib import Path
import sys
from PyInstaller.archive.readers import CArchiveReader


def check_credentials(binary,platform):
    archive=CArchiveReader(str(binary)).open_embedded_archive('PYZ.pyz')
    modules=set(archive.toc)
    required={'gnb_player.broadcast_profile','keyring','keyring.backend','jaraco.context','jaraco.functools'}
    required.update({'keyring.backends.Windows','win32ctypes.pywin32.win32cred'} if platform=='Windows' else
                    {'keyring.backends.SecretService','secretstorage','jeepney','cryptography','_cffi_backend'})
    # Native extension modules live outside PYZ.
    internal=Path(binary).parent/'_internal'
    if any(internal.glob('_cffi_backend*')):modules.add('_cffi_backend')
    assert required<=modules, 'Missing protected credential dependencies: '+str(sorted(required-modules))
    forbidden={'keyring.backends.chainer','keyring.backends.null','keyring.backends.libsecret',
               'keyring.backends.kwallet','keyring.backends.macOS','keyrings.alt'}
    forbidden.add('keyring.backends.SecretService' if platform=='Windows' else 'keyring.backends.Windows')
    assert not any(module==name or module.startswith(name+'.') for module in modules for name in forbidden)
    return {'required_native_modules':sorted(required),'alternate_backends':'absent from packaged PYZ',
            'native_keyring_probe':'none; archive inspection only'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('binary',type=Path)
    parser.add_argument('--platform',choices=['Linux','Windows'],default='Windows' if sys.platform=='win32' else 'Linux')
    args=parser.parse_args();print(json.dumps(check_credentials(args.binary,args.platform),indent=2))
