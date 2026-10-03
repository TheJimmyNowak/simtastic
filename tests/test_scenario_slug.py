from app.services.scenario_slug import slugify_scenario_id


def test_slugify_basic() -> None:
    assert slugify_scenario_id("Test Net") == "test-net"
    assert slugify_scenario_id("Scenariusz 1") == "scenariusz-1"
    assert slugify_scenario_id("Łódź mesh") == "lodz-mesh"


def test_slugify_dedupes() -> None:
    taken = {"foo", "foo-2"}
    assert slugify_scenario_id("Foo", taken) == "foo-3"
