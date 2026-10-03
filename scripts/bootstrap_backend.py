#!/usr/bin/env python3
"""Generate backend package files (UTF-8)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend"

FILES: dict[str, str] = {}

def w(path: str, content: str) -> None:
    FILES[path] = content

w("app/__init__.py", "")
w("app/config.py", '''
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    scenarios_dir: Path = Path(__file__).resolve().parents[2] / "scenarios"
    srtm_cache_dir: Path = data_dir / "srtm_cache"
    influx_url: str = "http://localhost:8086"
    influx_token: str = "meshtastic-dev-token"
    influx_org: str = "meshtastic"
    influx_bucket: str = "meshtastic_sim"
    meshtastic_image: str = "meshtastic/meshtasticd:latest"
    tcp_port_base: int = 4403
    hw_id_offset: int = 16
    dry_run: bool = False
    meshtastic_tcp_host: str = "127.0.0.1"
    sim_mesh_boot_grace_s: float = 8.0
    sim_post_provision_settle_s: float = 2.0
    sim_meshtastic_tcp_timeout_s: float = 120.0
    sim_start_thread_join_s: float = 45.0

settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "scenarios").mkdir(parents=True, exist_ok=True)
settings.scenarios_dir.mkdir(parents=True, exist_ok=True)
''')

w("app/models/environment.py", '''
from typing import Optional
from pydantic import BaseModel, Field

class LoraPreset(BaseModel):
    name: str
    frequency_mhz: float
    bandwidth_khz: float
    spreading_factor: int
    coding_rate: int
    tx_power_dbm: float
    sensitivity_dbm: float

LORA_PRESETS = {
    "LONG_FAST": LoraPreset(name="LONG_FAST", frequency_mhz=915.0, bandwidth_khz=250,
        spreading_factor=11, coding_rate=5, tx_power_dbm=27, sensitivity_dbm=-134),
    "MEDIUM_FAST": LoraPreset(name="MEDIUM_FAST", frequency_mhz=915.0, bandwidth_khz=250,
        spreading_factor=10, coding_rate=5, tx_power_dbm=27, sensitivity_dbm=-131),
    "SHORT_FAST": LoraPreset(name="SHORT_FAST", frequency_mhz=915.0, bandwidth_khz=250,
        spreading_factor=7, coding_rate=5, tx_power_dbm=27, sensitivity_dbm=-123),
    "SHORT_TURBO": LoraPreset(name="SHORT_TURBO", frequency_mhz=915.0, bandwidth_khz=500,
        spreading_factor=7, coding_rate=5, tx_power_dbm=27, sensitivity_dbm=-120),
}

class EnvironmentConfig(BaseModel):
    preset: str = "LONG_FAST"
    custom_frequency_mhz: Optional[float] = None
    custom_bandwidth_khz: Optional[float] = None
    custom_spreading_factor: Optional[int] = None
    custom_coding_rate: Optional[int] = None
    custom_tx_power_dbm: Optional[float] = None
    custom_sensitivity_dbm: Optional[float] = None
    fading_sigma_db: float = Field(default=8.0, ge=0)
    itm_climate: int = Field(default=5, ge=1, le=7)
    itm_polarization: int = Field(default=0, ge=0, le=1)
    shard_size: int = Field(default=10, ge=1, le=50)
    connectivity_threshold_dbm: float = -140.0
    experiment_id: str = "default"

    def resolve_lora(self) -> LoraPreset:
        if self.preset == "custom":
            return LoraPreset(name="custom",
                frequency_mhz=self.custom_frequency_mhz or 915.0,
                bandwidth_khz=self.custom_bandwidth_khz or 250,
                spreading_factor=self.custom_spreading_factor or 10,
                coding_rate=self.custom_coding_rate or 5,
                tx_power_dbm=self.custom_tx_power_dbm or 27,
                sensitivity_dbm=self.custom_sensitivity_dbm or -131)
        base = LORA_PRESETS[self.preset]
        return LoraPreset(name=base.name, frequency_mhz=self.custom_frequency_mhz or base.frequency_mhz,
            bandwidth_khz=self.custom_bandwidth_khz or base.bandwidth_khz,
            spreading_factor=self.custom_spreading_factor or base.spreading_factor,
            coding_rate=self.custom_coding_rate or base.coding_rate,
            tx_power_dbm=self.custom_tx_power_dbm or base.tx_power_dbm,
            sensitivity_dbm=self.custom_sensitivity_dbm or base.sensitivity_dbm)
''')

w("app/models/node.py", '''
import secrets
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator

class NodeRole(str, Enum):
    CLIENT = "CLIENT"
    ROUTER = "ROUTER"
    REPEATER = "REPEATER"
    CLIENT_MUTE = "CLIENT_MUTE"

def _default_mac() -> str:
    b = secrets.token_bytes(3)
    return f"AA:BB:CC:{b[0]:02X}:{b[1]:02X}:{b[2]:02X}"

class NodeConfig(BaseModel):
    id: str
    name: str = ""
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    agl_m: float = Field(default=2.0, ge=0)
    antenna_gain_tx_db: float = 2.0
    antenna_gain_rx_db: float = 2.0
    hop_limit: int = Field(default=3, ge=0, le=7)
    role: NodeRole = NodeRole.CLIENT
    noise_floor_dbm: float = -120.0
    mac_address: str = Field(default_factory=_default_mac)
    tcp_port: Optional[int] = None

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id required")
        return v.strip()
''')

w("app/models/scenario.py", '''
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4
from pydantic import BaseModel, Field
from app.models.environment import EnvironmentConfig
from app.models.node import NodeConfig

class ScenarioStatus(str, Enum):
    DRAFT = "draft"
    LINKS_READY = "links_ready"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"

class ScenarioCreate(BaseModel):
    name: str
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    nodes: list[NodeConfig] = Field(default_factory=list)

class Scenario(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    nodes: list[NodeConfig] = Field(default_factory=list)
    status: ScenarioStatus = ScenarioStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None
    itm_progress: float = 0.0
    itm_total: int = 0
''')

w("app/models/packet_trace.py", '''
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field

class PacketOtaTx(BaseModel):
    scenario_id: str
    experiment_id: str
    packet_id: int
    tx_node: str
    to_node: Optional[str] = None
    is_broadcast: bool = False
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None
    portnum: Optional[str] = None
    payload_len: int = 0
    is_rebroadcast: bool = False
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class LinkAttempt(BaseModel):
    scenario_id: str
    experiment_id: str
    packet_id: int
    tx_node: str
    rx_node: str
    path_loss_median_db: float
    shadow_db: float
    rssi_db: float
    snr_db: float
    delivered: bool
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PacketOtaRx(BaseModel):
    scenario_id: str
    experiment_id: str
    packet_id: int
    rx_node: str
    rssi_db: float
    snr_db: float
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
''')

w("app/models/__init__.py", '''
from app.models.environment import EnvironmentConfig, LoraPreset, LORA_PRESETS
from app.models.node import NodeConfig, NodeRole
from app.models.scenario import Scenario, ScenarioCreate, ScenarioStatus
from app.models.packet_trace import PacketOtaTx, LinkAttempt, PacketOtaRx
''')

# Continue in part 2 - services
print(f"Will write {len(FILES)} files part1")

for rel, content in FILES.items():
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    print("wrote", rel)
