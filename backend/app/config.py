from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = _ROOT / "data"
    scenarios_dir: Path = _ROOT / "scenarios"
    # Jedyne dane GUGiK trzymane na dysku: indeks budynków LoD1 pobranych powiatów.
    buildings_index_path: Path = data_dir / "buildings" / "buildings.sqlite"

    # Dane GUGiK pobierane na żądanie (app.services.propagation.gugik).
    gugik_nmt_url: str = "https://services.gugik.gov.pl/nmt/"
    gugik_prg_wfs_url: str = (
        "https://mapy.geoportal.gov.pl/wss/service/PZGIK/PRG/WFS/AdministrativeBoundaries"
    )
    gugik_lod1_url: str = (
        "https://opendata.geoportal.gov.pl/InneDane/Budynki3D/LOD1/{year}/{voivodeship}/{district}.zip"
    )
    gugik_lod1_year: int = 2024
    # Limit indeksu budynków: po przekroczeniu usuwane są najdawniej używane powiaty.
    buildings_index_max_mb: int = 500
    gugik_workers: int = 6
    # Odstęp próbek profilu terenu dla ITM (NMT ma siatkę 1 m; gęściej nie zmienia wyniku ITM).
    terrain_profile_step_m: float = 30.0

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
    # Rozgrzewka: NodeInfo każdego węzła w oknie max(okno, odstęp × N), potem przerwa.
    sim_nodeinfo_window_s: float = 20.0
    sim_nodeinfo_gap_s: float = 2.0
    sim_traffic_warmup_s: float = 10.0
    sim_start_thread_join_s: float = 45.0

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"


settings = Settings()
(settings.data_dir / "scenarios").mkdir(parents=True, exist_ok=True)
settings.scenarios_dir.mkdir(parents=True, exist_ok=True)
