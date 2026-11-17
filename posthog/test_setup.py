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
            assert query.count("from events where") == query.count(setup.REAL_PRODUCT), (name, tile[0])
