import secrets
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator

class NodeRole(str, Enum):
    """Config.DeviceConfig.Role values that meshtasticd keeps after a reboot.

    REPEATER and ROUTER_CLIENT are left out: they are deprecated and firmware 2.7.15
    silently stores them as CLIENT, which would fail verify_provisioning.
    """
    CLIENT = "CLIENT"
    CLIENT_MUTE = "CLIENT_MUTE"
    CLIENT_HIDDEN = "CLIENT_HIDDEN"
    CLIENT_BASE = "CLIENT_BASE"
    ROUTER = "ROUTER"
    ROUTER_LATE = "ROUTER_LATE"
    TRACKER = "TRACKER"
    SENSOR = "SENSOR"
    TAK = "TAK"
    TAK_TRACKER = "TAK_TRACKER"
    LOST_AND_FOUND = "LOST_AND_FOUND"

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
    # None = szum wynikający z pasma presetu (LoraPreset.noise_floor_dbm).
    noise_floor_dbm: Optional[float] = None
    mac_address: str = Field(default_factory=_default_mac)

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id required")
        return v.strip()
