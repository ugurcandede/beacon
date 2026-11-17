"""The parts of setup.py that need no PostHog: layout, quoting, coverage.
`setup.py --check` runs every query against PostHog itself."""

import pytest

import setup

# main() looks the action up in PostHog; the tiles only need an id.
setup.ACTIONS["Took a download step"] = 1


def test_every_product_has_a_dashboard_filtered_to_it():
    dashboards = setup.dashboards()
    for product, title in setup.PRODUCTS.items():
        _, tiles, filters = dashboards[title]
        assert filters == [{"key": "product", "value": product, "operator": "exact", "type": "event"}]
        assert any(not isinstance(t, str) for t in tiles)


def test_product_sql_is_limited_to_the_product():
    for tile in setup.product_tiles("lumea"):
        if not isinstance(tile, str) and tile[2]["kind"] == "DataVisualizationNode":
            assert "properties.product = 'lumea'" in tile[2]["source"]["query"], tile[0]


@pytest.mark.parametrize("name", list(setup.dashboards()))
def test_layout_fits_twelve_columns_without_overlap(name):
    placed = [place for _, place in setup.tiles_with_layout(setup.dashboards()[name][1])]
    cells = set()
    for p in placed:
        assert 0 <= p["x"] and p["x"] + p["w"] <= 12
        for x in range(p["x"], p["x"] + p["w"]):
            for y in range(p["y"], p["y"] + p["h"]):
                assert (x, y) not in cells
                cells.add((x, y))


@pytest.mark.parametrize("name", list(setup.dashboards()))
def test_tile_names_are_unique_per_dashboard(name):
    names = [t[0] for t in setup.dashboards()[name][1] if not isinstance(t, str)]
    assert len(names) == len(set(names))


def test_turkish_column_names_are_quoted_and_strings_left_alone():
    q = setup.sql("select properties.$os as işletim, 'şu' as x from events group by işletim")["source"]["query"]
    assert q == "select properties.$os as `işletim`, 'şu' as x from events group by `işletim`"


def test_every_alert_points_at_a_trends_tile_that_exists():
    dashboards = setup.dashboards()
    for dashboard, tile, name, bounds, interval, enabled in setup.alerts():
        tiles = {t[0]: t[2] for t in dashboards[dashboard][1] if not isinstance(t, str)}
        assert tile in tiles, (dashboard, tile)
        assert tiles[tile]["source"]["kind"] == "TrendsQuery", name  # alerts run on trends
        assert set(bounds) <= {"upper", "lower"} and interval in ("hourly", "daily", "weekly")


def test_product_alerts_count_only_their_product():
    for dashboard, tile, name, *_ in setup.alerts():
        if dashboard in setup.PRODUCTS.values():
            product = next(k for k, v in setup.PRODUCTS.items() if v == dashboard)
            query = next(t[2] for t in setup.dashboards()[dashboard][1] if not isinstance(t, str) and t[0] == tile)
            for series in query["source"]["series"]:
                assert {"key": "product", "value": product, "operator": "exact", "type": "event"} in series["properties"]


def test_alerts_fit_the_plan():
    assert len(setup.alerts()) <= setup.ALERT_LIMIT
    assert len({a[2] for a in setup.alerts()}) == len(setup.alerts())


def test_every_sql_tile_leaves_test_products_out():
    for name, (_, tiles, _) in setup.dashboards().items():
        for tile in tiles:
            if isinstance(tile, str) or tile[2]["kind"] != "DataVisualizationNode":
                continue
            query = tile[2]["source"]["query"]
            assert setup.leaves_test_products_out(query), (name, tile[0])


# ---- what a run may delete, and in what order -------------------------------------------


def tile(tid, name=None, text=None):
    return {"id": tid, "insight": {"id": tid * 10, "name": name} if name else None, "text": {"body": text} if text else None}


