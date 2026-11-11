import json

import httpx
import pytest
from hypothesis import given, strategies as st

from app.core.models import Arrival
from app.collect.serverclient import RouteNotFound, ServerClient, ServerUnavailable
from tests.support.fake_server import FakeServer


def A(**kw):
    d = dict(routeShortName="15", destination="Clongriffin", dueMinutes=3, isRealtime=True,
             tripId="t1", routeId="r15", expectedEpochSeconds=1000, delaySeconds=60,
             vehicleId="v1", possiblyPassed=False, scheduledEpochSeconds=940)
    d.update(kw)
    return d


def make(fake=None, base="http://srv"):
    fake = fake or FakeServer()
    return fake, ServerClient(base, fake.client(base))


def mock_client(handler, base="http://srv"):
    return ServerClient(base, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_route_arrivals_flattens_by_stop():
    fake, c = make()
    fake.set_arrivals("15", {"s1": [A(), A(tripId="t2")], "s2": [A(tripId="t3", possiblyPassed=True)]})
    out = await c.route_arrivals("15")
    assert sorted((a.stop_id, a.trip_id) for a in out) == [("s1", "t1"), ("s1", "t2"), ("s2", "t3")]
    a = out[0]
    assert a == Arrival("s1", "t1", "15", 3, 1000, 940, 60, True, "v1", False)
    assert [x.possibly_passed for x in out if x.trip_id == "t3"] == [True]


async def test_missing_scheduled_is_none():
    fake, c = make()
    raw = A()
    for k in ("scheduledEpochSeconds", "delaySeconds", "vehicleId"):
        del raw[k]
    fake.set_arrivals("15", {"s1": [raw]})
    (a,) = await c.route_arrivals("15")
    assert (a.scheduled, a.delay, a.vehicle_id) == (None, None, None)


async def test_null_fields_are_none():
    fake, c = make()
    fake.set_arrivals("15", {"s1": [A(scheduledEpochSeconds=None, delaySeconds=None, vehicleId=None)]})
    (a,) = await c.route_arrivals("15")
    assert (a.scheduled, a.delay, a.vehicle_id) == (None, None, None)


async def test_defaults_for_omitted_server_defaults():
    # Kotlin defaults: tripId="", expectedEpochSeconds=0, possiblyPassed=false
    fake, c = make()
    fake.set_arrivals("15", {"s1": [{"routeShortName": "15", "destination": "x", "dueMinutes": 0, "isRealtime": False}]})
    (a,) = await c.route_arrivals("15")
    assert (a.trip_id, a.expected, a.possibly_passed, a.due_min, a.realtime) == ("", 0, False, 0, False)


async def test_extra_unknown_fields_ignored():
    fake, c = make()
    fake.set_arrivals("15", {"s1": [A(brandNew="x")]})
    assert len(await c.route_arrivals("15")) == 1


async def test_empty_map_and_unset_route():
    fake, c = make()
    fake.set_arrivals("15", {})
    assert await c.route_arrivals("15") == []
    assert await c.route_arrivals("15") == []


async def test_unknown_route_404_raises_route_not_found():
    _, c = make()
    with pytest.raises(RouteNotFound):
        await c.route_arrivals("nope")


async def test_server_500_raises_unavailable():
    fake, c = make()
    fake.set_status(500)
    with pytest.raises(ServerUnavailable):
        await c.route_arrivals("15")


async def test_set_down_raises_unavailable():
    fake, c = make()
    fake.set_down(True)
    with pytest.raises(ServerUnavailable):
        await c.status()
    fake.set_down(False)
    fake.set_status(None)
    assert (await c.status())["ok"] is True


async def test_connection_refused_raises_unavailable():
    def h(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(ServerUnavailable):
        await mock_client(h).route_arrivals("15")


async def test_timeout_raises_unavailable():
    def h(req):
        raise httpx.ReadTimeout("slow")
    with pytest.raises(ServerUnavailable):
        await mock_client(h).status()


@pytest.mark.parametrize("body", [b"not json", b"[]", b'"x"', b'{"s1": 5}', b'{"s1": [1]}',
                                  b'{"s1": [{"routeShortName": "15", "isRealtime": true, "dueMinutes": "soon"}]}', b'{"s1": [{}]}'])
async def test_malformed_or_unexpected_shape_raises_unavailable(body):
    c = mock_client(lambda r: httpx.Response(200, content=body))
    with pytest.raises(ServerUnavailable):
        await c.route_arrivals("15")


async def test_other_4xx_raises_unavailable():
    c = mock_client(lambda r: httpx.Response(400, json={"error": "x"}))
    with pytest.raises(ServerUnavailable):
        await c.route_arrivals("15")


async def test_trailing_slash_base_url_and_path():
    seen = []

    def h(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={})
    c = mock_client(h, base="http://srv:8080/")
    await c.route_arrivals("15")
    assert seen == ["http://srv:8080/v1/routes/15/arrivals"]


@pytest.mark.parametrize("route,enc", [("15/A", "15%2FA"), ("N4", "N4"), ("a b", "a%20b"), ("15?x=1", "15%3Fx%3D1")])
async def test_route_is_url_encoded(route, enc):
    seen = []

    def h(req):
        seen.append(req.url.raw_path.decode())
        return httpx.Response(200, json={})
    await mock_client(h).route_arrivals(route)
    assert seen == [f"/v1/routes/{enc}/arrivals"]


async def test_search_routes_stops_status():
    fake, c = make()
    fake.set_routes([{"id": "r15", "shortName": "15", "longName": "x", "agencyId": "a"}])
    fake.set_route_stops("15", {"0": [{"id": "s1"}], "1": [{"id": "s2"}]})
    assert (await c.search_routes("15"))[0]["shortName"] == "15"
    assert await c.search_routes("zzz") == []
    assert set(await c.route_stops("15")) == {"0", "1"}
    assert (await c.status())["ok"] is True


async def test_search_routes_passes_query():
    seen = []

    def h(req):
        seen.append(req.url.params["query"])
        return httpx.Response(200, json=[])
    await mock_client(h).search_routes("a&b")
    assert seen == ["a&b"]


async def test_unknown_route_stops_not_found():
    _, c = make()
    with pytest.raises(RouteNotFound):
        await c.route_stops("nope")


async def test_default_client_created_and_closeable():
    c = ServerClient("http://127.0.0.1:1")
    with pytest.raises(ServerUnavailable):
        await c.status()
    await c.aclose()


payload = st.dictionaries(
    st.text(min_size=1, max_size=8),
    st.lists(st.fixed_dictionaries(
        {"routeShortName": st.text(max_size=5), "destination": st.text(max_size=5),
         "dueMinutes": st.integers(-5, 10_000), "isRealtime": st.booleans()},
        optional={"tripId": st.text(max_size=5), "routeId": st.text(max_size=5),
                  "expectedEpochSeconds": st.integers(0, 2**40),
                  "delaySeconds": st.none() | st.integers(-10_000, 10_000),
                  "vehicleId": st.none() | st.text(max_size=5), "possiblyPassed": st.booleans(),
                  "scheduledEpochSeconds": st.none() | st.integers(0, 2**40),
                  "extra": st.integers()}), max_size=4),
    max_size=4)


@given(payload)
async def test_any_wellformed_payload_round_trips(p):
    c = mock_client(lambda r: httpx.Response(200, content=json.dumps(p)))
    out = await c.route_arrivals("15")
    assert len(out) == sum(len(v) for v in p.values())
    assert all(isinstance(a, Arrival) for a in out)


async def test_read_timeout_is_configurable():
    seen = []

    class Stub:
        async def get(self, url, params=None, timeout=None):
            seen.append(timeout)
            return httpx.Response(200, json={})

    await ServerClient("http://x", client=Stub(), read_timeout_s=7).status()
    await ServerClient("http://x", client=Stub()).status()
    assert (seen[0].read, seen[0].connect) == (7.0, 5.0)
    assert (seen[1].read, seen[1].connect) == (45.0, 5.0)
