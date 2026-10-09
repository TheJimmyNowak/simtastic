from __future__ import annotations

import logging
import time

from meshtastic import admin_pb2, config_pb2
from meshtastic.tcp_interface import TCPInterface

from app.models.environment import LORA_PRESETS, LoraPreset
from app.models.node import NodeConfig

logger = logging.getLogger(__name__)

LoRaConfig = config_pb2.Config.LoRaConfig
Role = config_pb2.Config.DeviceConfig.Role

# meshtasticd ma małą kolejkę pakietów lokalnych: seria komunikatów admin wysłana
# bez przerw przepełnia ją i firmware po cichu odrzuca najstarsze („drop oldest”).
ADMIN_GAP_S = 0.5


def _apply_lora(lora_cfg: LoRaConfig, node: NodeConfig, preset_name: str, lora: LoraPreset) -> None:
    lora_cfg.region = LoRaConfig.RegionCode.EU_868
    lora_cfg.hop_limit = int(node.hop_limit)
    lora_cfg.tx_enabled = True
    lora_cfg.tx_power = int(lora.tx_power_dbm)
    if preset_name in LORA_PRESETS:
        lora_cfg.use_preset = True
        lora_cfg.modem_preset = LoRaConfig.ModemPreset.Value(preset_name)
        lora_cfg.override_frequency = 0.0
    else:
        lora_cfg.use_preset = False
        lora_cfg.bandwidth = int(lora.bandwidth_khz)
        lora_cfg.spread_factor = int(lora.spreading_factor)
        lora_cfg.coding_rate = int(lora.coding_rate)
        lora_cfg.override_frequency = float(lora.frequency_mhz)


def provision_node(
    iface: TCPInterface,
    node: NodeConfig,
    index: int,
    *,
    preset_name: str,
    lora: LoraPreset,
    altitude_m: float = 0.0,
) -> None:
    """Configure meshtasticd in one settings transaction (single reboot on commit).

    Sets owner, disables UDP, LoRa region/preset/hop_limit (the firmware must use the
    same modem parameters as the RF model, otherwise airtime differs), role and a fixed
    position. `altitude_m` is the antenna height above sea level.
    """
    local = iface.localNode
    if local is None:
        raise RuntimeError("No local node on interface")

    local.beginSettingsTransaction()
    time.sleep(ADMIN_GAP_S)

    p = admin_pb2.AdminMessage()
    p.set_owner.long_name = node.name or node.id
    p.set_owner.short_name = (node.name or node.id)[:4]
    local._sendAdmin(p)
    time.sleep(ADMIN_GAP_S)

    network_cfg = local.localConfig.network
    network_cfg.enabled_protocols = 0
    p = admin_pb2.AdminMessage()
    p.set_config.network.CopyFrom(network_cfg)
    local._sendAdmin(p)
    time.sleep(ADMIN_GAP_S)

    lora_cfg = local.localConfig.lora
    _apply_lora(lora_cfg, node, preset_name, lora)
    p = admin_pb2.AdminMessage()
    p.set_config.lora.CopyFrom(lora_cfg)
    local._sendAdmin(p)
    time.sleep(ADMIN_GAP_S)

    device_cfg = local.localConfig.device
    device_cfg.role = Role.Value(node.role.value)
    device_cfg.node_info_broadcast_secs = 90
    p = admin_pb2.AdminMessage()
    p.set_config.device.CopyFrom(device_cfg)
    local._sendAdmin(p)
    time.sleep(ADMIN_GAP_S)

    pos_cfg = local.localConfig.position
    pos_cfg.position_broadcast_secs = 90
    p = admin_pb2.AdminMessage()
    p.set_config.position.CopyFrom(pos_cfg)
    local._sendAdmin(p)
    time.sleep(ADMIN_GAP_S)

    # set_fixed_position zapisuje pozycję w firmware i włącza fixed_position
    # (sendPosition tylko nadawał jednorazowy pakiet, bez zapisu).
    local.setFixedPosition(float(node.lat), float(node.lon), int(round(altitude_m)))
    time.sleep(ADMIN_GAP_S)

    local.commitSettingsTransaction()
    time.sleep(ADMIN_GAP_S)
    logger.info("Provisioned node %s (index %s)", node.id, index)


def verify_provisioning(iface: TCPInterface, node: NodeConfig, preset_name: str) -> None:
    """Fail loudly when the firmware did not keep the provisioned configuration."""
    cfg = iface.localNode.localConfig
    expected = {
        "region": (cfg.lora.region, LoRaConfig.RegionCode.EU_868),
        "hop_limit": (cfg.lora.hop_limit, int(node.hop_limit)),
        "role": (cfg.device.role, Role.Value(node.role.value)),
    }
    if preset_name in LORA_PRESETS:
        expected["modem_preset"] = (cfg.lora.modem_preset, LoRaConfig.ModemPreset.Value(preset_name))
    wrong = {key: values for key, values in expected.items() if values[0] != values[1]}
    if wrong:
        details = ", ".join(f"{k}={got} (expected {want})" for k, (got, want) in wrong.items())
        raise RuntimeError(f"Node {node.id} lost provisioned config: {details}")
