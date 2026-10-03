from __future__ import annotations

from pathlib import Path

import yaml

from app.models.node import NodeConfig


def write_default_traffic_yaml(path: Path, nodes: list[NodeConfig]) -> Path:
    """Write a minimal traffic schedule using actual scenario node ids."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(nodes) < 1:
        schedule: list[dict] = []
    else:
        n0 = nodes[0].id
        n1 = nodes[1].id if len(nodes) > 1 else n0
        n2 = nodes[2].id if len(nodes) > 2 else n1
        schedule = [
            {"delay_s": 15, "action": "broadcast", "from": n0, "text": "scalability probe 1", "want_ack": False},
            {"delay_s": 30, "action": "broadcast", "from": n1, "text": "scalability probe 2", "want_ack": False},
            {"delay_s": 45, "action": "traceroute", "from": n0, "to": n2},
        ]
    path.write_text(
        yaml.safe_dump({"schedule": schedule}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def resolve_traffic_path(scenario_id: str, data_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        from app.config import settings

        candidate = explicit.resolve()
        allowed_roots = [
            (data_dir / "scenarios").resolve(),
            settings.scenarios_dir.resolve(),
        ]
        if not any(candidate.is_relative_to(root) for root in allowed_roots):
            raise ValueError("traffic_config must be inside a configured scenarios directory")
        if not candidate.is_file():
            raise ValueError(f"traffic_config not found: {explicit}")
        if candidate.suffix.lower() not in {".yaml", ".yml"}:
            raise ValueError("traffic_config must be a YAML file")
        return candidate
    per_scenario = data_dir / "scenarios" / scenario_id / "traffic.yaml"
    if per_scenario.exists():
        return per_scenario
    return None
