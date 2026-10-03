from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = _ROOT / "data"
    scenarios_dir: Path = _ROOT / "scenarios"
    srtm_cache_dir: Path = data_dir / "srtm_cache"
    buildings_raw_dir: Path = data_dir / "buildings_raw"
    buildings_index_path: Path = data_dir / "buildings" / "buildings.sqlite"

    influx_url: str = "http://localhost:8086"
    influx_token: str = "meshtastic-dev-token"
    influx_org: str = "meshtastic"
    influx_bucket: str = "meshtastic_sim"

    meshtastic_image: str = "meshtastic/meshtasticd:latest"
    hw_id_offset: int = 16
    # How the backend reaches node containers: "network" — by container name on
    # `sim_docker_network` (backend in docker compose); "host" — via a port published
    # on 127.0.0.1 (backend or scripts running directly on the host).
    sim_node_access: Literal["network", "host"] = "host"
    sim_docker_network: str = "meshtastic_sim"
    sim_provision_workers: int = 4

    # Timeouts / pauses [s]
    sim_meshtastic_tcp_timeout_s: float = 120.0
    sim_mesh_connect_timeout_s: float = 90.0
    sim_traffic_warmup_s: float = 5.0
    sim_start_thread_join_s: float = 45.0

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"


settings = Settings()
(settings.data_dir / "scenarios").mkdir(parents=True, exist_ok=True)
settings.scenarios_dir.mkdir(parents=True, exist_ok=True)
