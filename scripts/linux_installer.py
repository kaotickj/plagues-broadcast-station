#!/usr/bin/env python3
"""Linux per-user installer. Standard library only; never changes app data."""
from __future__ import annotations

import argparse
import contextlib
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

APP_ID = "plagues-broadcast-station"
MARKER = "PlaguesBroadcastStation managed install v1\n"
PAYLOAD_MARKER = b"\n" + b"__PBS_ARCHIVE_BELOW__" + b"\n"
EXECUTABLE = "PlaguesBroadcastStation"


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_write(path, content, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".pbs-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def clean_path(value):
    if any(c in str(value) for c in "\n\r\x00"):
        raise ValueError("Paths cannot contain newlines or NUL characters.")
    return Path(value).expanduser().absolute()


def safe_directory(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError(f"Expected an ordinary directory, refusing: {path}")
    path.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def locked_root(root, create=False):
    if root.is_symlink():
        raise ValueError("The install directory must not be a symbolic link.")
    marker = root / ".pbs-installation"
    if not marker.exists():
        if not create or (root.exists() and any(root.iterdir())):
            raise ValueError("Not a managed install directory. Choose a new empty directory; existing folders are never overwritten.")
        safe_directory(root)
        # Exclusive creation also prevents two fresh installers adopting one root.
        with marker.open("x") as handle:
            handle.write(MARKER)
    if marker.is_symlink() or marker.read_text() != MARKER:
        raise ValueError("Unrecognized installation marker.")
    lock_path = root / ".install.lock"
    if lock_path.is_symlink():
        raise ValueError("Refusing a symbolic-link installation lock.")
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another installer is using this directory; try again after it finishes.") from None
        yield


def read_state(root):
    path = root / "install.json"
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError("Refusing symbolic-link installation metadata.")
    state = json.loads(path.read_text())
    if state.get("app_id") != APP_ID or state.get("schema") != 1:
        raise ValueError("Unrecognized installation metadata.")
    for key in ("current", "previous"):
        if state.get(key) is not None:
            release_path(root, state[key])
    return state


def release_path(root, release):
    if not isinstance(release, str) or not release or release in (".", "..") or Path(release).name != release:
        raise ValueError("Invalid release identifier.")
    target = root / "releases" / release
    if (root / "releases").is_symlink() or target.is_symlink():
        raise ValueError("Refusing symbolic-link release directory.")
    return target


def inventory(bundle):
    entries = {}
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle).as_posix()
        if relative == ".pbs-release.json":
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(bundle.resolve()):
                raise ValueError(f"Bundle link escapes its directory: {relative}")
            entries[relative] = {"link": os.readlink(path)}
        elif path.is_file():
            entries[relative] = {"sha256": digest(path), "executable": bool(path.stat().st_mode & 0o111)}
        elif not path.is_dir():
            raise ValueError(f"Unsupported bundle entry: {relative}")
    return entries


def verify_release(bundle):
    if (bundle / ".pbs-release.json").is_symlink():
        raise ValueError("Refusing a symbolic-link release manifest.")
    manifest = json.loads((bundle / ".pbs-release.json").read_text())
    if manifest.get("app_id") != APP_ID or manifest.get("schema") != 1:
        raise ValueError("Unrecognized release manifest.")
    if (not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", "")) or
            not re.fullmatch(r"[0-9a-f]{64}", manifest.get("source_tree_sha256", "")) or
            manifest.get("platform") != "Linux" or not isinstance(manifest.get("source_dirty"), bool)):
        raise ValueError("Invalid release provenance.")
    if hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest() != manifest["bundle_sha256"]:
        raise ValueError("Invalid bundle manifest checksum.")
    if inventory(bundle) != manifest["files"]:
        raise ValueError(f"Release verification failed: {bundle}")
    binary = bundle / EXECUTABLE
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("The package has no executable player.")
    return manifest


def selected_data(root, state, requested):
    if state:
        selected = clean_path(state["data_dir"])
        if requested and clean_path(requested).resolve() != selected.resolve():
            raise ValueError("This install already uses a different data directory. Omit --data-dir to keep it; use a separate install directory for a separate session.")
    elif requested:
        selected = clean_path(requested)
    else:
        raise ValueError("First install requires --data-dir pointing to the session you want to keep (use the existing launcher's --data-dir).")
    if selected.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(selected.resolve()):
        raise ValueError("Install and data directories must be separate, without either containing the other.")
    if selected.exists() and not selected.is_dir():
        raise ValueError("The chosen data directory is not a directory.")
    return str(selected)


class PayloadReader:
    """Expose the appended gzip stream with offsets relative to its start."""
    def __init__(self, source, offset):
        self.source, self.offset = source, offset

    def read(self, size=-1):
        return self.source.read(size)

    def tell(self):
        return self.source.tell() - self.offset

    def seek(self, position, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            position += self.offset
        return self.source.seek(position, whence) - self.offset


def extract_payload(archive, offset, expected_hash, destination):
    with archive.open("rb") as source:
        source.seek(offset)
        result = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
        if result.hexdigest() != expected_hash:
            raise ValueError("Installer payload checksum failed. The active release was not changed.")
        source.seek(offset)
        # Validate all names/types/links before writing; no tar extraction APIs
        # can follow a package-supplied link into an unrelated directory.
        with tarfile.open(fileobj=PayloadReader(source, offset), mode="r:gz") as package:
            members = package.getmembers()
            names = set()
            for entry in members:
                path = PurePosixPath(entry.name)
                if path.is_absolute() or ".." in path.parts or not path.parts or entry.name in names:
                    raise ValueError("Unsafe or duplicate archive path.")
                names.add(entry.name)
                if not (entry.isfile() or entry.isdir() or entry.issym()):
                    raise ValueError("Unsupported archive entry type.")
                target = destination / entry.name
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Archive path escapes its release.")
                if entry.issym() and (Path(entry.linkname).is_absolute() or not (target.parent / entry.linkname).resolve().is_relative_to(destination.resolve())):
                    raise ValueError("Archive link escapes its release.")
            links = {entry.name for entry in members if entry.issym()}
            for entry in members:
                if any(parent.as_posix() in links for parent in PurePosixPath(entry.name).parents):
                    raise ValueError("Archive entry is nested inside a symbolic link.")
            for entry in members:
                target = destination / entry.name
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif entry.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.extractfile(entry) as original, target.open("xb") as output:
                        shutil.copyfileobj(original, output)
                        output.flush()
                        os.fsync(output.fileno())
                    target.chmod(0o755 if entry.mode & 0o111 else 0o644)
            for entry in members:
                if entry.issym():
                    target = destination / entry.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(entry.linkname)


LAUNCHER = '''#!/usr/bin/env python3
import os, pathlib, runpy, sys
root = pathlib.Path(os.path.abspath(__file__)).parent.parent
manager = runpy.run_path(str(root / "installer.py"))
try:
    raise SystemExit(manager["launch"](root, sys.argv[1:]))
except (ValueError, OSError, KeyError) as error:
    print("Broadcast Station: " + str(error), file=sys.stderr)
    raise SystemExit(1)
'''


def decode_desktop_value(value):
    escapes = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    def replace(match):
        if match[1] not in escapes:
            raise ValueError("Unrecognized escape in legacy desktop entry.")
        return escapes[match[1]]
    return re.sub(r"\\(.)", replace, value)


def desktop_target(args, root, data_dir):
    if not args.desktop_entry and not args.desktop_file and not args.adopt_desktop_entry:
        return None
    target = clean_path(args.desktop_file or (Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications" / (APP_ID + ".desktop")))
    if target.is_symlink():
        raise ValueError("Refusing a symbolic-link desktop entry.")
    ownership = "X-PlaguesBroadcastStation-InstallId=" + hashlib.sha256(str(root).encode()).hexdigest()
    original = target.read_bytes() if target.exists() else None
    fields, adopt = {}, False
    if original is not None:
        text = original.decode("utf-8")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(text)
        fields = dict(parser["Desktop Entry"])
        if ownership not in text.splitlines():
            if not args.adopt_desktop_entry or any(line.startswith("X-PlaguesBroadcastStation-Install") for line in text.splitlines()):
                raise ValueError(f"An existing desktop entry belongs to another install; it was not changed: {target}")
            if fields.get("Name") != "Plague's Broadcast Station" or fields.get("Type") != "Application":
                raise ValueError("Legacy adoption requires this application's exact Name and Type.")
            command = shlex.split(decode_desktop_value(fields.get("Exec", "")))
            if (len(command) != 3 or not Path(command[0]).is_absolute() or
                    Path(command[0]).name not in (EXECUTABLE, "run-linux.sh") or command[1] != "--data-dir"):
                raise ValueError("Legacy adoption requires a direct player/run-linux.sh Exec with only an explicit --data-dir; shell wrappers are refused.")
            if clean_path(command[2]).resolve() != Path(data_dir).resolve():
                raise ValueError("Legacy desktop data directory differs from the selected session; nothing was changed.")
            adopt = True
    preserved = {key: fields[key] for key in ("Icon", "Path", "StartupNotify", "StartupWMClass", "Categories") if fields.get(key)}
    if any("\n" in value or "\r" in value for value in preserved.values()):
        raise ValueError("Refusing multiline desktop fields.")
    return {"target": target, "original": original, "fields": preserved, "adopt": adopt}


def write_desktop(plan, root):
    target, original = plan["target"], plan["original"]
    if target.is_symlink() or (target.read_bytes() if target.exists() else None) != original:
        raise ValueError("The desktop entry changed during installation; it was not replaced.")
    # Desktop Entry Exec has its own quoting rules; '%' is a field-code marker.
    executable = str(root / "bin" / APP_ID).replace("%", "%%")
    executable = ''.join("\\" + c if c in '\\"`$' else c for c in executable)
    # Desktop values decode backslashes before Exec's quoted argument parser.
    executable = executable.replace("\\", "\\\\")
    ownership = hashlib.sha256(str(root).encode()).hexdigest()
    fields = {"Icon": str(root / "app-icon.png").replace("\\", "\\\\"), "Categories": "AudioVideo;Audio;", **plan["fields"]}
    content = ("[Desktop Entry]\nType=Application\nName=Plague's Broadcast Station\n"
               f'Exec=python3 "{executable}"\nTerminal=false\n'
               f"Comment=Launch the active installed release with your saved session\nX-PlaguesBroadcastStation-InstallId={ownership}\n")
    content += "".join(key + "=" + value + "\n" for key, value in fields.items())
    target.parent.mkdir(parents=True, exist_ok=True)
    if plan["adopt"]:
        backup = target.with_name(target.name + ".pbs-before-managed-" + hashlib.sha256(original).hexdigest()[:12])
        if backup.is_symlink() or (backup.exists() and backup.read_bytes() != original):
            raise ValueError("Legacy desktop backup already exists with different content; nothing was overwritten.")
        if not backup.exists():
            with backup.open("xb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            backup.chmod(0o600)
        print("Saved legacy desktop entry: " + str(backup))
    atomic_write(target, content.encode(), 0o644)


def describe(root, state):
    if not state:
        return {"install_dir": str(root), "status": "incomplete; rerun installer"}
    result = {"install_dir": str(root), "launcher": str(root / "bin" / APP_ID), "data_dir": state["data_dir"]}
    for key in ("current", "previous"):
        release = state.get(key)
        result[key] = None if not release else {k: v for k, v in json.loads((release_path(root, release) / ".pbs-release.json").read_text()).items() if k != "files"}
    return result


def launch(root, arguments):
    state = read_state(root)
    if not state:
        raise ValueError("Installation is incomplete; rerun the installer.")
    if arguments == ["--installer-info"]:
        print(json.dumps(describe(root, state), indent=2))
        return 0
    if arguments == ["--installer-rollback"]:
        return main(["--install-dir", str(root), "--rollback"])
    bundle = release_path(root, state["current"])
    metadata = json.loads((bundle / ".pbs-release.json").read_text())
    if arguments == ["--version"]:
        print(f"Plague's Broadcast Station {metadata['version']} ({metadata['release']}; source {metadata['source_commit'][:12]})")
        return 0
    if any(arg == "--data-dir" or arg.startswith("--data-dir=") for arg in arguments):
        raise ValueError("The stable launcher preserves its selected data directory. Use a separate install directory for another session.")
    executable = bundle / EXECUTABLE
    os.execv(str(executable), [str(executable), "--data-dir", state["data_dir"], *arguments])


def install(root, data_dir, payload, source):
    state = read_state(root)
    data = selected_data(root, state, data_dir)
    if platform.system() != "Linux" or platform.machine() != payload["architecture"]:
        raise ValueError(f"This installer requires Linux {payload['architecture']}.")
    safe_directory(root / "releases")
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        extract_payload(Path(payload["archive"]), payload["offset"], payload["sha256"], stage)
        manifest = verify_release(stage)
        if manifest["release"] != payload["release"] or manifest["architecture"] != payload["architecture"]:
            raise ValueError("Release identity does not match installer.")
        # Check native loader/dependencies and Python entry point without opening
        # a window or reading/writing the chosen data directory.
        version = subprocess.run([str(stage / EXECUTABLE), "--version"], check=True, timeout=30,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if version.stdout.strip() != "Plague's Broadcast Station " + manifest["version"]:
            raise ValueError("The player version does not match its release manifest.")
        destination = release_path(root, manifest["release"])
        if destination.exists():
            if verify_release(destination) != manifest:
                raise ValueError("A different/corrupt release already uses this identity; nothing was replaced.")
        else:
            os.replace(stage, destination)
        install_tools_from_source(root, source)
        icon = destination / "_internal/gnb_player/assets/broadcast-icon-256.png"
        if icon.is_file():
            if (root / "app-icon.png").is_symlink():
                raise ValueError("Refusing symbolic-link application icon.")
            atomic_write(root / "app-icon.png", icon.read_bytes(), 0o644)
        if not state or state["current"] != manifest["release"]:
            write_json(root / "install.json", {"app_id": APP_ID, "schema": 1, "data_dir": data,
                       "current": manifest["release"], "previous": state["current"] if state else None})
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def install_tools_from_source(root, source):
    safe_directory(root / "bin")
    for target, content, mode in ((root / "installer.py", source.encode(), 0o644),
                                  (root / "bin" / APP_ID, LAUNCHER.encode(), 0o755)):
        if target.is_symlink():
            raise ValueError(f"Refusing symbolic-link tool path: {target}")
        atomic_write(target, content, mode)


def rollback(root):
    state = read_state(root)
    if not state or not state.get("previous"):
        raise ValueError("No previous installed release is available for rollback.")
    verify_release(release_path(root, state["previous"]))
    state["current"], state["previous"] = state["previous"], state["current"]
    write_json(root / "install.json", state)


def main(argv=None, payload=None, source=None):
    parser = argparse.ArgumentParser(description="Linux-only Broadcast Station installer/upgrader. Never starts/stops the player or changes app data.")
    parser.add_argument("--install-dir", type=clean_path, default=Path.home() / ".local/opt" / APP_ID, help="Stable installation root (default: ~/.local/opt/plagues-broadcast-station)")
    parser.add_argument("--data-dir", type=clean_path, help="Existing session directory; required on first install, remembered on upgrade")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="Show current/previous versions, source identity, launcher and data path")
    actions.add_argument("--rollback", action="store_true", help="Atomically switch back to the previous retained release; app data is unchanged")
    parser.add_argument("--desktop-entry", action="store_true", help="Opt in to a stable application-menu entry under XDG_DATA_HOME/applications")
    parser.add_argument("--desktop-file", type=clean_path, help="Opt in to an explicit .desktop path (existing unrelated entries are refused)")
    parser.add_argument("--adopt-desktop-entry", action="store_true", help="Explicit one-time migration of a validated same-app legacy menu entry, with a no-clobber backup; uses --desktop-file or the default menu entry")
    args = parser.parse_args(argv)
    root = args.install_dir
    try:
        if args.status:
            print(json.dumps(describe(root, read_state(root)), indent=2))
            return 0
        if args.rollback and (args.data_dir or args.desktop_entry or args.desktop_file or args.adopt_desktop_entry):
            raise ValueError("Rollback takes only --install-dir; it keeps the saved data path and desktop launcher.")
        if not args.rollback and payload is None:
            raise ValueError("Run a .run package to install/upgrade, or use --status / --rollback.")
        # Validate first-install choices before creating a managed root.
        data = selected_data(root, read_state(root), args.data_dir) if not args.rollback else None
        desktop = desktop_target(args, root, data)
        with locked_root(root, create=not args.rollback):
            if args.rollback:
                rollback(root)
            else:
                install(root, args.data_dir, payload, source)
                if desktop:
                    try:
                        write_desktop(desktop, root)
                    except (ValueError, OSError) as error:
                        raise ValueError("Package installed successfully; optional desktop integration failed. Rerun the installer to retry it. " + str(error)) from error
        print(json.dumps(describe(root, read_state(root)), indent=2))
        print("Launch when ready: " + str(root / "bin" / APP_ID))
        return 0
    except (ValueError, OSError, KeyError, EOFError, configparser.Error, tarfile.TarError, subprocess.SubprocessError) as error:
        print("Installer: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
