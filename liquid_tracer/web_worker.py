"""Private subprocess adapter: terminal credentials stay outside the HTTP server."""

import contextlib
import io
import json
import os
import signal
import sys
from pathlib import Path

from .cli import main as cli_main
from .miro_conflicts import public_report as public_edit_conflicts
from .progress import ProgressReporter


def _interrupt(*_):
    raise KeyboardInterrupt


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        return 2
    request, result = map(Path, arguments)
    # Unwind CLI cleanup (especially Mermaid's separate renderer group) before
    # the server applies its bounded force-kill fallback.
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        payload = json.loads(request.read_text(encoding="utf-8"))
        output = io.StringIO()
        edit_conflicts = None

        def diagnostic(value):
            # Only this bounded, known diagnostic can leave the CLI. Provider
            # messages and arbitrary exception strings remain in the terminal.
            nonlocal edit_conflicts
            clean = public_edit_conflicts(value)
            if clean is not None and edit_conflicts is None:
                edit_conflicts = clean

        with contextlib.redirect_stdout(output):
            status = cli_main(payload["arguments"], progress=ProgressReporter(request.parent / "progress.json"),
                              diagnostics=diagnostic)
        # Errors and provider diagnostics go to the actual terminal, never an API
        # response. The separately typed edit report is the only failure detail.
        if status != 0 and output.getvalue():
            print(output.getvalue(), file=sys.stderr)
        report = json.loads(output.getvalue()) if status == 0 else None
        document = {"ok": status == 0, "result": report}
        if status != 0 and edit_conflicts is not None:
            document["edit_conflicts"] = edit_conflicts
        with result.open("x", encoding="utf-8") as stream:
            os.chmod(result, 0o600)
            json.dump(document, stream)
        return status
    except KeyboardInterrupt:
        print("Local web action interrupted.", file=sys.stderr)
        return 130
    except Exception as error:
        print("Local web action failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
