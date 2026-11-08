from urllib.parse import quote

import httpx

from app.models import Arrival

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class ServerUnavailable(Exception):
    """Connection failure, timeout, 5xx/unexpected status, or an unparseable response."""


class RouteNotFound(Exception):
    """Server answered 404 (unknown route or trip). Not an outage: callers should not retry."""


class ServerClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params):
        try:
            r = await self._client.get(self._base + path, params=params or None, timeout=TIMEOUT)
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
                    realtime=bool(a["isRealtime"]), vehicle_id=a.get("vehicleId"),
                    possibly_passed=bool(a.get("possiblyPassed", False)),
                )
                for stop, arrs in data.items() for a in arrs
            ]
        except (AttributeError, KeyError, TypeError) as e:
            raise ServerUnavailable(f"unexpected arrivals shape: {e!r}") from e

    async def search_routes(self, query: str) -> list[dict]:
        return await self._get("/v1/routes", query=query)

    async def route_stops(self, route: str) -> dict[str, list[dict]]:
        return (await self._get(f"/v1/routes/{quote(route, safe='')}/stops"))["directions"]

    async def trip_calls(self, trip_id: str) -> list[dict]:
        return (await self._get(f"/v1/trips/{quote(trip_id, safe='')}"))["stopCalls"]

    async def status(self) -> dict:
        return await self._get("/v1/status")


def _int(v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise TypeError(f"expected int, got {v!r}")
    return v
