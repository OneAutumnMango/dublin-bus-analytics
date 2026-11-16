import re

import pytest
from playwright.sync_api import expect


def trip_id(seed_end):
    return f"15-{seed_end:%Y%m%d}-0630"


def open_hash(page, hash_):
    page.evaluate("h => { location.hash = h }", hash_)
    expect(page.locator("#main h1").first).to_be_visible()
    expect(page.locator("#main [aria-busy=true]")).to_have_count(0, timeout=60_000)
    errors = page.locator("#main .error:visible").all_inner_texts()  # controls.js keeps a hidden .error for validation
    assert not errors, f"{hash_} shows an error box: {errors}"


@pytest.mark.parametrize("hash_, title", [
    ("#/", "Routes"),
    ("#/route/15", "Route 15"),
    ("#/stop/15/15-S01", ""),
    ("#/analytics/15", "Analytics for route 15"),
    ("#/lab/15", ""),
    ("#/lab", ""),
    ("#/all", ""),
    ("#/settings", ""),
    ("#/map", ""),
])
def test_page_renders(app, hash_, title):
    open_hash(app, hash_)
    if title:
        expect(app.locator("#main h1").first).to_contain_text(title)


def test_trip_page_renders(app, seed_end):
    open_hash(app, f"#/trip/{trip_id(seed_end)}/15-S01")


def test_analytics_draws_charts(app):
    open_hash(app, "#/analytics/15")
    expect(app.locator("#main svg").first).to_be_visible()


def test_changing_a_filter_reloads_the_view(app):
    open_hash(app, "#/analytics/15")
    app.get_by_label("Confirmed arrivals only").uncheck()
    app.get_by_role("button", name="Apply").click()
    expect(app).to_have_url(re.compile(r"conf=0"))
    expect(app.locator("#main svg").first).to_be_visible()
    open_hash(app, "#/analytics/15?conf=0")  # the new view finished loading without an error box
