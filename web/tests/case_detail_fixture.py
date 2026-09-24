"""Supply real offline case-detail HTTP responses to the browser contract test."""

import http.client
import json
import tempfile
import threading
from pathlib import Path

from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.web import LocalServer


def main():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        cases = {}
        for name, seeds in (
            ("seeded", ["a" * 64 + ":1", "a" * 64 + ":4", "b" * 64 + ":0"]),
            ("empty", []),
        ):
            case = create_investigation(base / "cases", name, seeds=seeds)
            cases[name] = read_case(case)["case_id"]
        server = LocalServer(base / "cases", base / "assets", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            responses = {}
            for name, identity in cases.items():
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request("GET", "/api/cases/" + identity)
                    response = connection.getresponse()
                    payload = response.read()
                    if response.status != 200:
                        raise AssertionError((response.status, payload))
                    responses[name] = json.loads(payload)
                finally:
                    connection.close()
            print(json.dumps(responses))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    main()
