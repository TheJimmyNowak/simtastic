from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"
    scenarios_dir: Path = Path(__file__).resolve().parents[2] / "scenarios"
    srtm_cache_dir: Path = data_dir / "srtm_cache"
    buildings_raw_dir: Path = data_dir / "buildings_raw"
    buildings_index_path: Path = data_dir / "buildings" / "buildings.sqlite"
    influx_url: str = "http://localhost:8086"
    influx_token: str = "meshtastic-dev-token"
    influx_org: str = "meshtastic"
    influx_bucket: str = "meshtastic_sim"
    meshtastic_image: str = "meshtastic/meshtasticd:latest"
    tcp_port_base: int = 4403
    hw_id_offset: int = 16
    dry_run: bool = False
    meshtastic_tcp_host: str = "127.0.0.1"
    sim_docker_network: str = "meshtastic_sim"
    # Symulacja Docker: czasy startu / TCP (sekundy)
    sim_mesh_boot_grace_s: float = 3.0
    sim_meshtasticd_start_gap_s: float = 2.0
    sim_post_provision_settle_s: float = 5.0
    sim_mesh_connect_timeout_s: float = 90.0
    sim_traffic_warmup_s: float = 5.0
    sim_meshtastic_tcp_timeout_s: float = 120.0
    sim_start_thread_join_s: float = 45.0
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "scenarios").mkdir(parents=True, exist_ok=True)
settings.scenarios_dir.mkdir(parents=True, exist_ok=True)
