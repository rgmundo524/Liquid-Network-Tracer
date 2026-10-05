"""Delete one explicitly confirmed local investigation, retaining shared data.

The atomic move is the commit point. A small receipt survives deletion; an
interrupted cleanup remains hidden beneath .deleted-investigations and never
appears as a partially deleted investigation in the workspace.
"""

import fcntl
import json
import os
import re
import shutil
import stat
import sys
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .common import StopRun, TraceError, now
from .investigations import _validate_case

_DELETED = ".deleted-investigations"
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _path(value):
    path = Path(value).expanduser().absolute()
    if ".." in path.parts:
        raise TraceError("Investigation deletion paths must not contain parent traversal")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise TraceError("Investigation deletion paths must not contain symbolic links")
    return path


@contextmanager
def _directory(path):
    """Open every ancestor without following links, then pin the directory."""
    descriptor = os.open(path.anchor, _DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _child_directory(parent, name):
    descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def operation_guard(case):
    """Keep an existing investigation alive for a CLI operation's full lifetime.

    Directory flocks need no new files and permit concurrent ordinary workers.
    Initial trace commands may create a missing/uninitialized case themselves;
    their existing trace.lock still protects the entire collection operation.
    """
    if case is None:
        yield
        return
    case = _path(case)
    with ExitStack() as operation:
        try:
            parent_fd = operation.enter_context(_directory(case.parent))
            case_fd = operation.enter_context(_child_directory(parent_fd, case.name))
            metadata = os.stat("case.json", dir_fd=case_fd, follow_symlinks=False)
        except FileNotFoundError:
            # No initialized investigation exists yet. In particular, do not
            # create case/lock files just to guard a read-only CLI operation.
            yield
            return
        except OSError as error:
            raise TraceError("Unable to protect this investigation for the operation") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise TraceError("Investigation metadata must be an ordinary file, not a link")
        try:
            fcntl.flock(case_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("Investigation deletion is active; reload the workspace after it finishes") from None
        try:
            _same_directory(parent_fd, case.name, case_fd)
        except (OSError, TraceError):
            raise TraceError("The investigation was deleted or replaced; reload the workspace") from None
        yield


def _ordinary(descriptor, label):
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise TraceError(f"Investigation {label} must be an ordinary file, not a link")


def _metadata(case_fd):
    descriptor = os.open("case.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=case_fd)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        _ordinary(stream.fileno(), "metadata")
        return _validate_case(json.load(stream))


@contextmanager
def _lock(case_fd, name):
    descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=case_fd)
    try:
        _ordinary(descriptor, "lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TraceError("An investigation operation is active; delete it after all its tasks finish") from None
        yield
    finally:
        os.close(descriptor)


def _mount_points():
    """Include Linux bind mounts, which st_dev and ismount alone can miss."""
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), line.split()[4]))
            for line in lines if len(line.split()) >= 5}


def _check_tree(case_fd, case):
    device = os.fstat(case_fd).st_dev
    mounts = _mount_points()
    if any(path == case or case in path.parents for path in mounts) or os.path.ismount(case):
        raise TraceError("Investigation deletion cannot remove mounted directories; unmount them first")
    # fwalk does not follow symlinks, and pins each inspected directory. Files
    # behind export/evidence symlinks are never visited or read.
    for _, directories, _, descriptor in os.fwalk(".", follow_symlinks=False, dir_fd=case_fd):
        if os.fstat(descriptor).st_dev != device:
            raise TraceError("Investigation deletion cannot cross filesystem mounts")
        for name in directories:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and info.st_dev != device:
                raise TraceError("Investigation deletion cannot cross filesystem mounts")


def _same_directory(parent, name, descriptor):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    pinned = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise TraceError("The investigation directory changed; reload the workspace before deleting")


