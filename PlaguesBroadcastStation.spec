# Native one-folder desktop build. Model weights/runtime stay separate.
from pathlib import Path
import shutil
import subprocess
import sys
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
package = root / "src" / "gnb_player"
worker_data = [(str(package / name), "gnb_player")
               for name in ("extract_worker.py", "extraction.py", "lyrics.py", "model_install.py",
                            "model_install_worker.py", "model_discovery.py")]
worker_data += [(str(path), "gnb_player/extraction_backend")
                for path in sorted((package / "extraction_backend").glob("*.py"))]
assets = [(str(path), "assets") for path in (root / "assets").glob("*") if path.is_file()]
assets += [(str(path), "gnb_player/assets")
           for path in (package / "assets").glob("*") if path.is_file()]
ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
if not ffmpeg or not ffprobe:
    raise SystemExit("Build prerequisite missing: install FFmpeg and ffprobe on PATH; this standalone package must include both tools.")
license_dir = root / "build" / "third-party"
license_dir.mkdir(parents=True, exist_ok=True)
build_info = []
for command in ([ffmpeg, "-version"], [ffmpeg, "-L"], [ffprobe, "-version"]):
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True)
    build_info += ["COMMAND: " + " ".join(command), result.stdout, result.stderr]
(license_dir / "FFmpeg-build-info.txt").write_text("\n".join(build_info), encoding="utf-8")
notices = [(str(root / "THIRD_PARTY_NOTICES.md"), ".")]
notices += [(str(path), "licenses") for path in (root / "licenses").glob("*.txt")]
notices += [(str(license_dir / "FFmpeg-build-info.txt"), "licenses")]
credential_metadata = ["keyring", "jaraco.classes", "jaraco.context", "jaraco.functools", "more-itertools"]
credential_imports = ["keyring.backends.Windows", "win32ctypes.pywin32.win32cred"] if sys.platform == "win32" else ["keyring.backends.SecretService", "secretstorage", "jeepney"]
credential_metadata += ["pywin32-ctypes"] if sys.platform == "win32" else ["SecretStorage", "jeepney", "cryptography", "cffi", "pycparser"]
# No alternate, chain, environment-selected or plaintext credential backends.
credential_excludes = ["keyrings", "keyrings.alt", "keyring.backends.chainer", "keyring.backends.null",
                       "keyring.backends.libsecret", "keyring.backends.kwallet", "keyring.backends.macOS"]
credential_excludes += ["keyring.backends.SecretService", "secretstorage", "jeepney"] if sys.platform == "win32" else ["keyring.backends.Windows"]
for package_name in ["PySide6", "PySide6_Essentials", "PySide6_Addons", "shiboken6", "numpy", "pyinstaller", *credential_metadata]:
    notices += copy_metadata(package_name)


a = Analysis(
    [str(root / "launch.py")],
    pathex=[str(root / "src")],
    binaries=[(ffmpeg, "bin"), (ffprobe, "bin")],
    datas=worker_data + assets + notices,
    hiddenimports=["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtMultimedia", *credential_imports],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
              "PySide6.QtPdf", "PySide6.QtPdfWidgets", "torch", "torchaudio", "demucs",
              "faster_whisper", "transformers", "tensorflow", *credential_excludes],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PlaguesBroadcastStation",
    icon=str(package / "assets" / "broadcast-icon.ico") if sys.platform == "win32" else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

# Preserve Debian copyright/source-version notices for native dependencies when
# the builder supplies them. The binaries themselves stay in PyInstaller's
# normal dependency analysis so missing DLL/SO files are diagnosed by the build.
if shutil.which("dpkg-query"):
    debian_notices = license_dir / "debian"
    debian_notices.mkdir(exist_ok=True)
    package_versions = set()
    seen_packages = set()
    for destination, source, kind in a.binaries:
        source_path = Path(source).resolve()
        if not source_path.is_file() or not str(source_path).startswith(("/usr/", "/lib/")):
            continue
        found = subprocess.run(["dpkg-query", "-S", str(source_path)], capture_output=True, text=True, timeout=5)
        if found.returncode:
            continue
        for line in found.stdout.splitlines():
            package = line.split(": ", 1)[0]
            if package in seen_packages:
                continue
            seen_packages.add(package)
            name = package.split(":", 1)[0]
            copyright_file = Path("/usr/share/doc") / name / "copyright"
            if copyright_file.is_file():
                target = debian_notices / (name + "-copyright.txt")
                shutil.copy2(copyright_file, target)
                a.datas.append(("licenses/debian/" + target.name, str(target), "DATA"))
            version = subprocess.run(["dpkg-query", "-W", "-f=${binary:Package} ${Version} source=${source:Package} ${source:Version}\n", package], capture_output=True, text=True, timeout=5)
            if version.returncode == 0:
                package_versions.add(version.stdout)
    versions_file = debian_notices / "package-source-versions.txt"
    versions_file.write_text("".join(sorted(package_versions)), encoding="utf-8")
    a.datas.append(("licenses/debian/" + versions_file.name, str(versions_file), "DATA"))

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PlaguesBroadcastStation")
