import type { NodeConfig, Scenario, ScenarioDraft, ScenarioStatus } from "./api";

export const STATUS_LABELS: Record<ScenarioStatus, string> = {
  draft: "Wersja robocza",
  precomputing: "Obliczanie łączy",
  links_ready: "Łącza gotowe",
  starting: "Uruchamianie",
  running: "Symulacja działa",
  stopping: "Zatrzymywanie",
  stopped: "Zatrzymano",
  error: "Błąd",
};

/** Background operation in progress — the scenario cannot be edited. */
export const BUSY_STATUSES: ScenarioStatus[] = ["precomputing", "starting", "running", "stopping"];

export const isBusy = (status: ScenarioStatus) => BUSY_STATUSES.includes(status);

export const draftOf = (s: Scenario): ScenarioDraft => ({
  name: s.name,
  environment: s.environment,
  nodes: s.nodes,
});

/** Problems that would make the backend reject the scenario, in user terms. */
export function draftErrors(draft: ScenarioDraft): string[] {
  const errors: string[] = [];
  if (!draft.name.trim()) errors.push("Podaj nazwę scenariusza.");
  if (!draft.environment.experiment_id.trim()) errors.push("Podaj identyfikator eksperymentu.");
  const ids = draft.nodes.map((n) => n.id);
  if (new Set(ids).size !== ids.length) errors.push("Identyfikatory węzłów muszą być unikalne.");
  return errors;
}

export const sameDraft = (a: ScenarioDraft, b: ScenarioDraft) => JSON.stringify(a) === JSON.stringify(b);

export function newNode(existing: NodeConfig[], near?: { lat: number; lon: number }): NodeConfig {
  let index = existing.length;
  while (existing.some((node) => node.id === `node${index}`)) index += 1;
  const last = existing[existing.length - 1];
  const anchor = near ?? (last ? { lat: last.lat + 0.004, lon: last.lon + 0.006 } : { lat: 52.0, lon: 21.0 });
  return {
    id: `node${index}`,
    name: `Węzeł ${index + 1}`,
    lat: anchor.lat,
    lon: anchor.lon,
    agl_m: 3,
    antenna_gain_tx_db: 2,
    antenna_gain_rx_db: 2,
    hop_limit: 3,
    role: "CLIENT",
    noise_floor_dbm: null,
  };
}

export function defaultDraft(name: string): ScenarioDraft {
  const nodes: NodeConfig[] = [];
  for (const position of [
    { lat: 52.0, lon: 21.0 },
    { lat: 52.03, lon: 21.05 },
    { lat: 52.06, lon: 21.1 },
  ]) {
    nodes.push(newNode(nodes, position));
  }
  return {
    name,
    environment: {
      preset: "LONG_FAST",
      fading_sigma_db: 8,
      experiment_id: "exp1",
      random_seed: null,
      building_model_enabled: true,
      fresnel_clearance_ratio: 0.6,
      connectivity_threshold_dbm: -140,
      clutter_loss_db: 0,
      link_shadowing_sigma_db: 0,
      link_shadowing_seed: null,
    },
    nodes,
  };
}

export type SignalLevel = "strong" | "medium" | "weak" | "none";

/** Signal quality relative to the receiver sensitivity (margin in dB). */
export function signalLevel(rssi: number, sensitivity: number): SignalLevel {
  const margin = rssi - sensitivity;
  if (margin >= 20) return "strong";
  if (margin >= 10) return "medium";
  if (margin >= 0) return "weak";
  return "none";
}

export const SIGNAL_COLORS: Record<SignalLevel, string> = {
  strong: "#2f9e5f",
  medium: "#a3a83a",
  weak: "#c27a32",
  none: "#4a2a2f",
};

export const nodeLabel = (nodes: NodeConfig[], id: string | null | undefined) =>
  (id && nodes.find((node) => node.id === id)?.name) || id || "?";
