"""A tiny local stand-in for the GitHub REST API used by the test suite."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Each entry maps a request path to (HTTP status, response body). Tests can
# mutate the dict while the server is running to change upstream answers.
ApiResponses = dict[str, tuple[int, str]]


class _FakeApiHandler(BaseHTTPRequestHandler):
    api_responses: ApiResponses

    def do_GET(self) -> None:
        status, body = self.api_responses.get(self.path, (404, "{}"))
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def commit_body(sha: str) -> str:
    return json.dumps({"sha": sha})


def pull_body(sha: str) -> str:
    return json.dumps({"head": {"sha": sha}})


class FakeGitHubApi:
    """Serve canned GitHub API responses on a loopback port."""

    def __init__(self, api_responses: ApiResponses) -> None:
        self.api_responses = api_responses
        handler = type(
            "FakeApiHandler", (_FakeApiHandler,), {"api_responses": api_responses}
        )
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