def _receipt(directory_fd, document):
    descriptor = os.open("receipt.json.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace("receipt.json.tmp", "receipt.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    os.fsync(directory_fd)


def _cleanup(directory_fd):
    # On supported Unix platforms rmtree uses file descriptors throughout and
    # unlinks symlinks instead of traversing their targets.
    shutil.rmtree("investigation", dir_fd=directory_fd)
    os.fsync(directory_fd)


def delete_investigation(root, case, *, case_id, confirm_name, progress=None):
    """Permanently delete local case files after exact identity/name confirmation.

    Shared collection data, other cases and remote Miro boards are untouched.
    The caller runs this in a task worker and reserves the investigation against
    newly submitted work. Existing cross-process jobs are protected by locks.
    """
    if not isinstance(case_id, str) or not re.fullmatch(r"[0-9a-f]{32}", case_id):
        raise TraceError("Confirm the selected investigation identity before deleting")
    if not isinstance(confirm_name, str) or not confirm_name:
        raise TraceError("Type the exact investigation name to confirm deletion")
    root, case = _path(root), _path(case)
    if case.parent != root or case.name.startswith("."):
        raise TraceError("Only an investigation directly inside this workspace can be deleted")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise TraceError("This platform does not support safe investigation deletion")
    try:
        with ExitStack() as operation:
            root_fd = operation.enter_context(_directory(root))
            case_fd = operation.enter_context(_child_directory(root_fd, case.name))
            if os.fstat(case_fd).st_dev != os.fstat(root_fd).st_dev:
                raise TraceError("Investigation deletion cannot remove a mounted directory")
            try:
                fcntl.flock(case_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TraceError("An investigation operation is active; delete it after all its tasks finish") from None
            operation.enter_context(_lock(case_fd, "trace.lock"))
            operation.enter_context(_lock(case_fd, "case.lock"))
            metadata = _metadata(case_fd)
            if "shared_dataset" in metadata:
                raise TraceError("The shared collection cannot be deleted as an investigation")
            if metadata["case_id"] != case_id:
                raise TraceError("The selected investigation changed; reload before deleting")
            if metadata.get("name") != confirm_name:
                raise TraceError("Type the exact current investigation name to confirm deletion")
            if progress:
                progress({"phase": "deleting_investigation", "completed": 0, "total": 1})
            _check_tree(case_fd, case)
            try:
                os.mkdir(_DELETED, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
            trash_fd = operation.enter_context(_child_directory(root_fd, _DELETED))
            if (os.fstat(trash_fd).st_dev != os.fstat(root_fd).st_dev
                    or root / _DELETED in _mount_points()):
                raise TraceError("Investigation deletion records must be on the workspace filesystem")
            token = case_id + "-" + uuid.uuid4().hex
            os.mkdir(token, 0o700, dir_fd=trash_fd)
            receipt_fd = operation.enter_context(_child_directory(trash_fd, token))
            receipt = {"schema_version": 1, "case_id": case_id, "name": confirm_name,
                       "original_directory": case.name, "deleted_at": now(), "status": "cleanup_pending"}
            _receipt(receipt_fd, receipt)
            os.fsync(trash_fd)
            _same_directory(root_fd, case.name, case_fd)
            # Recheck authority immediately before the irreversible atomic move.
            current = _metadata(case_fd)
            if current.get("case_id") != case_id or current.get("name") != confirm_name or "shared_dataset" in current:
                raise TraceError("The investigation changed; reload before deleting")
            move_error = None
            try:
                os.rename(case.name, "investigation", src_dir_fd=root_fd, dst_dir_fd=receipt_fd)
            except (OSError, KeyboardInterrupt) as error:
                # A signal can arrive after rename succeeded but before Python
                # returned from it. Inspect the pinned inode before deciding
                # whether the investigation is still present.
                try:
                    _same_directory(receipt_fd, "investigation", case_fd)
                except (OSError, TraceError):
                    raise error
                move_error = error
            # Do not remove a replacement directory if another local process
            # swapped the path around the atomic move.
            _same_directory(receipt_fd, "investigation", case_fd)
            pending = False
            try:
                if move_error is not None:
                    raise move_error
                os.fsync(root_fd)
                os.fsync(receipt_fd)
                _cleanup(receipt_fd)
                _receipt(receipt_fd, {**receipt, "status": "deleted"})
            except (OSError, TraceError, KeyboardInterrupt) as error:
                # Deletion has committed. Never describe a cleanup failure or
                # late cancellation as an intact, canceled investigation.
                pending = True
                print("Investigation removed from the workspace; local file cleanup is pending "
                      f"in {_DELETED}/{token}: {type(error).__name__}", file=sys.stderr)
            if progress:
                try:
                    progress({"phase": "deleting_investigation", "completed": 1, "total": 1})
                except (StopRun, KeyboardInterrupt):
                    pass
            return {"case_id": case_id, "name": confirm_name, "deleted": True, "cleanup_pending": pending}
    except (OSError, ValueError) as error:
        raise TraceError("Unable to delete this investigation safely; check its files and permissions") from error
