from meshtastic import admin_pb2, config_pb2
from meshtastic.protobuf import localonly_pb2

from app.models.environment import LORA_PRESETS, EnvironmentConfig
from app.models.node import NodeConfig, NodeRole
from app.services import node_provisioner
from app.services.node_provisioner import provision_node
from app.services.node_containers import LABEL_SCENARIO, NodeContainers

LoRaConfig = config_pb2.Config.LoRaConfig
node_provisioner.ADMIN_GAP_S = 0.0


class FakeLocalNode:
    def __init__(self) -> None:
        self.localConfig = localonly_pb2.LocalConfig()
        self.calls: list[str] = []
        self.admin: list[admin_pb2.AdminMessage] = []

    def beginSettingsTransaction(self) -> None:
        self.calls.append("begin")

    def commitSettingsTransaction(self) -> None:
        self.calls.append("commit")

    def setFixedPosition(self, lat: float, lon: float, alt: int) -> None:
        self.calls.append(f"position {lat} {lon} {alt}")

    def _sendAdmin(self, message: admin_pb2.AdminMessage) -> None:
        self.calls.append("admin")
        self.admin.append(message)


class FakeIface:
    def __init__(self) -> None:
        self.localNode = FakeLocalNode()


def _lora_message(local: FakeLocalNode) -> LoRaConfig:
    return next(m.set_config.lora for m in local.admin if m.set_config.HasField("lora"))


def test_provisioning_uses_single_transaction_and_sets_firmware_preset() -> None:
    iface = FakeIface()
    node = NodeConfig(id="r1", lat=49.77, lon=22.77, hop_limit=5, role=NodeRole.ROUTER)
    env = EnvironmentConfig(preset="MEDIUM_FAST")
    provision_node(iface, node, 0, preset_name=env.preset, lora=env.resolve_lora(), altitude_m=312.4)

    calls = iface.localNode.calls
    assert calls[0] == "begin" and calls[-1] == "commit"
    assert "position 49.77 22.77 312" in calls
    lora = _lora_message(iface.localNode)
    assert lora.region == LoRaConfig.RegionCode.EU_868
    assert lora.use_preset and lora.modem_preset == LoRaConfig.ModemPreset.MEDIUM_FAST
    assert lora.hop_limit == 5
    device = next(m.set_config.device for m in iface.localNode.admin if m.set_config.HasField("device"))
    assert device.role == config_pb2.Config.DeviceConfig.Role.ROUTER


def test_custom_preset_is_sent_as_explicit_modem_parameters() -> None:
    iface = FakeIface()
    env = EnvironmentConfig(preset="custom", custom_spreading_factor=12, custom_bandwidth_khz=125)
    provision_node(
        iface, NodeConfig(id="c", lat=50.0, lon=20.0), 0, preset_name=env.preset, lora=env.resolve_lora()
    )
    lora = _lora_message(iface.localNode)
    assert not lora.use_preset
    assert (lora.spread_factor, lora.bandwidth) == (12, 125)


def test_node_container_runs_meshtasticd_without_erase_flag() -> None:
    kwargs = NodeContainers("scenario")._run_kwargs(3)
    assert kwargs["entrypoint"] == ["meshtasticd"]
    assert "-e" not in kwargs["command"] and "--erase" not in kwargs["command"]
    assert kwargs["command"][:3] == ["-s", "-d", "/data"] and "--hwid=19" in kwargs["command"]
    assert kwargs["labels"][LABEL_SCENARIO] == "scenario"


def test_node_access_mode_selects_network_or_published_port(monkeypatch) -> None:
    from app.services import node_containers

    monkeypatch.setattr(node_containers.settings, "sim_node_access", "network")
    kwargs = NodeContainers("s")._run_kwargs(0)
    assert kwargs["network"] == node_containers.settings.sim_docker_network and "ports" not in kwargs
    monkeypatch.setattr(node_containers.settings, "sim_node_access", "host")
    kwargs = NodeContainers("s")._run_kwargs(0)
    assert kwargs["ports"] == {"4403/tcp": ("127.0.0.1", None)} and "network" not in kwargs


def test_presets_match_meshtastic_eu868() -> None:
    assert {name: (p.spreading_factor, p.bandwidth_khz) for name, p in LORA_PRESETS.items()} == {
        "LONG_FAST": (11, 250),
        "MEDIUM_SLOW": (10, 250),
        "MEDIUM_FAST": (9, 250),
        "SHORT_FAST": (7, 250),
        "SHORT_TURBO": (7, 500),
    }
    assert all(p.frequency_mhz == 869.525 for p in LORA_PRESETS.values())
    assert LORA_PRESETS["LONG_FAST"].sensitivity_dbm == -131.5
    for name in LORA_PRESETS:
        LoRaConfig.ModemPreset.Value(name)


def test_dm_destination_uses_real_node_number() -> None:
    from types import SimpleNamespace

    from app.services.radio_gateway import RadioGateway
    from app.services.traffic_generator import TrafficGenerator

    gw = RadioGateway("s", "e", [], None, 0.0)
    gw.attach_node("a", SimpleNamespace(myInfo=SimpleNamespace(my_node_num=0x4E1C43B)), 0)
    gw.attach_node("b", SimpleNamespace(), 1)  # no handshake info -> hwid fallback 0x11
    gen = TrafficGenerator(None, gw, [], {"a": 0, "b": 1})
    assert gen._dest("a") == "!04e1c43b"
    assert gen._dest("b") == "!00000011"
