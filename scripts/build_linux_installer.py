#!/usr/bin/env python3
"""Wrap a freshly built native folder in an offline, versioned Linux installer."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linux_installer as installer

ROOT = Path(__file__).resolve().parents[1]


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()


def write_bundle_archive(bundle, manifest, archive, prefix=""):
    """Preserve native Linux modes/links in installer and portable archives."""
    with tarfile.open(archive, "w:gz", compresslevel=6, dereference=False) as package:
        data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        entry = tarfile.TarInfo(prefix + ".pbs-release.json")
        entry.size = len(data)
        entry.mode = 0o644
        package.addfile(entry, io.BytesIO(data))
        for path in sorted(bundle.iterdir()):
            package.add(path, arcname=prefix + path.name, recursive=True)


def build(bundle, output_dir):
    if platform.system() != "Linux":
        raise ValueError("The .run package is Linux only; build Windows packages on Windows.")
    bundle = bundle.resolve()
    binary = bundle / installer.EXECUTABLE
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Expected a native Linux PyInstaller executable.")
    with binary.open("rb") as source:
        magic = source.read(4)
    if magic != b"\x7fELF":
        raise ValueError("Expected a native Linux PyInstaller executable.")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    reported = subprocess.check_output([str(binary), "--version"], text=True, timeout=30).strip()
    if reported != f"Plague's Broadcast Station {version}":
        raise ValueError(f"Bundle version does not match source: {reported!r}")
    # A build-time provenance record is required; never relabel an arbitrary old
    # executable as a new source version. The clean build script writes it before
    # PyInstaller and packaging checks source and binary against it.
    provenance = json.loads((ROOT / "build" / "linux-build-source.json").read_text())
    if provenance != source_identity():
        raise ValueError("Source changed since the native build began; rebuild before packaging.")
    files = installer.inventory(bundle)
    content_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    release = f"{version}-{provenance['source_commit'][:12]}-{content_hash[:12]}"
    manifest = {"schema": 1, "app_id": installer.APP_ID, "version": version, "release": release,
                "architecture": platform.machine(), "platform": "Linux", **provenance,
                "bundle_sha256": content_hash, "files": files}
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"PlaguesBroadcastStation-{version}-linux-{platform.machine()}.run"
    with tempfile.TemporaryDirectory(prefix="pbs-package-", dir=output_dir) as temporary:
        archive = Path(temporary) / "payload.tar.gz"
        write_bundle_archive(bundle, manifest, archive)
        metadata = {"sha256": installer.digest(archive), "architecture": platform.machine(), "release": release}
        source = (ROOT / "scripts/linux_installer.py").read_text()
        runner = ("#!/bin/sh\nexec python3 - \"$0\" \"$@\" <<'PBS_INSTALLER_PY'\n"
                  "import pathlib, sys\n"
                  f"source = {source!r}\n"
                  "namespace = {'__name__': 'pbs_installer'}\n"
                  "exec(compile(source, '<linux-installer>', 'exec'), namespace)\n"
                  f"payload = {metadata!r}\n"
                  "archive = pathlib.Path(sys.argv[1]).resolve()\n"
                  "with archive.open('rb') as handle:\n"
                  "    while handle.readline() != namespace['PAYLOAD_MARKER'].lstrip(b'\\n'):\n"
                  "        if handle.tell() >= archive.stat().st_size:\n"
                  "            raise SystemExit('Installer archive marker is missing.')\n"
                  "    payload.update(archive=str(archive), offset=handle.tell())\n"
                  "raise SystemExit(namespace['main'](sys.argv[2:], payload, source))\n"
                  "PBS_INSTALLER_PY\n__PBS_ARCHIVE_BELOW__\n").encode()
        with output.open("wb") as target, archive.open("rb") as payload:
            import shutil
            target.write(runner)
            shutil.copyfileobj(payload, target)
        output.chmod(0o755)
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    checksum = output.with_suffix(".run.sha256")
    checksum.write_text(installer.digest(output) + "  " + output.name + "\n")
    portable = output.with_name(output.stem + "-portable.tar.gz")
    write_bundle_archive(bundle, manifest, portable, prefix="PlaguesBroadcastStation/")
    portable.with_name(portable.name + ".sha256").write_text(installer.digest(portable) + "  " + portable.name + "\n")
    print(output)
    print(portable)
    return output


def source_identity():
    paths = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    result = hashlib.sha256()
    for name in sorted(paths):
        path = ROOT / name
        if path.is_file():
            result.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return {"source_commit": git("rev-parse", "HEAD"), "source_tree_sha256": result.hexdigest(),
            "source_dirty": bool(git("status", "--porcelain")),
            "build_python": platform.python_version(), "build_system": platform.platform()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-source", action="store_true", help="Record exact source immediately before the clean PyInstaller build")
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/PlaguesBroadcastStation")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()
    if args.record_source:
        (ROOT / "build").mkdir(exist_ok=True)
        (ROOT / "build/linux-build-source.json").write_text(json.dumps(source_identity(), indent=2) + "\n")
    else:
        build(args.bundle, args.output_dir)
