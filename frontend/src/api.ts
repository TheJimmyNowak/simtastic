// Role z firmware Meshtastic (bez przestarzałych REPEATER i ROUTER_CLIENT, które firmware zapisuje jako CLIENT).
export const ROLES = [
  "CLIENT",
  "CLIENT_MUTE",
  "CLIENT_HIDDEN",
  "CLIENT_BASE",
  "ROUTER",
  "ROUTER_LATE",
  "TRACKER",
  "SENSOR",
  "TAK",
  "TAK_TRACKER",
  "LOST_AND_FOUND",
] as const;
export type Role = (typeof ROLES)[number];
export const INFRASTRUCTURE_ROLES: readonly Role[] = ["ROUTER", "ROUTER_LATE"];

export type ScenarioStatus =
  | "draft"
  | "precomputing"
  | "links_ready"
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "error";

export type NodeConfig = {
  id: string;
  name: string;
  lat: number;
  lon: number;
  agl_m: number;
  antenna_gain_tx_db: number;
  antenna_gain_rx_db: number;
  hop_limit: number;
  role: Role;
  /** null = szum wynikający z pasma presetu. */
  noise_floor_dbm: number | null;
};

export type Environment = {
  preset: string;
  fading_sigma_db: number;
  experiment_id: string;
  random_seed: number | null;
  building_model_enabled: boolean;
  fresnel_clearance_ratio: number;
  connectivity_threshold_dbm: number;
  clutter_loss_db: number;
  link_shadowing_sigma_db: number;
  link_shadowing_seed: number | null;
  collisions_enabled: boolean;
  capture_threshold_db: number;
  [other: string]: unknown;
};

export type Scenario = {
  id: string;
  name: string;
  status: ScenarioStatus;
  environment: Environment;
  nodes: NodeConfig[];
  progress_done: number;
  progress_total: number;
  error_message?: string | null;
};

/** Fields the user edits; everything else is server state. */
export type ScenarioDraft = Pick<Scenario, "name" | "environment" | "nodes">;

export type LoraPreset = {
  name: string;
  frequency_mhz: number;
  bandwidth_khz: number;
  spreading_factor: number;
  coding_rate: number;
  tx_power_dbm: number;
  sensitivity_dbm: number;
  noise_floor_dbm: number;
};

export type NodePlacement = {
  placement: "rooftop" | "terrain";
  building_id: string | null;
  mast_height_m: number;
};

export type Links = {
  node_ids: string[];
  median_rssi_db: number[][];
  terrain_loss_db: number[][] | null;
  building_loss_db: number[][] | null;
  link_shadowing_db: number[][] | null;
  blocked_buildings_count: number[][] | null;
  distance_m: number[][];
  node_placement: Record<string, NodePlacement>;
  propagation_model: string;
};

export type PacketTx = {
  packet_id: number;
  tx_node: string;
  origin_node: string | null;
  to_node: string | null;
  is_broadcast: boolean;
  hop_limit: number | null;
  hop_start: number | null;
  portnum: string | null;
  is_rebroadcast: boolean;
  timestamp: string;
};

export type LinkAttempt = {
  packet_id: number;
  tx_node: string;
  rx_node: string;
  rssi_db: number;
  snr_db: number;
  shadow_db: number;
  building_diffraction_db: number;
  delivered: boolean;
  loss_reason: "weak" | "collision" | "half_duplex" | null;
  timestamp: string;
};

export type Packets = {
  transmissions: PacketTx[];
  recent_attempts: LinkAttempt[];
};

export type SimulationStatus = {
  active_scenario_id: string | null;
  start_in_progress: boolean;
};

async function errorMessage(response: Response): Promise<string> {
  const text = await response.text();
  try {
    const detail = JSON.parse(text).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // FastAPI validation errors: [{loc: ["body", "nodes", 0, "lat"], msg: "..."}]
      return detail
        .map((e: { loc?: unknown[]; msg?: string }) => `${(e.loc ?? []).slice(1).join(" → ")}: ${e.msg}`)
        .join("\n");
    }
  } catch {
    /* not JSON */
  }
  return text || `${response.status} ${response.statusText}`;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!response.ok) throw new Error(await errorMessage(response));
  const body = response.status === 204 ? "" : await response.text();
  return (body ? JSON.parse(body) : undefined) as T;
}

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const scenarioApi = {
  list: () => api<Scenario[]>("/api/scenarios"),
  get: (id: string) => api<Scenario>(`/api/scenarios/${id}`),
  create: (draft: ScenarioDraft) => api<Scenario>("/api/scenarios", json("POST", draft)),
  save: (id: string, draft: ScenarioDraft) => api<Scenario>(`/api/scenarios/${id}`, json("PUT", draft)),
  remove: (id: string) => api<void>(`/api/scenarios/${id}`, json("DELETE")),
  precompute: (id: string) => api<void>(`/api/scenarios/${id}/precompute-links`, json("POST")),
  links: (id: string) => api<Links>(`/api/scenarios/${id}/links`),
  start: (id: string) => api<void>(`/api/scenarios/${id}/start`, json("POST", {})),
  stop: (id: string) => api<{ containers_removed: string[] }>(`/api/scenarios/${id}/stop`, json("POST")),
  packets: (id: string, limit = 200) => api<Packets>(`/api/scenarios/${id}/packets?limit=${limit}`),
  status: (id: string) => api<SimulationStatus>(`/api/scenarios/${id}/status`),
  presets: () => api<Record<string, LoraPreset>>("/api/presets"),
  cleanupAll: () => api<{ containers_removed: string[] }>("/api/docker/cleanup-all", json("POST")),
};
