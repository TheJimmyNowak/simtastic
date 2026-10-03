from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import docker
from docker.models.containers import Container

from app.config import settings
from app.models.node import NodeConfig

logger = logging.getLogger(__name__)

MESHTASTICD = "meshtasticd"


def _docker_client() -> docker.DockerClient:
    try:
        return docker.from_env()
    except docker.errors.DockerException as exc:
        raise RuntimeError(
            "Docker niedostępny. Uruchom backend na hoście z zainstalowanym dockerem "
            "lub przez `docker compose` (wymagany /var/run/docker.sock)."
        ) from exc


@dataclass
class ShardInfo:
    shard_id: int
    container_name: str
    node_indices: list[int] = field(default_factory=list)


class ShardManager:
    def __init__(self, scenario_id: str, shard_size: int) -> None:
        self.scenario_id = scenario_id
        self.shard_size = max(1, shard_size)
        self.shards: list[ShardInfo] = []

    def _container_name(self, shard_id: int) -> str:
        return f"sim-{self.scenario_id[:8]}-shard-{shard_id}"

    def tcp_endpoint(self, node_index: int) -> tuple[str, int]:
        """Host/port for Meshtastic TCP from the backend container."""
        shard_id = node_index // self.shard_size
        if shard_id < len(self.shards):
            host = self.shards[shard_id].container_name
        else:
            host = settings.meshtastic_tcp_host
        port = settings.tcp_port_base + node_index
        return host, port

    @staticmethod
    def _remove_container(client: docker.DockerClient, name: str) -> None:
        try:
            c = client.containers.get(name)
            c.remove(force=True)
        except Exception:
            pass

    @staticmethod
    def _port_open_in_container(container: Container, port: int) -> bool:
        result = container.exec_run(
            [
                "sh",
                "-c",
                f"timeout 1 bash -c '</dev/tcp/127.0.0.1/{port}' 2>/dev/null",
            ],
            detach=False,
        )
        return result.exit_code == 0

    def _wait_port_in_container(
        self, container: Container, port: int, timeout: float, *, want_open: bool
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            open_now = self._port_open_in_container(container, port)
            if open_now == want_open:
                return
            time.sleep(0.5)
        state = "open" if want_open else "free"
        raise TimeoutError(f"port {port} not {state} in {container.name} after {timeout:.0f}s")

    @staticmethod
    def _pid_file(node_index: int) -> str:
        return f"/tmp/meshtasticd_{node_index}.pid"

    def _meshtasticd_cmd(self, node_index: int, hw_id: int, port: int) -> str:
        # Bez `-e/--erase`: ta flaga kasuje VFS przy KAŻDYM starcie, więc restart po
        # provisioningu przywracał konfigurację domyślną (hop_limit, rola, preset).
        # Czysty stan zapewnia świeży kontener tworzony przy każdym uruchomieniu.
        data_dir = f"/data/node_{node_index}"
        return (
            f"mkdir -p {data_dir} && "
            f"nohup {MESHTASTICD} -s -d {data_dir} "
            f"--hwid=0x{hw_id:x} --port={port} >>/tmp/out_{node_index}.log 2>&1 & "
            f"echo $! > {self._pid_file(node_index)}"
        )

    def start(self, nodes: list[NodeConfig]) -> None:
        if settings.dry_run:
            logger.info("dry_run: skip Docker shard start")
            return
        client = _docker_client()
        num_shards = (len(nodes) + self.shard_size - 1) // self.shard_size
        network = settings.sim_docker_network
        for shard_id in range(num_shards):
            start = shard_id * self.shard_size
            chunk = nodes[start : start + self.shard_size]
            indices = list(range(start, start + len(chunk)))
            info = ShardInfo(
                shard_id=shard_id,
                container_name=self._container_name(shard_id),
                node_indices=indices,
            )
            self.shards.append(info)
            self._start_shard(client, info, chunk, start, network)

    def _start_shard(
        self,
        client: docker.DockerClient,
        info: ShardInfo,
        chunk: list[NodeConfig],
        base_index: int,
        network: str,
    ) -> None:
        port_map: dict[str, int] = {}
        for i, node in enumerate(chunk):
            global_i = base_index + i
            port = settings.tcp_port_base + global_i
            node.tcp_port = port
            node.tcp_host = info.container_name
            port_map[f"{port}/tcp"] = port

        self._remove_container(client, info.container_name)

        run_kwargs: dict = {
            "image": settings.meshtastic_image,
            "command": ["sh", "-c", "while true; do sleep 3600; done"],
            "name": info.container_name,
            "detach": True,
            "ports": port_map,
        }
        try:
            client.containers.run(**run_kwargs, network=network)
        except docker.errors.APIError as exc:
            logger.warning("Network %s unavailable (%s), using default bridge", network, exc)
            self._remove_container(client, info.container_name)
            client.containers.run(**run_kwargs)

        time.sleep(1.0)
        container = client.containers.get(info.container_name)
        gap = settings.sim_meshtasticd_start_gap_s
        for i, node in enumerate(chunk):
            global_i = base_index + i
            port = settings.tcp_port_base + global_i
            hw_id = settings.hw_id_offset + global_i
            container.exec_run(["sh", "-c", self._meshtasticd_cmd(global_i, hw_id, port)], detach=True)
            logger.info("Started meshtasticd %s in %s on port %s", node.id, info.container_name, port)
            self._wait_port_in_container(container, port, timeout=90.0, want_open=True)
            if i + 1 < len(chunk):
                time.sleep(gap)

    def restart_meshtasticd(self, node_index: int) -> None:
        """Restart meshtasticd after admin provision (firmware reboot often kills the process)."""
        if settings.dry_run or not self.shards:
            return
        shard_id = node_index // self.shard_size
        if shard_id >= len(self.shards):
            return
        info = self.shards[shard_id]
        client = _docker_client()
        container = client.containers.get(info.container_name)
        port = settings.tcp_port_base + node_index
        hw_id = settings.hw_id_offset + node_index
        pid_file = self._pid_file(node_index)
        container.exec_run(
            ["sh", "-c", f"[ -f {pid_file} ] && kill $(cat {pid_file}) 2>/dev/null; true"],
            detach=False,
        )
        self._wait_port_in_container(container, port, timeout=30.0, want_open=False)
        time.sleep(0.5)
        container.exec_run(
            ["sh", "-c", self._meshtasticd_cmd(node_index, hw_id, port)],
            detach=True,
        )
        self._wait_port_in_container(container, port, timeout=90.0, want_open=True)
        logger.info("Restarted meshtasticd index %s on port %s", node_index, port)

    def stop(self) -> None:
        if settings.dry_run:
            return
        client = _docker_client()
        for info in self.shards:
            self._remove_container(client, info.container_name)
        self.shards.clear()

    @staticmethod
    def cleanup_scenario_containers(scenario_id: str) -> list[str]:
        if settings.dry_run:
            return []
        client = _docker_client()
        prefix = f"sim-{scenario_id[:8]}-shard-"
        removed: list[str] = []
        for c in client.containers.list(all=True):
            if c.name.startswith(prefix):
                c.remove(force=True)
                removed.append(c.name)
                logger.info("Removed container %s", c.name)
        return removed

    @staticmethod
    def cleanup_all_sim_containers() -> list[str]:
        if settings.dry_run:
            return []
        client = _docker_client()
        removed: list[str] = []
        for c in client.containers.list(all=True):
            if c.name.startswith("sim-"):
                c.remove(force=True)
                removed.append(c.name)
                logger.info("Removed container %s", c.name)
        return removed

    def status(self) -> list[dict]:
        client = _docker_client()
        out: list[dict] = []
        for info in self.shards:
            try:
                c: Container = client.containers.get(info.container_name)
                state = c.status
            except Exception:
                state = "missing"
            out.append(
                {
                    "shard_id": info.shard_id,
                    "container": info.container_name,
                    "state": state,
                    "nodes": info.node_indices,
                }
            )
        return out
