"""Local renderer memory budgets and diagnostics that contain no graph text."""

import json
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
    """Choose 75% of currently available memory, or an explicit old-space MiB cap.

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
    budget = (available * 3) // (4 * _MIB)
    if budget < 1:
        raise TraceError("Too little available memory to start the local renderer; free memory and retry the saved run")
    return min(budget, 2147483647)


_ELK_FAILURE_DETAILS = {
    "elk_invalid_request": "The local ELK worker rejected its generated layout request.",
    "elk_invalid_output": "The local ELK worker could not validate or encode the engine's returned layout.",
    "elk_worker_setup": "The local ELK worker could not load or initialize its engine; check the pinned Node executable and local layout dependencies.",
    "elk_input_order": "ELK did not preserve valid connector attachment ordering.",
    "elk_unsupported_graph": "ELK reported an unsupported graph structure.",
    "elk_unsupported_configuration": "ELK rejected a layout configuration.",
    "elk_index_error": "ELK reported an internal index or array-size error.",
    "elk_illegal_state": "ELK reported an invalid internal layout state.",
    "elk_illegal_argument": "ELK reported an invalid argument during layout calculation.",
    "elk_null_pointer": "ELK reported an internal missing-value error.",
    "elk_assertion": "ELK reported an internal assertion failure.",
    "elk_type_error": "ELK reported an internal type error.",
    "elk_reference_error": "ELK reported an internal reference error.",
    "elk_engine_error": "The local ELK worker reported an unclassified engine error; its cause was not confirmed.",
}
RENDERER_FAILURE_CODES = frozenset(_ELK_FAILURE_DETAILS) | frozenset({
    "heap_exhausted", "memory_exhausted", "stack_limit", "browser_timeout", "browser_launch",
    "graph_parse", "worker_aborted", "worker_killed", "unknown_exit",
})
_ELK_STAGES = {
    "load_engine": "loading the local engine",
    "read_request": "reading the request", "validate_request": "validating the request",
    "geometry_layout": "calculating the geometry layout", "order_constraints": "preparing attachment ordering",
    "traced_first_layout": "calculating the traced-first layout",
    "validate_input_order": "validating attachment ordering", "serialize": "encoding the layout result",
}


def _stderr_excerpt(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    if not isinstance(stderr, str):
        stderr = ""
    return stderr if len(stderr) <= 131072 else stderr[:65536] + "\n" + stderr[-65536:]


def _elk_failure_diagnostic(stderr):
    """Read only a bounded, allowlisted diagnostic; never return worker strings."""
    prefix = "LIQUID_ELK_FAILURE "
    for line in reversed(_stderr_excerpt(stderr).splitlines()):
        if not line.startswith(prefix) or len(line) > 4096:
            continue
        try:
            payload = json.loads(line[len(prefix):])
        except (ValueError, RecursionError):
            continue
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            continue
        code = payload.get("code")
        if not isinstance(code, str) or code not in RENDERER_FAILURE_CODES:
            continue
        result = {"code": code}
        stage = payload.get("stage")
        if isinstance(stage, str) and stage in _ELK_STAGES:
            result["stage"] = stage
        seed = payload.get("seed")
        if type(seed) is int and 1 <= seed <= 2147483647:
            result["seed"] = seed
        profile = payload.get("branch_profile")
        if profile in ("balanced", "flow_weighted"):
            result["branch_profile"] = profile
        policy = payload.get("input_order_policy")
        if policy in ("geometry", "traced_first"):
            result["input_order_policy"] = policy
        return result
    return {}


def renderer_failure_code(stderr, returncode, engine="ELK"):
    """Return an allowlisted cause code, including worker exceptions when available."""
    excerpt = _stderr_excerpt(stderr).lower()
    # Fatal runtime signatures take precedence over a caught worker exception.
    if any(marker in excerpt for marker in ("javascript heap out of memory", "reached heap limit",
                                            "ineffective mark-compacts near heap limit", "fatalprocessoutofmemory")):
        return "heap_exhausted"
    if any(marker in excerpt for marker in ("out of memory", "out-of-memory", "cannot allocate memory")):
        return "memory_exhausted"
    if any(marker in excerpt for marker in ("maximum call stack size exceeded", "stack overflow", "stack_overflow", "stackoverflowerror", "too much recursion")):
        return "stack_limit"
    if engine == "ELK":
        diagnostic = _elk_failure_diagnostic(stderr)
        if diagnostic:
            return diagnostic["code"]
        if any(marker in excerpt for marker in ("err_module_not_found", "module_not_found",
                                                "err_package_path_not_exported", "err_unknown_file_extension",
                                                "err_require_esm", "syntaxerror:")):
            return "elk_worker_setup"
    if "timed out" in excerpt and any(marker in excerpt for marker in ("protocol", "runtime.callfunctionon", "runtime.evaluate")):
        return "browser_timeout"
    if any(marker in excerpt for marker in ("could not find chrome", "failed to launch the browser", "no usable sandbox",
                                            "error while loading shared libraries")):
        return "browser_launch"
    if "parse error" in excerpt or "syntax error in text" in excerpt:
        return "graph_parse"
    if returncode == -6:
        return "worker_aborted"
    if returncode == -9:
        return "worker_killed"
    return "unknown_exit"


def renderer_failure(stderr, returncode, engine, heap_mb):
    """Classify fixed signatures; never echo stderr, labels, paths or tokens."""
    engine = engine if engine in ("ELK", "Mermaid") else "Renderer"
    status = f"signal {-returncode}" if returncode < 0 else f"exit {returncode}"
    prefix = f"{engine} rendering failed ({status}; heap budget {heap_mb:,} MiB). "
    code = renderer_failure_code(stderr, returncode, engine)
    details = {
        "heap_exhausted": ("The JavaScript heap was exhausted. Chromium may enforce a lower heap limit than requested; "
                           "try the ELK preview for this saved run. Increasing the requested budget may not help this browser build.")
                          if engine == "Mermaid" else
                          ("The JavaScript heap was exhausted. Free memory or increase LIQUID_RENDER_HEAP_MB "
                           "in devenv.nix, then retry this saved run."),
        "memory_exhausted": "The renderer reported memory exhaustion; free memory and retry this saved run.",
        "stack_limit": "The layout engine exceeded its call-stack limit; increasing heap memory alone may not resolve it.",
        "browser_timeout": "A browser protocol operation timed out; check that the generated Puppeteer configuration is being used.",
        "browser_launch": "Chromium could not start; enter the project's devenv shell and check its browser configuration.",
        "graph_parse": f"{engine} could not parse the generated graph; retain the saved run for inspection.",
        "worker_aborted": "The worker aborted; memory exhaustion or an engine failure is possible, but the cause was not confirmed.",
        "worker_killed": "The worker was killed; the operating system or another process may have stopped it.",
        "unknown_exit": "The renderer did not report a recognized cause; graph complexity or an engine error may be responsible.",
        **_ELK_FAILURE_DETAILS,
    }
    message = prefix + details[code]
    diagnostic = _elk_failure_diagnostic(stderr) if engine == "ELK" else {}
    context = []
    if "stage" in diagnostic:
        context.append(_ELK_STAGES[diagnostic["stage"]])
    if "seed" in diagnostic:
        context.append(f"seed {diagnostic['seed']}")
    if "branch_profile" in diagnostic:
        context.append({"balanced": "balanced profile", "flow_weighted": "flow-weighted profile"}[diagnostic["branch_profile"]])
    if context:
        message += " Worker context: " + "; ".join(context) + "."
    return message
