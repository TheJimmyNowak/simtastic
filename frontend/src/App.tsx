import { useCallback, useEffect, useState } from "react";
import { MapContainer, TileLayer, Marker, useMapEvents } from "react-leaflet";
import L from "leaflet";
import { api, Scenario, NodeConfig } from "./api";

const STEPS = ["Środowisko", "Węzły", "Łącza ITM", "Symulacja", "Pakiety"];
const STEP_HINTS = [
  "Parametry propagacji i eksperymentu",
  "Położenie oraz konfiguracja urządzeń",
  "Obliczenie terenu, budynków i zasięgu",
  "Uruchomienie firmware Meshtastic",
  "Ślad transmisji i prób radiowych",
];

const STATUS_LABELS: Record<string, string> = {
  draft: "Wersja robocza",
  precomputing: "Obliczanie łączy",
  links_ready: "Łącza gotowe",
  starting: "Uruchamianie",
  running: "Symulacja działa",
  stopping: "Zatrzymywanie",
  stopped: "Zatrzymano",
  error: "Błąd",
};

/** Parse number input; 0 is valid (unlike `+value` on empty string which is also 0). */
function parseNumberField(raw: string, fallback = 0): number {
  if (raw.trim() === "") return fallback;
  const n = Number(raw);
  return Number.isFinite(n) ? n : fallback;
}

function MapClick({ onPick }: { onPick: (lat: number, lon: number) => void }) {
  useMapEvents({ click: (e) => onPick(e.latlng.lat, e.latlng.lng) });
  return null;
}