def test_a_rebuild_only_removes_what_the_script_made():
    current = [tile(1, "Aktif kullanıcı"), tile(2, "Benim elle eklediğim grafik"), tile(3, text="## Başlık"),
               tile(4, "Eski adıyla bir kutu")]
    mine = setup.owned(current, titles={"Aktif kullanıcı"}, retired={"Eski adıyla bir kutu"})
    assert [t["id"] for t in mine] == [1, 3, 4]  # the hand-made insight stays


class FakeApi:
    """Answers what build() asks for and records the calls; fails on the nth insight if told to."""

    def __init__(self, fail_on_insight=None):
        self.calls, self.made, self.fail_on = [], 0, fail_on_insight
        self.tiles = [tile(1, "Eski kutu"), tile(2, "Elle eklenen")]

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST" and path == "/insights/":
            self.made += 1
            if self.made == self.fail_on:
                raise SystemExit("POST /insights/: 500")
            self.tiles.append(tile(100 + self.made, body["name"]))
            return {"id": (100 + self.made) * 10, "dashboard_tiles": [{"id": 100 + self.made, "dashboard_id": 7}]}
        if method == "PATCH" and body and "tiles" in body and "text" in body["tiles"][0]:
            self.tiles.append(tile(200 + len(self.tiles), text=body["tiles"][0]["text"]["body"]))
        return {"id": 7, "tiles": list(self.tiles)}


def deletions(api):
    return [c for c in api.calls if c[0] == "PATCH" and c[2] and (c[2].get("deleted") or any(t.get("deleted") for t in c[2].get("tiles", [])))]


def test_a_rebuild_makes_the_new_tiles_before_it_removes_the_old(monkeypatch):
    api = FakeApi()
    monkeypatch.setattr(setup, "call", api)
    monkeypatch.setattr(setup, "RETIRED", {"Eski kutu"})
    tiles = [("Yeni kutu", "", {"kind": "x"}, 6, 5), "## Başlık", ("İkinci", "", {"kind": "x"}, 6, 5)]
    setup.build("Pano", "", tiles, None, {"Pano": {"id": 7}})
    first_delete = api.calls.index(deletions(api)[0])
    last_create = max(i for i, c in enumerate(api.calls) if c[0] == "POST" and c[1] == "/insights/")
    assert last_create < first_delete
    deleted_insights = [c[1] for c in api.calls if c[2] == {"deleted": True}]
    assert deleted_insights == ["/insights/10/"]  # the retired one; "Elle eklenen" (20) is untouched


def test_a_rebuild_that_fails_half_way_has_removed_nothing(monkeypatch):
    api = FakeApi(fail_on_insight=2)
    monkeypatch.setattr(setup, "call", api)
    monkeypatch.setattr(setup, "RETIRED", {"Eski kutu"})
    tiles = [("Yeni kutu", "", {"kind": "x"}, 6, 5), ("İkinci", "", {"kind": "x"}, 6, 5)]
    with pytest.raises(SystemExit):
        setup.build("Pano", "", tiles, None, {"Pano": {"id": 7}})
    assert deletions(api) == []  # the dashboard still shows what it showed


# ---- alerts -----------------------------------------------------------------------------


def test_a_silent_alert_is_on_only_for_a_product_that_ships_on_beacon(monkeypatch):
    silent = lambda: {a[2].split(" sessiz")[0]: a[5] for a in setup.alerts() if "sessiz" in a[2]}
    monkeypatch.setattr(setup, "LIVE", set())
    assert set(silent().values()) == {False}
    monkeypatch.setattr(setup, "LIVE", {"sotto"})
    assert silent() == {setup.PRODUCTS[p]: p == "sotto" for p in setup.PRODUCTS}


# ---- the test-product filter ------------------------------------------------------------


@pytest.mark.parametrize("query, ok", [
    (f"select 1 from events where {setup.REAL_PRODUCT} and x", True),
    ("select 1 from events group by event", False),  # no WHERE at all: test products would be counted
    (f"select 1 from events where {setup.REAL_PRODUCT} union all select 1 from events where y", False),
])
def test_the_filter_check_sees_a_query_without_the_filter(query, ok):
    assert setup.leaves_test_products_out(query) is ok
