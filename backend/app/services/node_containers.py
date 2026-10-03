"""Docker containers running meshtasticd — one container per simulated node."""

from __future__ import annotations

import logging

import docker
from docker.models.containers import Container

from app.config import settings

logger = logging.getLogger(__name__)

MESHTASTIC_PORT = 4403
# Labels identify simulator containers, so cleanup never relies on name prefixes.
LABEL_APP = "mesh-sim"
LABEL_SCENARIO = "mesh-sim.scenario"


def _docker_client() -> docker.DockerClient:
    try:
        return docker.from_env()
    except docker.errors.DockerException as exc:
        raise RuntimeError(
            "Docker niedostępny. Uruchom backend z dostępem do /var/run/docker.sock "
            "(np. przez `docker compose`)."
        ) from exc


class NodeContainers:
    """Starts, restarts and removes the meshtasticd containers of one scenario.

    Each container runs `meshtasticd -s` (simulated radio) as its main process with a
    fresh filesystem, so every run starts from a clean node state. The firmware keeps
    its configuration across `restart()` because the container layer is preserved.
    """

    def __init__(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        self._names: list[str] = []
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = _docker_client()
        return self._client

    def container_name(self, index: int) -> str:
        return f"sim-{self.scenario_id}-node-{index}"

    @staticmethod
    def hw_id(index: int) -> int:
        return settings.hw_id_offset + index

    def _run_kwargs(self, index: int) -> dict:
        kwargs: dict = {
            "image": settings.meshtastic_image,
            "entrypoint": ["meshtasticd"],
            # --hwid must be decimal: meshtasticd parses "0x.." as 0 and then every node
            # gets the same node number. With a decimal hwid the node number equals it.
            "command": ["-s", "-d", "/data", f"--hwid={self.hw_id(index)}", f"--port={MESHTASTIC_PORT}"],
            "name": self.container_name(index),
            "detach": True,
            "labels": {LABEL_APP: "1", LABEL_SCENARIO: self.scenario_id},
        }
        if settings.sim_node_access == "network":
            kwargs["network"] = settings.sim_docker_network
        else:
            # Random free host port, reachable only from this machine.
            kwargs["ports"] = {f"{MESHTASTIC_PORT}/tcp": ("127.0.0.1", None)}
        return kwargs

    def start(self, node_count: int) -> None:
        self.cleanup_scenario_containers(self.scenario_id)
        for index in range(node_count):
            self.client.containers.run(**self._run_kwargs(index))
            self._names.append(self.container_name(index))
        logger.info("Started %s meshtasticd containers for %s", node_count, self.scenario_id)

    def _container(self, index: int) -> Container:
        return self.client.containers.get(self._names[index])

    def endpoint(self, index: int) -> tuple[str, int]:
        """TCP endpoint of node `index` as seen by the backend."""
        if settings.sim_node_access == "network":
            return self._names[index], MESHTASTIC_PORT
        container = self._container(index)
        container.reload()  # the published port changes after every restart
        binding = container.ports[f"{MESHTASTIC_PORT}/tcp"][0]
        return binding["HostIp"], int(binding["HostPort"])

    def restart(self, index: int) -> None:
        """Restart the node so the committed configuration takes effect."""
        self._container(index).restart(timeout=2)

    def stop(self) -> None:
        for name in self._names:
            try:
                self.client.containers.get(name).remove(force=True)
            except docker.errors.NotFound:
                pass
        self._names.clear()

    def status(self) -> list[dict]:
        out: list[dict] = []
        for index, name in enumerate(self._names):
            try:
                state = self.client.containers.get(name).status
            except docker.errors.NotFound:
                state = "missing"
            out.append({"node_index": index, "container": name, "state": state})
        return out

    @staticmethod
    def _remove_labeled(label: str) -> list[str]:
        client = _docker_client()
        removed: list[str] = []
        for container in client.containers.list(all=True, filters={"label": label}):
            container.remove(force=True)
            removed.append(container.name)
            logger.info("Removed container %s", container.name)
        return removed

    @staticmethod
    def cleanup_scenario_containers(scenario_id: str) -> list[str]:
        return NodeContainers._remove_labeled(f"{LABEL_SCENARIO}={scenario_id}")

    @staticmethod
    def cleanup_all_sim_containers() -> list[str]:
        return NodeContainers._remove_labeled(LABEL_APP)