export default function App() {
  const [step, setStep] = useState(0);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [current, setCurrent] = useState<Scenario | null>(null);
  const [selectedNode, setSelectedNode] = useState(0);
  const [links, setLinks] = useState<any>(null);
  const [packets, setPackets] = useState<any>(null);
  const [presets, setPresets] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dryRun, setDryRun] = useState(false);
  const [starting, setStarting] = useState(false);
  const [activePacketScenarioId, setActivePacketScenarioId] = useState<string | null>(null);

  const refreshList = useCallback(async () => {
    const list = await api<Scenario[]>("/api/scenarios");
    setScenarios(list);
  }, []);

  const reloadCurrent = useCallback(async (scenarioId?: string) => {
    const id = scenarioId ?? current?.id;
    if (!id) return;
    const s = await api<Scenario>(`/api/scenarios/${id}`);
    setCurrent(s);
  }, [current?.id]);

  const refresh = useCallback(async () => {
    await refreshList();
    await reloadCurrent();
  }, [refreshList, reloadCurrent]);

  useEffect(() => {
    api<Record<string, unknown>>("/api/presets").then(setPresets).catch(() => {});
    api<{ dry_run: boolean }>("/api/config").then((c) => setDryRun(c.dry_run)).catch(() => {});
    refresh();
  }, []);

  useEffect(() => {
    if (!current) return;
    const poll = async () => {
      try {
        const st = await api<{
          active_scenario_id?: string | null;
          gateway_running?: boolean;
        }>(`/api/scenarios/${current.id}/status`);
        const packetSid = st.active_scenario_id || current.id;
        setActivePacketScenarioId(st.active_scenario_id || null);
        if (st.gateway_running || ["running", "starting"].includes(current.status)) {
          const p = await api(`/api/scenarios/${packetSid}/packets?limit=50`);
          setPackets(p);
        }
        const s = await api<Scenario>(`/api/scenarios/${current.id}`);
        setScenarios((prev) =>
          prev.map((x) => (x.id === s.id ? { ...x, status: s.status } : x))
        );
        setCurrent((prev) =>
          prev && prev.id === s.id
            ? {
                ...prev,
                status: s.status,
                error_message: s.error_message,
                itm_progress: s.itm_progress,
                itm_total: s.itm_total,
              }
            : prev
        );
      } catch {}
    };
    poll();
    const t = setInterval(poll, 2000);
    return () => clearInterval(t);
  }, [current?.id, current?.status]);

  const createScenario = async () => {
    const s = await api<Scenario>("/api/scenarios", {
      method: "POST",
      body: JSON.stringify({
        name: `Scenariusz ${scenarios.length + 1}`,
        environment: { preset: "LONG_FAST", fading_sigma_db: 8, shard_size: 10, experiment_id: "exp1" },
        nodes: [
          { id: "node0", name: "N0", lat: 52.0, lon: 21.0, agl_m: 10, hop_limit: 3, role: "CLIENT" },
          { id: "node1", name: "N1", lat: 52.05, lon: 21.08, agl_m: 5, hop_limit: 3, role: "ROUTER" },
          { id: "node2", name: "N2", lat: 52.1, lon: 21.15, agl_m: 8, hop_limit: 5, role: "CLIENT" },
        ],
      }),
    });
    setCurrent(s);
    setSelectedNode(0);
    setLinks(null);
    setPackets(null);
    setStep(0);
    setNotice("Utworzono scenariusz. Nadaj mu nazwę i skonfiguruj środowisko.");
    await refreshList();
  };

  const saveScenario = async () => {
    if (!current) return;
    setError(null);
    const saved = await api<Scenario>(`/api/scenarios/${current.id}`, {
      method: "PUT",
      body: JSON.stringify({ name: current.name, environment: current.environment, nodes: current.nodes }),
    });
    setCurrent(saved);
    setLinks(null);
    setNotice("Zapisano zmiany. Jeśli scenariusz był wcześniej obliczony, wykonaj ponownie Precompute ITM.");
    await refreshList();
  };

  const selectScenario = async (id: string) => {
    setError(null);
    setNotice(null);
    setLinks(null);
    setPackets(null);
    setSelectedNode(0);
    if (!id) {
      setCurrent(null);
      return;
    }
    const scenario = await api<Scenario>(`/api/scenarios/${id}`);
    setCurrent(scenario);
    setStep(0);
    if (scenario.status === "links_ready" || scenario.status === "running") {
      api(`/api/scenarios/${id}/links`).then(setLinks).catch(() => {});
    }
  };

  const deleteScenario = async () => {
    if (!current) return;
    if (["running", "starting", "stopping", "precomputing"].includes(current.status)) {
      setError("Najpierw zatrzymaj symulację lub poczekaj na zakończenie obliczeń.");
      return;
    }
    if (!window.confirm(`Usunąć scenariusz „${current.name}”? Tej operacji nie można cofnąć.`)) return;
    await api(`/api/scenarios/${current.id}`, { method: "DELETE" });
    setCurrent(null);
    setLinks(null);
    setPackets(null);
    setStep(0);
    setNotice("Scenariusz został usunięty.");
    await refreshList();
  };

  const deleteSelectedNode = () => {
    if (!current || !node) return;
    if (!window.confirm(`Usunąć węzeł „${node.name || node.id}”?`)) return;
    const nodes = current.nodes.filter((_, index) => index !== selectedNode);
    setCurrent({ ...current, nodes });
    setSelectedNode(Math.max(0, Math.min(selectedNode, nodes.length - 1)));
    setNotice("Węzeł usunięto lokalnie. Kliknij „Zapisz węzły”, aby zatwierdzić zmianę.");
  };

  const addNode = () => {
    if (!current) return;
    let index = current.nodes.length;
    while (current.nodes.some((item) => item.id === `node${index}`)) index += 1;
    const previous = current.nodes[current.nodes.length - 1];
    const id = `node${index}`;
    const suffix = crypto.getRandomValues(new Uint8Array(3));
    const mac = `AA:BB:CC:${[...suffix].map((value) => value.toString(16).padStart(2, "0").toUpperCase()).join(":")}`;
    const newNode: NodeConfig = {
      id,
      name: `Węzeł ${index + 1}`,
      lat: (previous?.lat ?? 52) + (previous ? 0.005 : 0),
      lon: (previous?.lon ?? 21) + (previous ? 0.005 : 0),
      agl_m: 3,
      antenna_gain_tx_db: 2,
      antenna_gain_rx_db: 2,
      hop_limit: 3,
      role: "CLIENT",
      noise_floor_dbm: -120,
      mac_address: mac,
    };
    setCurrent({ ...current, nodes: [...current.nodes, newNode] });
    setSelectedNode(current.nodes.length);
    setNotice("Dodano węzeł. Ustaw jego pozycję kliknięciem na mapie.");
  };

  const precompute = async () => {
    if (!current) return;
    await saveScenario();
    await api(`/api/scenarios/${current.id}/precompute-links`, { method: "POST" });
    setNotice("Rozpoczęto obliczanie łączy. Możesz obserwować postęp poniżej.");
    const poll = setInterval(async () => {
      const s = await api<Scenario>(`/api/scenarios/${current.id}`);
      setCurrent(s);
      if (s.status === "links_ready" || s.status === "error") {
        clearInterval(poll);
        if (s.status === "links_ready") {
          setLinks(await api(`/api/scenarios/${current.id}/links`));
        }
      }
    }, 1000);
  };

  const startSim = async () => {
    if (!current) return;
    setError(null);
    setStarting(true);
    try {
      await api(`/api/scenarios/${current.id}/start`, { method: "POST", body: "{}" });
      const t0 = Date.now();
      const poll = setInterval(async () => {
        const s = await api<Scenario>(`/api/scenarios/${current.id}`);
        const st = await api<{
          scenario_status: string;
          error_message?: string;
          start_in_progress?: boolean;
        }>(`/api/scenarios/${current.id}/status`);
        setCurrent(s);
        if (st.scenario_status === "running") {
          clearInterval(poll);
          setStarting(false);
          setStep(4);
        } else if (st.scenario_status === "error") {
          clearInterval(poll);
          setStarting(false);
          setError(st.error_message || "Start failed");
        } else if (Date.now() - t0 > 600_000) {
          clearInterval(poll);
          setStarting(false);
          setError("Start timeout (>10 min). Użyj Stop i spróbuj ponownie.");
        }
      }, 1500);
    } catch (e: unknown) {
      setStarting(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const stopSim = async () => {
    if (!current) return;
    try {
      const res = await api(`/api/scenarios/${current.id}/stop`, { method: "POST" });
      const removed = (res as { containers_removed?: string[] }).containers_removed;
      setError(null);
      setNotice(removed?.length ? `Zatrzymano symulację i usunięto ${removed.length} kontenerów.` : "Symulacja została zatrzymana.");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
    await refresh();
  };

  const forceCleanupDocker = async () => {
    try {
      const res = await api("/api/docker/cleanup-all", { method: "POST" });
      const removed = (res as { containers_removed?: string[] }).containers_removed ?? [];
      setNotice(removed.length ? `Usunięto ${removed.length} osieroconych kontenerów.` : "Nie znaleziono osieroconych kontenerów.");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
    await refresh();
  };

  const node = current?.nodes[selectedNode];

  const rssiColor = (v: number) => {
    if (v < -130) return "#3d1f1f";
    if (v < -120) return "#4d3a1f";
    if (v < -110) return "#3d4d1f";
    return "#1f4d2d";
  };

  return (
    <div className="app">
      <header className="hero">
        <div>
          <span className="eyebrow">ŚRODOWISKO BADAWCZE</span>
          <h1>Meshtastic Lab</h1>
          <p>Buduj scenariusz krok po kroku, oblicz propagację i obserwuj routing firmware.</p>
        </div>
        <div className={`status-badge status-${current?.status || "idle"}`}>
          <span className="status-dot" />
          {current ? STATUS_LABELS[current.status] || current.status : "Brak scenariusza"}
        </div>
      </header>

      <section className="scenario-bar">
        <div className="scenario-picker">
          <label htmlFor="scenario-select">Aktywny scenariusz</label>
          <select id="scenario-select" value={current?.id || ""} onChange={(e) => selectScenario(e.target.value)}>
            <option value="">Wybierz scenariusz…</option>
            {scenarios.map((s) => <option key={s.id} value={s.id}>{s.name} — {STATUS_LABELS[s.status] || s.status}</option>)}
          </select>
        </div>
        <button className="button primary" onClick={createScenario}>+ Nowy scenariusz</button>
        {current && <button className="button danger ghost" onClick={deleteScenario}>Usuń scenariusz</button>}
      </section>

      {notice && <div className="callout success"><span>✓</span><p>{notice}</p><button onClick={() => setNotice(null)}>×</button></div>}
      {error && <div className="callout error"><span>!</span><pre>{error}</pre><button onClick={() => setError(null)}>×</button></div>}

      {!current && (
        <section className="empty-state panel">
          <div className="empty-icon">⌁</div>
          <h2>Zacznij od scenariusza</h2>
          <p>Utwórz nowy eksperyment albo wybierz istniejący. Następnie przeprowadzimy Cię przez konfigurację węzłów i modelu radiowego.</p>
          <button className="button primary large" onClick={createScenario}>Utwórz pierwszy scenariusz</button>
        </section>
      )}

      {current && (
        <nav className="wizard" aria-label="Etapy konfiguracji">
          {STEPS.map((label, index) => (
            <button key={label} className={step === index ? "active" : ""} onClick={() => setStep(index)}>
              <span className="step-number">{index + 1}</span>
              <span><strong>{label}</strong><small>{STEP_HINTS[index]}</small></span>
            </button>
          ))}
        </nav>
      )}

      {current && <div className="section-heading"><div><span>KROK {step + 1} Z {STEPS.length}</span><h2>{STEPS[step]}</h2><p>{STEP_HINTS[step]}</p></div></div>}

      {step === 0 && current && (
        <div className="panel grid2">
          <div>
            <label>Nazwa scenariusza</label>
            <input value={current.name} maxLength={120} onChange={(e) => setCurrent({ ...current, name: e.target.value })} />
            <label>Identyfikator eksperymentu</label>
            <input value={current.environment.experiment_id} onChange={(e) => setCurrent({ ...current, environment: { ...current.environment, experiment_id: e.target.value } })} />
            <label>Preset LoRa</label>
            <select value={current.environment.preset} onChange={(e) => setCurrent({ ...current, environment: { ...current.environment, preset: e.target.value } })}>
              {Object.keys(presets).map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <label>Fading σ (dB)</label>
            <input
              type="number"
              min={0}
              step={0.1}
              value={current.environment.fading_sigma_db}
              onChange={(e) =>
                setCurrent({
                  ...current,
                  environment: {
                    ...current.environment,
                    fading_sigma_db: parseNumberField(e.target.value, 0),
                  },
                })
              }
            />
            <label>Shard size</label>
            <input
              type="number"
              min={1}
              value={current.environment.shard_size}
              onChange={(e) =>
                setCurrent({
                  ...current,
                  environment: {
                    ...current.environment,
                    shard_size: parseNumberField(e.target.value, 10),
                  },
                })
              }
            />
            <label>Seed fadingu (puste = losowy)</label>
            <input
              type="number"
              value={current.environment.random_seed ?? ""}
              onChange={(e) =>
                setCurrent({
                  ...current,
                  environment: {
                    ...current.environment,
                    random_seed: e.target.value.trim() === "" ? null : parseNumberField(e.target.value),
                  },
                })
              }
            />
            <label>
              <input
                type="checkbox"
                checked={current.environment.building_model_enabled !== false}
                onChange={(e) =>
                  setCurrent({
                    ...current,
                    environment: { ...current.environment, building_model_enabled: e.target.checked },
                  })
                }
              />{" "}
              Budynki LoD1 (ITM + P.526)
            </label>
            <label>Wymagany prześwit Fresnela</label>
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={current.environment.fresnel_clearance_ratio ?? 0.6}
              onChange={(e) =>
                setCurrent({
                  ...current,
                  environment: {
                    ...current.environment,
                    fresnel_clearance_ratio: parseNumberField(e.target.value, 0.6),
                  },
                })
              }
            />
            <div className="form-actions">
              <button className="button primary" onClick={saveScenario}>Zapisz środowisko</button>
              <button className="button" onClick={() => setStep(1)}>Dalej: węzły →</button>
            </div>
          </div>
          <aside className="info-card">
            <h3>Co konfigurujesz?</h3>
            <p>Preset określa parametry LoRa, fading dodaje losową zmienność sygnału, a seed pozwala odtworzyć identyczny przebieg.</p>
            <p>Model budynków rozpoznaje automatycznie, czy antena znajduje się na dachu. AGL jest wtedy wysokością masztu nad dachem.</p>
          </aside>
        </div>
      )}

      {step === 1 && current && (
        <div className="panel">
          <div className="node-toolbar">
            <div className="node-tabs">
            {current.nodes.map((n, i) => (
              <button key={n.id} className={selectedNode === i ? "active" : ""} onClick={() => setSelectedNode(i)}>
                <span>{i + 1}</span>{n.name || n.id}
              </button>
            ))}
            </div>
            <button className="button primary" onClick={addNode}>+ Dodaj węzeł</button>
          </div>

          {!node && <div className="empty-inline"><h3>Brak węzłów</h3><p>Dodaj co najmniej dwa węzły, aby obliczyć łącza radiowe.</p><button className="button primary" onClick={addNode}>Dodaj pierwszy węzeł</button></div>}

          {node && <div className="grid2 node-editor">
            <div>
              <div className="editor-title"><div><span>WĘZEŁ {selectedNode + 1}</span><h3>{node.name || node.id}</h3></div><button className="button danger ghost" onClick={deleteSelectedNode}>Usuń węzeł</button></div>
              <div className="form-grid">
                <div><label>Nazwa</label><input value={node.name} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, name: e.target.value }; setCurrent({ ...current, nodes }); }} /></div>
                <div><label>Identyfikator</label><input value={node.id} disabled title="Identyfikator jest stały po utworzeniu węzła" /></div>
                <div><label>Rola</label><select value={node.role} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, role: e.target.value }; setCurrent({ ...current, nodes }); }}>{["CLIENT","ROUTER","REPEATER","CLIENT_MUTE"].map((r) => <option key={r}>{r}</option>)}</select></div>
                <div><label>Limit przeskoków</label><input type="number" min={0} max={7} value={node.hop_limit} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, hop_limit: parseNumberField(e.target.value, 3) }; setCurrent({ ...current, nodes }); }} /></div>
                <div><label>Wysokość masztu AGL (m)</label><input type="number" min={0} step={0.5} value={node.agl_m} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, agl_m: parseNumberField(e.target.value, 0) }; setCurrent({ ...current, nodes }); }} /></div>
                <div><label>Zysk anteny (TX/RX dBi)</label><div className="inline-inputs"><input type="number" step={0.5} value={node.antenna_gain_tx_db} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, antenna_gain_tx_db: parseNumberField(e.target.value) }; setCurrent({ ...current, nodes }); }} /><input type="number" step={0.5} value={node.antenna_gain_rx_db} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, antenna_gain_rx_db: parseNumberField(e.target.value) }; setCurrent({ ...current, nodes }); }} /></div></div>
                <div><label>Szerokość geograficzna</label><input type="number" step="0.000001" value={node.lat} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, lat: parseNumberField(e.target.value) }; setCurrent({ ...current, nodes }); }} /></div>
                <div><label>Długość geograficzna</label><input type="number" step="0.000001" value={node.lon} onChange={(e) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, lon: parseNumberField(e.target.value) }; setCurrent({ ...current, nodes }); }} /></div>
              </div>
              <div className="form-actions"><button className="button primary" onClick={saveScenario}>Zapisz węzły</button><button className="button" disabled={current.nodes.length < 2} onClick={() => setStep(2)}>Dalej: łącza →</button></div>
            </div>
            <div className="map-wrap"><div className="map-help">Kliknij mapę, aby ustawić pozycję wybranego węzła.</div><MapContainer key={node.id} center={[node.lat, node.lon]} zoom={14} className="map"><TileLayer url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" />{current.nodes.map((n) => <Marker key={n.id} position={[n.lat, n.lon]} />)}<MapClick onPick={(lat, lon) => { const nodes = [...current.nodes]; nodes[selectedNode] = { ...node, lat, lon }; setCurrent({ ...current, nodes }); }} /></MapContainer></div>
          </div>}
        </div>
      )}

      {step === 2 && current && (
        <div className="panel">
          <div className="action-card">
            <div><h3>Oblicz macierz propagacji</h3><p>ITM uwzględni teren, a model P.526 budynki przecinające strefę Fresnela. Zmiana węzłów lub środowiska wymaga ponownego obliczenia.</p></div>
            <button className="button primary large" onClick={precompute} disabled={current.status === "precomputing" || current.nodes.length < 2}>
              {current.status === "precomputing" ? "Obliczanie…" : "Oblicz łącza"}
            </button>
          </div>
          {current.nodes.length < 2 && <div className="callout warning"><span>i</span><p>Dodaj co najmniej dwa węzły.</p></div>}
          {current.itm_total > 0 && <div className="progress-block"><div><span>Postęp</span><strong>{Math.round((current.itm_progress / current.itm_total) * 100)}%</strong></div><progress value={current.itm_progress} max={current.itm_total} /><small>{current.itm_progress} z {current.itm_total} kierunków</small></div>}
          {links && (
            <div className="heatmap">
              <p>Model: <strong>{links.propagation_model}</strong></p>
              {Object.entries(links.node_placement || {}).map(([id, placement]: [string, any]) => (
                <div key={`placement-${id}`}>
                  {id}: {placement.placement === "rooftop" ? "dach" : "teren"}, maszt {placement.mast_height_m} m
                  {placement.building_id ? `, budynek ${placement.building_id}` : ""}
                </div>
              ))}
              <table>
                <thead><tr><th></th>{links.node_ids.map((id: string) => <th key={id}>{id}</th>)}</tr></thead>
                <tbody>
                  {links.node_ids.map((from: string, i: number) => (
                    <tr key={from}><th>{from}</th>
                      {links.node_ids.map((to: string, j: number) => (
                        <td key={to} style={{ background: i === j ? "#222" : rssiColor(links.median_rssi_db[i][j]) }}>
                          {i === j ? "—" : (
                            <span title={`ITM: ${links.terrain_loss_db?.[i]?.[j]?.toFixed(1) ?? "?"} dB; budynki: ${links.building_loss_db?.[i]?.[j]?.toFixed(1) ?? "0"} dB; blokujące: ${links.blocked_buildings_count?.[i]?.[j] ?? 0}`}>
                              {links.median_rssi_db[i][j].toFixed(0)}
                              {(links.building_loss_db?.[i]?.[j] ?? 0) > 0 ? " 🏢" : ""}
                            </span>
                          )}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="legend"><span><i className="signal strong" /> silne</span><span><i className="signal medium" /> średnie</span><span><i className="signal weak" /> słabe</span><span>🏢 wpływ budynku</span></div>
              <div className="form-actions"><button className="button primary" onClick={() => setStep(3)}>Dalej: uruchom symulację →</button></div>
            </div>
          )}
        </div>
      )}

      {step === 3 && current && (
        <div className="panel">
          {dryRun && (
            <p style={{ color: "#f96" }}>
              Backend w trybie DRY_RUN — ustaw <code>DRY_RUN=false</code> i zrestartuj API.
            </p>
          )}
          <div className="simulation-actions"><button
            className="button primary large"
            onClick={startSim}
            disabled={starting || !["links_ready", "error"].includes(current.status)}
          >
            {starting ? "Uruchamianie…" : "Start (Docker)"}
          </button>
          <button
            className="button danger"
            onClick={stopSim}
            disabled={
              current.status !== "running" &&
              current.status !== "starting" &&
              current.status !== "error"
            }
          >
            Stop
          </button>
          <button className="button ghost" type="button" onClick={forceCleanupDocker}>
            Wyczyść osierocone kontenery
          </button>
          </div><div className="run-summary"><div><span>Status</span><strong>{STATUS_LABELS[current.status] || current.status}</strong></div><div><span>Eksperyment</span><strong>{current.environment.experiment_id}</strong></div><div><span>Węzły</span><strong>{current.nodes.length}</strong></div></div>
          {starting && current.itm_total > 0 && (
            <p>Uruchamianie węzłów: {Math.min(current.itm_progress, current.itm_total)} / {current.itm_total}</p>
          )}
          {current.status !== "links_ready" && current.status !== "running" && (
            <p>Najpierw: <strong>Precompute ITM</strong> (krok Łącza ITM).</p>
          )}
          {current.error_message && <pre style={{ color: "#f88" }}>{current.error_message}</pre>}
        </div>
      )}

      {step === 4 && current && (
        <p style={{ marginBottom: 8 }}>
          Źródło pakietów: <strong>{scenarios.find((s) => s.id === (activePacketScenarioId || current.id))?.name || activePacketScenarioId || current.id}</strong>
          {activePacketScenarioId ? ` (${activePacketScenarioId.slice(0, 8)}…)` : ""}
        </p>
      )}
      {step === 4 && (
        <div className="panel trace">
          {current?.status === "starting" && (
            <p>Symulacja się uruchamia… Pakiety pojawią się po statusie <strong>running</strong>.</p>
          )}
          {activePacketScenarioId && current && activePacketScenarioId !== current.id && (
            <p style={{ color: "#fa0" }}>
              Aktywna symulacja to inny scenariusz ({activePacketScenarioId.slice(0, 8)}…). Wybierz „test-net” na liście lub patrz poniżej — pakiety są z aktywnej symulacji.
            </p>
          )}
          {!packets?.transmissions?.length &&
            !packets?.recent_attempts?.length &&
            !packets?.deliveries?.length &&
            (activePacketScenarioId || current?.status === "running") && (
            <p>
              Oczekiwanie na ruch OTA (NodeInfo, Position, tekst, rebroadcasty) — pierwsze pakiety zwykle w ciągu
              ~10–90 s po starcie.
            </p>
          )}
          <p style={{ fontSize: "0.85em", opacity: 0.85, marginBottom: 8 }}>
            Śledzony jest cały ruch wychodzący z meshtasticd (wszystkie portnum) oraz symulacja RF (próby łącza +
            dostawy do odbiorców). Rebroadcasty węzłów pojawiają się jako osobne TX.
          </p>
          {packets?.transmissions?.map((t: any) => (
            <div key={`${t.packet_id}-${t.tx_node}-${t.timestamp}`}>
              TX {t.tx_node} pkt={t.packet_id} {t.portnum || "?"} hop={t.hop_limit ?? "?"}
              {t.hop_start != null ? `/${t.hop_start}` : ""} rebroadcast={String(t.is_rebroadcast)}
            </div>
          ))}
          {packets?.recent_attempts?.slice(-40).map((a: any, i: number) => (
            <div key={i} className={a.delivered ? "delivered" : "dropped"}>
              RF {a.tx_node}→{a.rx_node} pkt={a.packet_id} RSSI={a.rssi_db.toFixed(1)} {a.delivered ? "OK" : "DROP"}
              {(a.building_diffraction_db ?? 0) > 0
                ? ` budynki=${a.building_diffraction_db.toFixed(1)}dB (${a.blocked_buildings_count})`
                : ""}
            </div>
          ))}
          {packets?.deliveries?.slice(-20).map((d: any, i: number) => (
            <div key={`rx-${i}`} className="delivered">
              INJECT →{d.rx_node} pkt={d.packet_id} RSSI={d.rssi_db.toFixed(1)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
