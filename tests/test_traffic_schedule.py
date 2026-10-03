from pathlib import Path

from app.models.node import NodeConfig
from app.services.traffic_schedule import write_default_traffic_yaml
from app.services.traffic_generator import TrafficGenerator
import yaml


def test_write_default_traffic_yaml_uses_node_ids(tmp_path: Path) -> None:
    nodes = [
        NodeConfig(id="a", lat=52.0, lon=21.0),
        NodeConfig(id="b", lat=52.05, lon=21.1),
        NodeConfig(id="node2", lat=52.1, lon=21.2),
    ]
    path = write_default_traffic_yaml(tmp_path / "traffic.yaml", nodes)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    from_ids = {item["from"] for item in raw["schedule"]}
    assert from_ids <= {"a", "b", "node2"}


def test_schedule_rejects_unknown_destination(tmp_path: Path) -> None:
    path = tmp_path / "traffic.yaml"
    path.write_text(
        yaml.safe_dump(
            {"schedule": [{"delay_s": 1, "action": "dm", "from": "a", "to": "missing"}]}
        ),
        encoding="utf-8",
    )
    nodes = [NodeConfig(id="a", lat=52.0, lon=21.0)]
    generator = TrafficGenerator(path, object(), nodes, {"a": 0})
    import pytest

    with pytest.raises(ValueError, match="unknown destination"):
        generator.start()
