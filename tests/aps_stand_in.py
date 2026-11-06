"""A stand-in for the two Autodesk Platform Services APIs the connector uses, on localhost, so no test needs a network or an Autodesk account.

It serves exactly the routes and shapes xerlane/aps.py was written against (the vendor OpenAPI files, see docs/aps.md), and nothing else:

    POST /authentication/v2/token                                   form body, HTTP Basic client id and secret -> {access_token, token_type, expires_in}
    GET  /modelderivative/v2/designdata/{urn}/metadata              -> {data: {type: metadata, metadata: [{name, role, guid, isMasterView?}]}}
    GET  /modelderivative/v2/designdata/{urn}/metadata/{guid}/properties
                                                                   -> 202 {result: success} while "processing", then 200 {data: {type: properties, collection: [...]}}

It is NOT Autodesk's code: it proves the client does what the documentation says, never that Autodesk answers that way. Tests script the awkward answers (429 with
Retry-After, 5xx, an expired token, a redirect, a malformed body) through `script()`.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-secret-DO-NOT-LOG-0123456789"

PROPS_RE = re.compile(r"^/modelderivative/v2/designdata/([^/]+)/metadata/([^/]+)/properties$")
VIEWS_RE = re.compile(r"^/modelderivative/v2/designdata/([^/]+)/metadata$")


class FakeTime:
    """An injectable clock and sleep: tests run in no real time and can read back every wait the client asked for."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class StandIn:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.expires_in = 3599
        self.models: dict[str, dict[str, Any]] = {}          # urn -> {"views": [...], "props": {guid: collection}, "pending": {guid: int}, "raw": {guid: bytes}}
        self.requests: list[tuple[str, str]] = []             # (method, path) of every request, in order
        self.auth_headers: list[str] = []                     # the Authorization header of every request
        self.issued: list[str] = []                           # every token handed out, in order
        self.valid: set[str] = set()
        self.queues: dict[str, list[tuple[int, dict[str, str], bytes]]] = {"token": [], "views": [], "props": []}
        self.token_forms: list[dict[str, list[str]]] = []
        self.regions: list[str | None] = []
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        httpd.daemon_threads = True
        self.httpd = httpd
        self.url = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)     # the default 0.5 s poll makes every shutdown wait half a second
        self.thread.start()

    # -- test controls

    def add_model(self, urn: str, views: list[dict[str, Any]], props: dict[str, list[Any]], pending: dict[str, int] | None = None) -> None:
        self.models[urn] = {"views": views, "props": props, "pending": dict(pending or {}), "raw": {}}

    def script(self, route: str, *answers: tuple[int, dict[str, str], bytes]) -> None:
        """The next requests to `route` (token, views or props) get these answers first, in order, before normal handling."""
        with self.lock:
            self.queues[route].extend(answers)

    def expire_all_tokens(self) -> None:
        with self.lock:
            self.valid.clear()

    def count(self, method: str, path_part: str) -> int:
        return sum(1 for m, p in self.requests if m == method and path_part in p)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- the server

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:             # keep pytest output clean
                pass

            def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, doc: Any, headers: dict[str, str] | None = None) -> None:
                self._send(status, json.dumps(doc).encode(), headers)

            def _scripted(self, route: str) -> bool:
                with outer.lock:
                    q = outer.queues[route]
                    item = q.pop(0) if q else None
                if item is None:
                    return False
                self._send(item[0], item[2], item[1])
                return True

            def do_POST(self) -> None:
                path = urlsplit(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(n).decode())
                with outer.lock:
                    outer.requests.append(("POST", path))
                    outer.auth_headers.append(self.headers.get("Authorization", ""))
                if path != "/authentication/v2/token":
                    return self._json(404, {"developerMessage": "no such route"})
                if self._scripted("token"):
                    return
                want = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    return self._json(401, {"developerMessage": "Invalid client credentials"})
                if self.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded" or form.get("grant_type") != ["client_credentials"]:
                    return self._json(400, {"developerMessage": "unsupported grant_type"})
                outer.token_forms.append(form)
                with outer.lock:
                    token = f"tok{len(outer.issued) + 1}.{'x' * 40}"
                    outer.issued.append(token)
                    outer.valid.add(token)
                self._json(200, {"access_token": token, "token_type": "Bearer", "expires_in": outer.expires_in})

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                with outer.lock:
                    outer.requests.append(("GET", path))
                    outer.auth_headers.append(self.headers.get("Authorization", ""))
                    outer.regions.append(self.headers.get("region"))
                views, props = VIEWS_RE.match(path), PROPS_RE.match(path)
                if not (views or props):
                    return self._json(404, {"developerMessage": "no such route"})
                if self._scripted("views" if views else "props"):
                    return
                bearer = self.headers.get("Authorization", "")
                if not bearer.startswith("Bearer ") or bearer[7:] not in outer.valid:
                    return self._json(401, {"developerMessage": "Access token is invalid or expired"})
                urn = (views or props).group(1)
                model = outer.models.get(urn)
                if model is None:
                    return self._json(404, {"diagnostics": "Model not found"})
                if views:
                    return self._json(200, {"data": {"type": "metadata", "metadata": model["views"]}})
                guid = props.group(2)
                with outer.lock:
                    left = model["pending"].get(guid, 0)
                    if left > 0:
                        model["pending"][guid] = left - 1
                if left > 0:
                    return self._json(202, {"result": "success"})
                if guid in model["raw"]:
                    return self._send(200, model["raw"][guid])
                if guid not in model["props"]:
                    return self._json(404, {"diagnostics": "View not found"})
                self._json(200, {"data": {"type": "properties", "collection": model["props"][guid]}})

        return H


@pytest.fixture()
def aps_server():
    server = StandIn()
    yield server
    server.close()


def make_client(server: StandIn, ft: FakeTime | None = None, **kwargs: Any):
    """A client pointed at the stand-in, with a fake clock and sleep unless given."""
    from xerlane.aps import ApsClient
    ft = ft or FakeTime()
    kwargs.setdefault("clock", ft.clock)
    kwargs.setdefault("sleep", ft.sleep)
    return ApsClient(CLIENT_ID, CLIENT_SECRET, base_url=server.url, **kwargs)


# A translated design as the documentation shows it: one 3d view, one 2d view.
URN = "dXJuOmFkc2sub2JqZWN0czpvcy5vYmplY3Q6dGVzdC9tb2RlbC5ydnQ"
GUID_3D = "1d6e48c5-e4a4-8ca5-5b02-3f2acc354470"
GUID_2D = "eea006f7-042b-c298-d497-9ef4047e8378"
VIEWS = [
    {"name": "New Construction", "role": "3d", "guid": GUID_3D, "isMasterView": True},
    {"name": "Level 1 plan", "role": "2d", "guid": GUID_2D},
]


def obj(objectid: Any, name: str | None = "Wall", external: str | None = None, task_code: str | None = None, **props: Any) -> dict[str, Any]:
    """One entry of data.collection in the documented shape."""
    properties: dict[str, Any] = {"Name": name, **props}
    if task_code is not None:
        properties["Schedule"] = {"task_code": task_code}
    return {"objectid": objectid, "name": name, "externalId": external if external is not None else f"ext-{objectid}", "properties": properties}


def sample_collection() -> list[dict[str, Any]]:
    return [obj(1, "Model"), obj(2, "Wall A", task_code="A1000", **{"__category__": {"Category": "Revit Walls"}}), obj(3, "Slab", task_code="A1010"),
            obj(4, "Door", task_code="NOPE-1")]
