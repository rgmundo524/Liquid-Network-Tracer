"""Coordinate local dependency setup and publish immutable Astro builds.

The lock directory belongs to one checkout. Servers resolve the published
``web/dist`` symlink once, so rebuilding cannot remove their live assets.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from typing import Iterator
import uuid

from .processes import defer_cancellation_during_spawn


_CACHE_NAME = ".devenv-liquid-builds"
_BUILD_REVISION = b"liquid-web-build-v1\0"


@contextmanager
def _lock(root: Path, name: str) -> Iterator[int]:
    cache = root / _CACHE_NAME
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / f"{name}.lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Waiting for another Liquid Tracer instance to finish {name} setup...", flush=True)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        # npm inherits this descriptor. Close our reference on exit, without
        # LOCK_UN: an installer surviving a forcibly killed launcher must
        # retain ownership until it finishes using shared dependencies.
        yield handle.fileno()


def _run(arguments: list[str], directory: Path, lock_fd: int) -> None:
    process = None
    try:
        with defer_cancellation_during_spawn():
            process = subprocess.Popen(arguments, cwd=directory, pass_fds=(lock_fd,), start_new_session=True)
        returncode = process.wait()
    except BaseException:
        if process is None:
            raise
        # npm can own shell/Node children. Stop the process group before the
        # caller removes staging files or lets another launcher acquire a lock.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # The npm leader may exit before a child that ignores SIGTERM.
            # Reap the leader and stop any remaining descendants as well.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, arguments)


def _dependency_key(directory: Path) -> str:
    # Retain the marker format used by the earlier sha256sum shell scripts,
    # avoiding an unnecessary npm reinstall when upgrading the launcher.
    digest = hashlib.sha256()
    for name in ("package.json", "package-lock.json"):
        file_hash = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        digest.update(f"{file_hash}  {name}\n".encode())
    return digest.hexdigest()


def _install(directory: Path, npm: str, lock_fd: int, *, ignore_scripts: bool) -> None:
    key = _dependency_key(directory)
    marker = directory / "node_modules" / ".liquid-lock"
    if marker.is_file() and marker.read_text().strip() == key:
        return
    arguments = [npm, "ci", "--no-audit", "--no-fund"]
    if ignore_scripts:
        arguments.append("--ignore-scripts")
    _run(arguments, directory, lock_fd)
    if _dependency_key(directory) != key:
        raise RuntimeError("Dependency files changed during setup. Run the launcher again.")
    marker.write_text(key + "\n")


def setup_layout(root: Path, npm: str) -> None:
    with _lock(root, "layout") as lock_fd:
        _install(root / "layout", npm, lock_fd, ignore_scripts=True)


def _source_key(web: Path) -> str:
    # Build only public repository sources. Do not hash .env files or any
    # runtime environment values into a cache shared by local instances.
    digest = hashlib.sha256(_BUILD_REVISION)
    files = [path for path in web.iterdir() if path.is_file() and not path.name.startswith(".")]
    for name in ("src", "public"):
        directory = web / name
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    for path in sorted(files):
        digest.update(path.relative_to(web).as_posix().encode() + b"\0")
        contents = path.read_bytes()
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()


def _publish(web: Path, built: Path, cache: Path) -> None:
    published = web / "dist"
    # Releases before the coordinator used an ordinary dist directory. Keep
    # that build too, but servers launched with those releases need a restart
    # when upgrading because they did not pin their original directory.
    if published.exists() and not published.is_symlink():
        legacy = Path(tempfile.mkdtemp(prefix="legacy-", dir=cache))
        legacy.rmdir()
        published.rename(legacy)
    link = cache / f"publish-{uuid.uuid4().hex}"
    try:
        link.symlink_to(os.path.relpath(built, web), target_is_directory=True)
        os.replace(link, published)
    finally:
        link.unlink(missing_ok=True)


def build_web(root: Path, npm: str) -> Path:
    # Complete layout setup before acquiring the web lock. Standalone layout
    # setup therefore cannot invert a nested lock order and deadlock a build.
    setup_layout(root, npm)
    web = root / "web"
    with _lock(root, "web") as lock_fd:
        _install(web, npm, lock_fd, ignore_scripts=False)
        cache = root / _CACHE_NAME / "web"
        cache.mkdir(parents=True, exist_ok=True)
        key = _source_key(web)
        built = cache / key
        if not (built / "index.html").is_file():
            staging = Path(tempfile.mkdtemp(prefix="building-", dir=cache))
            try:
                _run([npm, "run", "build", "--", "--outDir", str(staging)], web, lock_fd)
                if not (staging / "index.html").is_file():
                    raise RuntimeError("Astro did not produce index.html; the existing UI build was preserved.")
                if _source_key(web) != key:
                    raise RuntimeError("UI sources changed during the build. Run the launcher again.")
                staging.rename(built)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        else:
            print("Reusing the completed Liquid Tracer UI build.", flush=True)
        _publish(web, built, cache)
        return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("layout", "web"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--npm", required=True)
    args = parser.parse_args(argv)
    def cancelled(signum, frame):
        raise KeyboardInterrupt
    previous_sigterm = signal.signal(signal.SIGTERM, cancelled)
    try:
        if args.action == "layout":
            setup_layout(args.root.resolve(), args.npm)
        else:
            build_web(args.root.resolve(), args.npm)
    except KeyboardInterrupt:
        print("Local setup cancelled.", flush=True)
        return 130
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Liquid Tracer setup failed: {error}\n")
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
