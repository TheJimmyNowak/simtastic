from __future__ import annotations

import threading
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from app.config import settings
from app.models.scenario import Scenario, ScenarioCreate, ScenarioStatus
from app.services.scenario_slug import slugify_scenario_id


_RUNTIME_FIELDS = {"progress_done", "progress_total", "progress_unit"}


class ScenarioStore:
    _SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir or (settings.data_dir / "scenarios")
        self._base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: dict[str, Scenario] = {}

    def _path(self, scenario_id: str) -> Path:
        if not self._SAFE_ID.fullmatch(scenario_id):
            raise ValueError("invalid scenario id")
        return self._base / scenario_id / "scenario.yaml"

    def _load_file(self, scenario_id: str) -> Optional[Scenario]:
        path = self._path(scenario_id)
        if not path.exists():
            return None
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        scenario = Scenario.model_validate(raw)
        if scenario.id != scenario_id:
            scenario.id = scenario_id
        return scenario

    def _save_file(self, scenario: Scenario) -> None:
        path = self._path(scenario.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                scenario.model_dump(mode="json", exclude=_RUNTIME_FIELDS),
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def list_all(self) -> list[Scenario]:
        with self._lock:
            out: list[Scenario] = []
            if not self._base.exists():
                return out
            for d in sorted(self._base.iterdir()):
                if not d.is_dir():
                    continue
                s = self._cache.get(d.name) or self._load_file(d.name)
                if s:
                    self._cache[d.name] = s
                    out.append(s)
            out.sort(key=lambda x: x.created_at, reverse=True)
            return out

    def get(self, scenario_id: str) -> Optional[Scenario]:
        if not self._SAFE_ID.fullmatch(scenario_id):
            return None
        with self._lock:
            if scenario_id in self._cache:
                return self._cache[scenario_id]
            s = self._load_file(scenario_id)
            if s:
                self._cache[scenario_id] = s
            return s

    def _existing_ids(self) -> set[str]:
        if not self._base.exists():
            return set()
        return {d.name for d in self._base.iterdir() if d.is_dir()}

    def create(self, body: ScenarioCreate) -> Scenario:
        with self._lock:
            scenario_id = slugify_scenario_id(body.name, self._existing_ids())
            scenario = Scenario(
                id=scenario_id,
                name=body.name,
                environment=body.environment,
                nodes=body.nodes,
            )
            self._cache[scenario.id] = scenario
            self._save_file(scenario)
        return scenario

    def update(self, scenario: Scenario) -> Scenario:
        scenario.updated_at = datetime.now(timezone.utc)
        with self._lock:
            self._cache[scenario.id] = scenario
            self._save_file(scenario)
        return scenario

    def delete(self, scenario_id: str) -> bool:
        if not self._SAFE_ID.fullmatch(scenario_id):
            return False
        with self._lock:
            self._cache.pop(scenario_id, None)
            d = self._base / scenario_id
            if not d.exists():
                return False
            for p in sorted(d.rglob("*"), reverse=True):
                if p.is_file():
                    p.unlink()
            for p in sorted(d.rglob("*"), reverse=True):
                if p.is_dir():
                    p.rmdir()
            if d.exists():
                d.rmdir()
            return True

    def set_status(
        self,
        scenario_id: str,
        status: ScenarioStatus,
        error_message: str | None = None,
    ) -> None:
        s = self.get(scenario_id)
        if not s:
            return
        s.status = status
        s.error_message = error_message
        s.updated_at = datetime.now(timezone.utc)
        self.update(s)

    def set_progress(self, scenario_id: str, done: int, total: int, unit: str = "") -> None:
        """Postęp długiej operacji (precompute / start) — tylko w pamięci, bez zapisu YAML.

        `unit` nazywa bieżący etap (np. „zapytań NMT”); pusty — jednostkę dobiera interfejs.
        """
        s = self.get(scenario_id)
        if not s:
            return
        s.progress_done = done
        s.progress_total = total
        s.progress_unit = unit


scenario_store = ScenarioStore()
