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
    tcp_host: Optional[str] = None

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("id required")
        return v.strip()
