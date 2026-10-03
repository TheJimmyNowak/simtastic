const BASE = "";

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!r.ok) throw new Error(await r.text());
  if (r.status === 204) return undefined as T;
  const body = await r.text();
  return body ? JSON.parse(body) as T : undefined as T;
}

export type NodeConfig = {
  id: string; name: string; lat: number; lon: number; agl_m: number;
  antenna_gain_tx_db: number; antenna_gain_rx_db: number;
  hop_limit: number; role: string; noise_floor_dbm: number; mac_address: string;
};

export type Scenario = {
  id: string; name: string; status: string;
  environment: {
    preset: string; fading_sigma_db: number; shard_size: number; experiment_id: string;
    random_seed?: number | null; building_model_enabled?: boolean; fresnel_clearance_ratio?: number;
  };
  nodes: NodeConfig[];
  itm_progress: number; itm_total: number; error_message?: string;
};
