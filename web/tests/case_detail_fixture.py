"""Supply real offline case-detail HTTP responses to the browser contract test."""

import http.client
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.web import LocalServer
from liquid_tracer.investigation_boards import link_board
from liquid_tracer.plots import preview_plot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout


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
        # Populate through production plot and registry code, then read the real
        # HTTP contract. No blockchain or Miro requests are allowed here.
        workflow = create_investigation(base / "cases", "workflow", seeds=[tx("a") + ":0"],
                                        run_defaults={"layout_attempts": 1})
        state = graph_state((("a:0", "c"), ("c:0", "b")), seeds=("a:0", "b:0"))
        add_pegout(state, tx("b"))
        saved_case(workflow, state)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            preview_plot(workflow, "pegouts", max_hops=10)
        link_board(workflow, "pegouts", "Peg-out case board", "workflow-board")
        cases["workflow"] = read_case(workflow)["case_id"]
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
