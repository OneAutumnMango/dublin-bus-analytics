from urllib.parse import quote

import httpx

from app.core.models import Arrival


class ServerUnavailable(Exception):
    """Connection failure, timeout, 5xx/unexpected status, or an unparseable response."""


class RouteNotFound(Exception):
    """Server answered 404 (unknown route or trip). Not an outage: callers should not retry."""


class ServerClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None,
                 read_timeout_s: float = 45.0):
        self._base = base_url.rstrip("/")
        self._timeout = httpx.Timeout(read_timeout_s, connect=5.0)
        self._client = client or httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params):
        try:
            r = await self._client.get(self._base + path, params=params or None, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise ServerUnavailable(str(e) or type(e).__name__) from e
        if r.status_code == 404:
            raise RouteNotFound(path)
        if r.status_code != 200:
            raise ServerUnavailable(f"HTTP {r.status_code} for {path}")
        try:
            return r.json()
        except ValueError as e:
            raise ServerUnavailable(f"invalid JSON from {path}") from e

    async def route_arrivals(self, route: str) -> list[Arrival]:
        data = await self._get(f"/v1/routes/{quote(route, safe='')}/arrivals")
        try:
            return [
                Arrival(
                    stop_id=stop, trip_id=a.get("tripId", ""), route=a["routeShortName"],
                    due_min=_int(a["dueMinutes"]), expected=_int(a.get("expectedEpochSeconds", 0)),
                    scheduled=a.get("scheduledEpochSeconds"), delay=a.get("delaySeconds"),
                    realtime=bool(a["isRealtime"]),
                )
                for stop, arrs in data.items() for a in arrs
            ]
        except (AttributeError, KeyError, TypeError) as e:
            raise ServerUnavailable(f"unexpected arrivals shape: {e!r}") from e

    async def search_routes(self, query: str) -> list[dict]:
        return await self._get("/v1/routes", query=query)

    async def route_stops(self, route: str) -> dict[str, list[dict]]:
        return (await self._get(f"/v1/routes/{quote(route, safe='')}/stops"))["directions"]

    async def route_shape(self, route: str, direction: str | None = None) -> list[list[float]]:
        """[[lat, lon], ...] of the route's most detailed shape (optionally for one direction id)."""
        params = {"format": "points", **({"direction": direction} if direction is not None else {})}
        pts = (await self._get(f"/v1/routes/{quote(route, safe='')}/shape", **params)).get("points")
        if not isinstance(pts, list):
            raise ServerUnavailable("shape response has no points")
        return pts


def _int(v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"expected int, got {v!r}")
    return v
