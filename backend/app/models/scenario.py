from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig

class ScenarioStatus(str, Enum):
    DRAFT = "draft"
    PRECOMPUTING = "precomputing"
    LINKS_READY = "links_ready"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"

# Statusy, w których trwa operacja w tle — scenariusza nie można wtedy edytować ani usunąć.
ACTIVE_STATUSES = frozenset(
    {
        ScenarioStatus.PRECOMPUTING,
        ScenarioStatus.STARTING,
        ScenarioStatus.RUNNING,
        ScenarioStatus.STOPPING,
    }
)


class ScenarioCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    nodes: list[NodeConfig] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name required")
        return value

    @model_validator(mode="after")
    def unique_node_ids(self) -> "ScenarioCreate":
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("node ids must be unique")
        return self

class Scenario(BaseModel):
    id: str
    name: str
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    nodes: list[NodeConfig] = Field(default_factory=list)
    status: ScenarioStatus = ScenarioStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None
    # Postęp bieżącej operacji (precompute: kierunki ITM, start: węzły); nie jest zapisywany.
    progress_done: int = 0
    progress_total: int = 0
    progress_unit: str = ""
