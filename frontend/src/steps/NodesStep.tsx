import type { NodeConfig, Role, ScenarioDraft } from "../api";
import { INFRASTRUCTURE_ROLES, ROLES } from "../api";
import { NodeMap } from "../components/NodeMap";
import { newNode } from "../model";
import { Field, NumberField } from "../ui";

type Props = {
  draft: ScenarioDraft;
  selected: number;
  disabled: boolean;
  onSelect: (index: number) => void;
  onChange: (draft: ScenarioDraft) => void;
  onNext: () => void;
};

const ROLE_HINTS: Record<Role, string> = {
  CLIENT: "Zwykły węzeł — retransmituje, jeśli nikt inny tego nie zrobił.",
  CLIENT_MUTE: "Nie retransmituje cudzych pakietów.",
  CLIENT_HIDDEN: "Nadaje tylko gdy musi — ogranicza własny ruch.",
  CLIENT_BASE: "Zawsze retransmituje pakiety ulubionych węzłów, resztę jak CLIENT.",
  ROUTER: "Węzeł infrastrukturalny — zawsze retransmituje, z priorytetem.",
  ROUTER_LATE: "Zawsze retransmituje, ale dopiero po pozostałych węzłach.",
  TRACKER: "Priorytetowo i częściej nadaje pozycję.",
  SENSOR: "Priorytetowo nadaje telemetrię środowiskową.",
  TAK: "Ruch ograniczony do współpracy z ATAK.",
  TAK_TRACKER: "Jak TAK, z automatycznym nadawaniem pozycji.",
  LOST_AND_FOUND: "Okresowo rozgłasza swoją pozycję na kanale głównym.",
};

export function NodesStep({ draft, selected, disabled, onSelect, onChange, onNext }: Props) {
  const nodes = draft.nodes;
  const node: NodeConfig | undefined = nodes[selected];

  const setNodes = (next: NodeConfig[]) => onChange({ ...draft, nodes: next });
  const updateNode = (patch: Partial<NodeConfig>) =>
    setNodes(nodes.map((n, i) => (i === selected ? { ...n, ...patch } : n)));

  const addNode = () => {
    setNodes([...nodes, newNode(nodes)]);
    onSelect(nodes.length);
  };
  const removeNode = () => {
    if (!node || !window.confirm(`Usunąć węzeł „${node.name || node.id}”?`)) return;
    setNodes(nodes.filter((_, i) => i !== selected));
    onSelect(Math.max(0, Math.min(selected, nodes.length - 2)));
  };

  return (
    <div className="panel">
      <div className="node-toolbar">
        <div className="node-tabs" role="tablist">
          {nodes.map((n, i) => (
            <button key={n.id} role="tab" aria-selected={selected === i} className={selected === i ? "active" : ""} onClick={() => onSelect(i)}>
              <span>{i + 1}</span>
              {n.name || n.id}
            </button>
          ))}
        </div>
        <button className="button primary" onClick={addNode} disabled={disabled}>
          + Dodaj węzeł
        </button>
      </div>

      {nodes.length < 2 && (
        <p className="muted">Do obliczenia łączy potrzebne są co najmniej dwa węzły.</p>
      )}

      <div className="grid2 node-editor">
        {node ? (
          <fieldset disabled={disabled}>
            <div className="editor-title">
              <div>
                <span>WĘZEŁ {selected + 1} · {node.id}</span>
                <h3>{node.name || node.id}</h3>
              </div>
              <button type="button" className="button danger ghost" onClick={removeNode}>
                Usuń węzeł
              </button>
            </div>
            <div className="form-grid">
              <Field label="Nazwa">
                {(id) => <input id={id} value={node.name} onChange={(e) => updateNode({ name: e.target.value })} />}
              </Field>
              <Field label="Rola" hint={ROLE_HINTS[node.role]}>
                {(id) => (
                  <select id={id} value={node.role} onChange={(e) => updateNode({ role: e.target.value as Role })}>
                    {ROLES.map((role) => (
                      <option key={role}>{role}</option>
                    ))}
                  </select>
                )}
              </Field>
              <NumberField label="Limit przeskoków" value={node.hop_limit} min={0} max={7} step={1} onChange={(v) => updateNode({ hop_limit: v ?? 3 })} />
              <NumberField
                label="Wysokość masztu [m]"
                value={node.agl_m}
                min={0}
                step={0.5}
                hint="Nad terenem lub nad dachem, jeśli węzeł stoi na budynku."
                onChange={(v) => updateNode({ agl_m: v ?? 0 })}
              />
              <NumberField label="Zysk anteny TX [dBi]" value={node.antenna_gain_tx_db} step={0.5} onChange={(v) => updateNode({ antenna_gain_tx_db: v ?? 0 })} />
              <NumberField label="Zysk anteny RX [dBi]" value={node.antenna_gain_rx_db} step={0.5} onChange={(v) => updateNode({ antenna_gain_rx_db: v ?? 0 })} />
              <NumberField label="Szerokość geogr." value={node.lat} min={-90} max={90} step="0.000001" onChange={(v) => updateNode({ lat: v ?? node.lat })} />
              <NumberField label="Długość geogr." value={node.lon} min={-180} max={180} step="0.000001" onChange={(v) => updateNode({ lon: v ?? node.lon })} />
            </div>
            <div className="form-actions">
              <button type="button" className="button" disabled={nodes.length < 2} onClick={onNext}>
                Dalej: łącza →
              </button>
            </div>
          </fieldset>
        ) : (
          <div className="empty-inline">
            <h3>Brak węzłów</h3>
            <p>Dodaj co najmniej dwa węzły, aby obliczyć łącza radiowe.</p>
          </div>
        )}
        <div className="map-wrap">
          <div className="map-help">
            {disabled ? "Zatrzymaj symulację, aby edytować węzły." : "Kliknij węzeł, aby go wybrać; kliknij mapę, aby przenieść wybrany węzeł."}
          </div>
          <NodeMap
            nodes={nodes}
            selectedIndex={selected}
            onSelect={onSelect}
            onPick={node && !disabled ? (lat, lon) => updateNode({ lat, lon }) : undefined}
          />
          <div className="legend">
            <span><i className="dot client" /> pozostałe role</span>
            <span><i className="dot router" /> {INFRASTRUCTURE_ROLES.join(" / ")}</span>
          </div>
        </div>
      </div>
    </div>
  );
}
