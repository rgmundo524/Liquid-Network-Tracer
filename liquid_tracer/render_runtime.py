"""Local renderer memory budgets and diagnostics that contain no graph text."""

import os
import re
from pathlib import Path

from .common import TraceError


_MIB = 1024 * 1024
_SETTING = "LIQUID_RENDER_HEAP_MB"


def _read(path):
    try:
        return Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return ""


def _number(path):
    value = _read(path).strip()
    return int(value) if re.fullmatch(r"[0-9]{1,20}", value) else None


def _cgroup_available():
    """Include process and ancestor limits on the usual Linux cgroup mounts.

    The mount root is checked even in a namespace whose /proc path refers to
    an inaccessible host hierarchy. Unsupported mounts leave host detection
    intact; an explicit budget remains available on other platforms.
    """
    roots = [(Path("/sys/fs/cgroup"), "memory.max", "memory.current", ""),
             (Path("/sys/fs/cgroup/memory"), "memory.limit_in_bytes", "memory.usage_in_bytes", "memory")]
    groups = []
    for line in _read("/proc/self/cgroup").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            groups.append((parts[1].split(","), Path(parts[2].lstrip("/"))))
    values = []
    for root, limit_name, used_name, controller in roots:
        paths = {root}
        for controllers, relative in groups:
            if controller not in controllers or ".." in relative.parts:
                continue
            current = root / relative
            while current != root:
                paths.add(current)
                current = current.parent
        for path in paths:
            limit, used = _number(path / limit_name), _number(path / used_name)
            if limit is not None:
                # A missing usage counter cannot establish spare capacity, but
                # the known hard limit must still constrain the host estimate.
                values.append(max(0, limit - (used or 0)))
    return values


def _available_bytes():
    values = []
    memory = {}
    for line in _read("/proc/meminfo").splitlines():
        match = re.fullmatch(r"(MemTotal|MemAvailable):\s+([0-9]+)\s+kB", line)
        if match:
            memory[match[1]] = int(match[2]) * 1024
    if "MemAvailable" in memory:
        values.append(memory["MemAvailable"])
    else:
        try:
            pages, size = os.sysconf("SC_AVPHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
            if pages >= 0 and size > 0:
                values.append(pages * size)
        except (OSError, ValueError, AttributeError):
            pass
    if "MemTotal" in memory:
        values.append(memory["MemTotal"])
    values.extend(_cgroup_available())
    return min(values) if values else None


def renderer_heap_mb():
    """Choose half currently available memory, or an explicit old-space MiB cap.

    This is a V8 heap allowance, not a reservation or a cap on total process
    memory. Python, native buffers and Chromium also need room. Re-evaluate for
    each calculation instead of fixing the allowance at shell startup.
    """
    value = os.environ.get(_SETTING, "auto").strip().lower()
    if value != "auto":
        if not re.fullmatch(r"[0-9]{1,10}", value) or not 1 <= int(value) <= 2147483647:
            raise TraceError(f"{_SETTING} must be auto or a positive integer in MiB; set it in devenv.nix")
        return int(value)
    available = _available_bytes()
    if available is None:
        return 1024  # Conservative portable fallback when memory is unreadable.
    budget = available // (2 * _MIB)
    if budget < 1:
        raise TraceError("Too little available memory to start the local renderer; free memory and retry the saved run")
    return min(budget, 2147483647)


def renderer_failure(stderr, returncode, engine, heap_mb):
    """Classify fixed signatures; never echo stderr, labels, paths or tokens."""
    engine = engine if engine in ("ELK", "Mermaid") else "Renderer"
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not isinstance(stderr, str):
        stderr = ""
    excerpt = (stderr[:65536] + stderr[-65536:]).lower()
    status = f"signal {-returncode}" if returncode < 0 else f"exit {returncode}"
    prefix = f"{engine} rendering failed ({status}; heap budget {heap_mb:,} MiB). "
    if any(marker in excerpt for marker in ("javascript heap out of memory", "reached heap limit",
                                            "ineffective mark-compacts near heap limit", "fatalprocessoutofmemory")):
        if engine == "Mermaid":
            return prefix + ("The JavaScript heap was exhausted. Chromium may enforce a lower heap limit than requested; "
                             "try the ELK preview for this saved run. Increasing the requested budget may not help this browser build.")
        return prefix + ("The JavaScript heap was exhausted. Free memory or increase LIQUID_RENDER_HEAP_MB "
                         "in devenv.nix, then retry this saved run.")
    if any(marker in excerpt for marker in ("out of memory", "out-of-memory", "cannot allocate memory")):
        return prefix + "The renderer reported memory exhaustion; free memory and retry this saved run."
    if any(marker in excerpt for marker in ("maximum call stack size exceeded", "stack overflow", "stack_overflow")):
        return prefix + "The layout engine exceeded its call-stack limit; increasing heap memory alone may not resolve it."
    if "timed out" in excerpt and any(marker in excerpt for marker in ("protocol", "runtime.callfunctionon", "runtime.evaluate")):
        return prefix + "A browser protocol operation timed out; check that the generated Puppeteer configuration is being used."
    if any(marker in excerpt for marker in ("could not find chrome", "failed to launch the browser", "no usable sandbox",
                                            "error while loading shared libraries")):
        return prefix + "Chromium could not start; enter the project's devenv shell and check its browser configuration."
    if "parse error" in excerpt or "syntax error in text" in excerpt:
        return prefix + f"{engine} could not parse the generated graph; retain the saved run for inspection."
    if returncode == -6:
        return prefix + "The worker aborted; memory exhaustion or an engine failure is possible, but the cause was not confirmed."
    if returncode == -9:
        return prefix + "The worker was killed; the operating system or another process may have stopped it."
    return prefix + "The renderer did not report a recognized cause; graph complexity or an engine error may be responsible."
