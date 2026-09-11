"""Private subprocess adapter: terminal credentials stay outside the HTTP server."""

import contextlib
import io
import json
import os
import signal
import sys
from pathlib import Path

from .cli import main as cli_main
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
        with contextlib.redirect_stdout(output):
            status = cli_main(payload["arguments"], progress=ProgressReporter(request.parent / "progress.json"))
        # Errors and provider diagnostics go to the actual terminal, never an API
        # response. Only a successful CLI JSON document crosses this boundary.
        if status != 0 and output.getvalue():
            print(output.getvalue(), file=sys.stderr)
        report = json.loads(output.getvalue()) if status == 0 else None
        with result.open("x", encoding="utf-8") as stream:
            os.chmod(result, 0o600)
            json.dump({"ok": status == 0, "result": report}, stream)
        return status
    except KeyboardInterrupt:
        print("Local web action interrupted.", file=sys.stderr)
        return 130
    except Exception as error:
        print("Local web action failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
