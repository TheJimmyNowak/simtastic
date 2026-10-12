from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field

class PacketOtaTx(BaseModel):
    scenario_id: str
    experiment_id: str
    packet_id: int
    tx_node: str
    origin_node: Optional[str] = None
    to_node: Optional[str] = None
    is_broadcast: bool = False
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None
    portnum: Optional[str] = None
    payload_len: int = 0
    is_rebroadcast: bool = False
    # Czas od odebrania zgłoszenia z TCP do jego obsługi (kolejka wątku biblioteki) [ms].
    report_delay_ms: Optional[float] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class LinkAttempt(BaseModel):
    scenario_id: str
    experiment_id: str
    packet_id: int
    tx_node: str
    rx_node: str
    # Pierwotny nadawca: węzły meshtasticd potrafią wylosować to samo id pakietu, więc
    # pakiet identyfikuje dopiero para (origin_node, packet_id) — jak w firmware.
    origin_node: Optional[str] = None
    path_loss_median_db: float
    terrain_loss_db: float = 0.0
    building_diffraction_db: float = 0.0
    blocked_buildings_count: int = 0
    fresnel_clearance_m: Optional[float] = None
    dominant_building_id: Optional[str] = None
    building_data_year: Optional[str] = None
    propagation_model: str = "itm"
    shadow_db: float
    rssi_db: float
    snr_db: float
    delivered: bool
    # None = dostarczony; weak / collision / half_duplex = przyczyna utraty.
    loss_reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class PacketOtaRx(BaseModel):
    scenario_id: str
    experiment_id: str
    packet_id: int
    rx_node: str
    rssi_db: float
    snr_db: float
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
