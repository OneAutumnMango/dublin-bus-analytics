"""Mutable in-memory ASGI stand-in for dublin-bus-server (the /v1 endpoints analytics uses)."""
import json
from urllib.parse import parse_qs, unquote

import httpx


class _FnTransport(httpx.AsyncBaseTransport):
    def __init__(self, fn):
        self.handle_async_request = fn


class FakeServer:
    def __init__(self):
        self.arrivals: dict[str, dict[str, list[dict]]] = {}
        self.routes: list[dict] = []
        self.route_stops: dict[str, dict[str, list[dict]]] = {}
        self.trip_calls: dict[str, list[dict]] = {}
        self.status_code: int | None = None  # forced status for every request
        self.down = False
        self.requests: list[str] = []

    def set_arrivals(self, route, by_stop):
        self.arrivals[route] = by_stop

    def set_routes(self, routes):
        self.routes = routes

    def set_route_stops(self, route, directions):
        self.route_stops[route] = directions

    def set_trip_calls(self, trip_id, calls):
        self.trip_calls[trip_id] = calls

    def set_status(self, code: int | None):
        self.status_code = code

    def set_down(self, down: bool):
        self.down = down

    def client(self, base_url="http://fake") -> httpx.AsyncClient:
        asgi = httpx.ASGITransport(app=self)

        async def handle(request):
            if self.down:  # like a refused connection
                raise httpx.ConnectError("fake server down", request=request)
            return await asgi.handle_async_request(request)

        return httpx.AsyncClient(transport=_FnTransport(handle), base_url=base_url)

    async def __call__(self, scope, receive, send):
        assert scope["type"] == "http"
        path = unquote(scope["path"])
        self.requests.append(path)
        qs = parse_qs(scope["query_string"].decode())
        code, body = self._route(path, qs)
        if self.status_code is not None:
            code, body = self.status_code, {"error": "forced"}
        raw = json.dumps(body).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": raw})

    def _route(self, path, qs):
        p = path.strip("/").split("/")
        nf = (404, {"error": "unknown"})
        if p[:1] != ["v1"]:
            return nf
        p = p[1:]
        if p == ["status"]:
            return 200, {"ok": True}
        if p == ["routes"]:
            q = qs.get("query", [""])[0].lower()
            return 200, [r for r in self.routes if q in r["shortName"].lower()]
        if len(p) == 3 and p[0] == "routes" and p[2] == "arrivals":
            return (200, self.arrivals[p[1]]) if p[1] in self.arrivals else nf
        if len(p) == 3 and p[0] == "routes" and p[2] == "stops":
            if p[1] not in self.route_stops:
                return nf
            return 200, {"routeId": p[1], "shortName": p[1], "directions": self.route_stops[p[1]]}
        if len(p) == 2 and p[0] == "trips":
            return (200, {"tripId": p[1], "stopCalls": self.trip_calls[p[1]]}) if p[1] in self.trip_calls else nf
        return nf
