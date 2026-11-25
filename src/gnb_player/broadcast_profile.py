"""Explicit native credential storage for one reusable stream profile per data dir.

Ordinary base server settings can be saved without a credential backend. Keys
and complete URLs containing credentials are saved only through the explicit
native credential path. Its marker contains a schema, active slot and random
revision token; the token refuses stale native fallback copies.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
import secrets
from pathlib import Path
import sys
from urllib.parse import urlsplit, unquote

from .settings import atomic_json

SERVICE = "PlaguesBroadcastStation.BroadcastProfile.v1"
PUBLIC_APPLICATIONS = frozenset({"live", "live2", "app", "application"})
PUBLIC_APPLICATION_PATHS = frozenset({"", "/"} | {
    suffix for app in PUBLIC_APPLICATIONS for suffix in ("/" + app, "/" + app + "/")
})


class ProfileError(ValueError):
    def __init__(self, message, *, credentials_deleted=False):
        super().__init__(message)
        self.credentials_deleted = credentials_deleted


@dataclass(frozen=True)
class StreamProfile:
    destination: str = field(repr=False)
    stream_key: str = field(default="", repr=False)
    picture_index: int = 0
    bitrate_kbps: int = 4500

    def validate(self):
        if (not isinstance(self.destination, str) or not 1 <= len(self.destination) <= 8192 or
                any(character.isspace() or ord(character) < 32 for character in self.destination)):
            raise ProfileError("Enter a valid RTMP or RTMPS streaming destination.")
        port = None
        try:
            target = urlsplit(self.destination)
            port = target.port
            valid = target.scheme.lower() in {"rtmp", "rtmps"} and target.hostname and not target.fragment
        except ValueError:
            valid = False
        if not valid or (port is not None and not 1 <= port <= 65535):
            raise ProfileError("Enter a valid RTMP or RTMPS streaming destination.")
        if (not isinstance(self.stream_key, str) or len(self.stream_key) > 8192 or
                any(character.isspace() or ord(character) < 32 for character in self.stream_key)):
            raise ProfileError("Enter a valid stream key, or leave it empty when the complete target is in Destination.")
        if type(self.picture_index) is not int or self.picture_index not in {0, 1}:
            raise ProfileError("Choose a supported broadcast picture size.")
        if type(self.bitrate_kbps) is not int or not 1000 <= self.bitrate_kbps <= 100000:
            raise ProfileError("Choose a video bitrate from 1000 to 100000 kbps (100 Mbps).")

    def _encode(self, revision):
        self.validate()
        return json.dumps({"schema": 2, "credential_revision": revision, "destination": self.destination, "stream_key": self.stream_key,
                           "picture_index": self.picture_index, "bitrate_kbps": self.bitrate_kbps}, separators=(",", ":"))

    @classmethod
    def _decode(cls, value, revision):
        try:
            if not isinstance(value, str) or len(value) > 40000:
                raise ValueError
            data = json.loads(value)
            if not isinstance(data, dict) or type(data.get("schema")) is not int or data.get("schema") != 2 or data.get("credential_revision") != revision:
                raise ValueError
            result = cls(data["destination"], data["stream_key"], data["picture_index"], data["bitrate_kbps"])
            result.validate()
            return result
        except (TypeError, ValueError, KeyError):
            raise ProfileError("The saved protected profile is unavailable or invalid. Re-enter it and Save profile, or Forget it.") from None


@dataclass(frozen=True)
class SavedStreamSettings:
    """Public profile and an explicit selection of protected credential storage."""
    profile: StreamProfile
    remember: bool = False


def public_destination(destination: str) -> bool:
    """Recognize only known static server/app paths for ordinary storage.

    An arbitrary single path segment might itself be a stream key. Custom
    application names, inline keys and authenticated URLs therefore require
    explicit protected storage, even when their syntax resembles a server URL.
    """
    try:
        target = urlsplit(destination)
        path = unquote(target.path)
        return (target.scheme.lower() in {"rtmp", "rtmps"} and bool(target.hostname)
                and target.username is None and target.password is None
                and "%" not in target.netloc and not target.query and not target.fragment
                and path in PUBLIC_APPLICATION_PATHS)
    except (TypeError, ValueError):
        return False


def native_backend():
    """Allowlist native protected backends; never use automatic selection."""
    try:
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring
            backend = WinVaultKeyring()
            backend.persist = "local machine"
            return backend
        if sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
            return Keyring()
    except Exception:
        pass
    raise ProfileError("Protected credential storage is unavailable. Unlock the desktop keyring and retry; current fields can still be used for this session.") from None


class BroadcastProfileStore:
    def __init__(self, data_dir, *, backend_factory=None, marker_writer=None):
        self.data_dir = Path(data_dir)
        canonical = os.path.normcase(str(self.data_dir.resolve()))
        self._account = hashlib.sha256(canonical.encode()).hexdigest()
        self.marker = self.data_dir / "broadcast-profile.json"
        self.protected_attempt_file = self.data_dir / "broadcast-profile-attempt.json"
        self.settings_file = self.data_dir / "broadcast-settings.json"
        self._factory = backend_factory or native_backend
        self._write_marker = marker_writer or atomic_json

    def _service(self, slot):
        # WinVault public APIs have a per-service primary/compound fallback.
        # Give each data dir AND slot a separate service; the revision binding
        # below also refuses a stale compound credential after primary loss.
        return SERVICE + "." + self._account + "." + str(slot)

    def _username(self, slot):
        return self._account + ":" + str(slot)

    @contextmanager
    def _lock(self, filename=".broadcast-profile.lock"):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock = self.data_dir / filename
        if lock.is_symlink():
            raise ProfileError("The saved profile is busy or unavailable. Retry after the other profile operation finishes.")
        with lock.open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    if handle.seek(0, os.SEEK_END) == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ProfileError("Another profile operation is in progress. Retry after it finishes.") from None
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def transaction(self):
        """Serialize a whole operator save/restore/forget across app windows.

        Protected credentials and ordinary settings have individual atomic
        operations, but their combined selection must be held as one operation.
        Use this outer lock around those calls; its separate file permits the
        individual methods to retain their existing inner locks.
        """
        from .studio_settings import studio_lock
        with studio_lock(self.data_dir), self._lock(".broadcast-profile-operation.lock"):
            yield

    def _read_marker(self):
        try:
            if self.marker.is_symlink():
                raise ValueError
            if not self.marker.exists():
                return None
            if self.marker.stat().st_size > 1024:
                raise ValueError
            data = json.loads(self.marker.read_text())
            if (not isinstance(data, dict) or set(data) != {"schema", "active_slot", "credential_revision"} or type(data["schema"]) is not int or data["schema"] != 2 or
                    type(data["active_slot"]) is not int or data["active_slot"] not in {0, 1} or
                    not isinstance(data["credential_revision"], str) or len(data["credential_revision"]) != 64 or
                    any(c not in "0123456789abcdef" for c in data["credential_revision"])):
                raise ValueError
            return data["active_slot"], data["credential_revision"]
        except (OSError, ValueError, TypeError):
            raise ProfileError("The saved profile reference could not be read. No protected profile was changed.") from None

    def _check_forget_marker(self):
        """Explicit Forget can recover corruption, but never another format."""
        if not self.marker.exists() and not self.marker.is_symlink():
            return
        try:
            if (self.marker.is_symlink() or not self.marker.is_file() or self.marker.stat().st_size > 1024 or
                    (hasattr(os, 'getuid') and self.marker.stat().st_uid != os.getuid())):
                raise ProfileError("The saved profile reference is not an owned regular file. Nothing was forgotten.")
            try:
                data = json.loads(self.marker.read_text())
            except ValueError:
                return  # A truncated regular marker is recoverable by Forget.
            if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 2:
                raise ProfileError("The saved profile reference uses an unknown format. Use its matching app version to forget it; nothing was changed.")
        except OSError:
            raise ProfileError("The saved profile reference could not be inspected. Nothing was forgotten.") from None

    def _check_attempt_file(self):
        """The attempt reference contains only a schema; never credential data."""
        path = self.protected_attempt_file
        if not path.exists() and not path.is_symlink():
            return
        try:
            if (path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 or
                    (hasattr(os, 'getuid') and path.stat().st_uid != os.getuid())):
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {"schema"} or type(data["schema"]) is not int or data["schema"] != 1:
                raise ValueError
        except (OSError, UnicodeError, ValueError, TypeError):
            raise ProfileError("The protected save-attempt reference is unavailable or invalid. No protected credentials were changed.") from None

    def _read_settings(self):
        try:
            path = self.settings_file
            if path.is_symlink():
                raise ValueError
            if not path.exists():
                return None
            if not path.is_file() or path.stat().st_size > 16000:
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or not {
                    "schema", "destination", "picture_index", "bitrate_kbps", "remember"}.issubset(data)
                    or type(data["schema"]) is not int or data["schema"] != 1
                    or type(data["remember"]) is not bool):
                raise ValueError
            profile = StreamProfile(data["destination"], "", data["picture_index"], data["bitrate_kbps"])
            # A blank ordinary destination is the intentional protected-only URL.
            if profile.destination == "":
                StreamProfile("rtmps://validation.invalid/app", "", profile.picture_index,
                              profile.bitrate_kbps).validate()
            else:
                profile.validate()
                if not public_destination(profile.destination):
                    raise ValueError
            return SavedStreamSettings(profile, data["remember"])
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise ProfileError("Saved stream settings could not be read. No protected credentials were changed.") from None

    def load_settings(self):
        """Read only ordinary settings; never initialize or contact a keyring."""
        if not self.settings_file.exists() and not self.settings_file.is_symlink():
            return None
        try:
            with self._lock():
                return self._read_settings()
        except ProfileError:
            raise
        except OSError:
            raise ProfileError("Saved stream settings could not be read. No protected credentials were changed.") from None

    def save_settings(self, profile: StreamProfile, *, remember=False):
        """Save ordinary settings, omitting keys and protected-only destinations.

        The caller must complete save(profile) first when remember is true.
        This method never writes or reads native credentials itself.
        """
        profile.validate()
        if type(remember) is not bool:
            raise ProfileError("Choose whether to remember credentials in protected storage.")
        safe_url = public_destination(profile.destination)
        # Even a known app name must not duplicate the whole entered key.
        # Keep this conservative check separate from URL classification: only
        # the explicit save operation has access to the session's key field.
        if profile.stream_key and unquote(profile.stream_key) == unquote(urlsplit(profile.destination).path).strip("/"):
            safe_url = False
        if not safe_url and not remember:
            raise ProfileError("To save this destination, put only the server/app URL in Destination and the key in Stream key, "
                               "or choose Remember in protected storage. Only the static app paths live, live2, app, application "
                               "or an empty path can be saved without protected storage; custom app names and URLs "
                               "with authentication, query parameters or an inline key require protected storage.")
        try:
            with self._lock():
                # Refuse unknown formats or links, preserving their existing data.
                self._read_settings()
                previous = json.loads(self.settings_file.read_text()) if self.settings_file.exists() else {}
                atomic_json(self.settings_file, {
                    **previous, "schema": 1, "destination": profile.destination if safe_url else "",
                    "picture_index": profile.picture_index, "bitrate_kbps": profile.bitrate_kbps,
                    "remember": remember,
                })
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Could not save ordinary stream settings. Retry Save; no key was written to ordinary settings.") from None

    def forget_settings(self):
        """Forget ordinary settings only; native credentials need explicit forget()."""
        try:
            with self._lock():
                self._read_settings()
                self.settings_file.unlink(missing_ok=True)
        except ProfileError:
            raise
        except OSError:
            raise ProfileError("Could not forget ordinary stream settings. Retry Forget.") from None

    def load(self):
        # Fresh installs and ordinary playback never probe the native keyring.
        if not self.marker.exists() and not self.marker.is_symlink():
            return None
        try:
            with self._lock():
                selected = self._read_marker()
                if selected is None:
                    return None
                slot, revision = selected
                backend = self._factory()
                value = backend.get_password(self._service(slot), self._username(slot))
                if value is None:
                    raise ProfileError("The saved protected profile is not available. Re-enter it and Save profile, or Forget it.")
                return StreamProfile._decode(value, revision)
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Could not restore the protected profile. Unlock the desktop keyring and retry; current fields remain session-only.") from None

    def save(self, profile):
        profile.validate()
        try:
            with self._lock():
                active = self._read_marker()
                self._check_attempt_file()
                # Record the possibility of a protected copy before mutation.
                # This survives failed first saves without an active marker,
                # so a restarted window can still offer verified cleanup.
                atomic_json(self.protected_attempt_file, {"schema": 1})
                slot = 1 - active[0] if active is not None else 0
                revision = secrets.token_hex(32)
                backend = self._factory()
                payload = profile._encode(revision)
                backend.set_password(self._service(slot), self._username(slot), payload)
                if backend.get_password(self._service(slot), self._username(slot)) != payload:
                    raise ValueError
                # Never overwrite the active credential before this atomic
                # switch. A failed write leaves the previous selection intact.
                self._write_marker(self.marker, {"schema": 2, "active_slot": slot, "credential_revision": revision})
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Could not finish saving the protected profile. The previous active profile was kept; current fields are session-only. A protected copy may remain—retry Save profile or Forget.") from None

    def forget(self):
        try:
            with self._lock():
                self._check_forget_marker()
                self._check_attempt_file()
                backend = self._factory()
                for slot in (0, 1):
                    account = self._username(slot)
                    if backend.get_password(self._service(slot), account) is not None:
                        backend.delete_password(self._service(slot), account)
                    if backend.get_password(self._service(slot), account) is not None:
                        raise ValueError
                try:
                    self.marker.unlink(missing_ok=True)
                except OSError:
                    raise ProfileError("Protected credentials were deleted, but the profile reference could not be cleared. Retry Forget.", credentials_deleted=True) from None
                try:
                    self.protected_attempt_file.unlink(missing_ok=True)
                except OSError:
                    raise ProfileError("Protected credentials were deleted, but the protected save-attempt reference could not be cleared. Retry Forget.", credentials_deleted=True) from None
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Could not fully forget the protected profile. Some protected data may remain; unlock the keyring and retry. Clear fields only clears this session.") from None
