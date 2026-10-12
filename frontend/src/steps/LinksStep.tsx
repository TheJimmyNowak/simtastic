import type { Links, LoraPreset, Scenario } from "../api";
import { NodeMap } from "../components/NodeMap";
import { SIGNAL_COLORS, isBusy, nodeLabel, signalLevel } from "../model";
import { Callout, Progress } from "../ui";

type Props = {
  scenario: Scenario;
  links: Links | null;
  preset: LoraPreset | undefined;
  dirty: boolean;
  onPrecompute: () => void;
  onNext: () => void;
};

export function LinksStep({ scenario, links, preset, dirty, onPrecompute, onNext }: Props) {
  const computing = scenario.status === "precomputing";
  const ready = links !== null && !computing;
  const sensitivity = preset?.sensitivity_dbm ?? -130;
  const canCompute = scenario.nodes.length >= 2 && !isBusy(scenario.status);

  return (
    <div className="panel">
      <div className="action-card">
        <div>
          <h3>Macierz propagacji</h3>
          <p>
            ITM (Longley–Rice) liczy straty terenowe z numerycznego modelu terenu GUGiK, a model ITU-R P.526
            dodaje dyfrakcję na budynkach LoD1 przecinających strefę Fresnela. Brakujące dane GUGiK pobierane
            są automatycznie. Wynik to medianowy RSSI dla każdej pary nadajnik → odbiornik.
          </p>
        </div>
        <button className="button primary large" onClick={onPrecompute} disabled={!canCompute}>
          {computing ? "Obliczanie…" : ready ? "Oblicz ponownie" : dirty ? "Zapisz i oblicz łącza" : "Oblicz łącza"}
        </button>
      </div>

      {scenario.nodes.length < 2 && <Callout kind="warning">Dodaj co najmniej dwa węzły.</Callout>}
      {computing && <Progress done={scenario.progress_done} total={scenario.progress_total} unit={scenario.progress_unit || "kierunków"} />}
      {!links && !computing && scenario.nodes.length >= 2 && (
        <Callout kind="info">Łącza nie są obliczone dla aktualnych ustawień scenariusza.</Callout>
      )}

      {ready && links && (
        <>
          <div className="grid2 links-view">
            <div>
              <h3 className="subheading">Mapa łączy</h3>
              <NodeMap nodes={scenario.nodes} links={links} sensitivityDbm={sensitivity} height={420} />
            </div>
            <div className="placements">
              <h3 className="subheading">Montaż anten</h3>
              <ul>
                {links.node_ids.map((id) => {
                  const p = links.node_placement[id];
                  return (
                    <li key={id}>
                      <strong>{nodeLabel(scenario.nodes, id)}</strong>{" "}
                      {p?.placement === "rooftop" ? `na dachu budynku ${p.building_id ?? ""}` : "na terenie"}
                      {p ? `, maszt ${p.mast_height_m} m` : ""}
                    </li>
                  );
                })}
              </ul>
              <p className="muted">Model: {links.propagation_model}</p>
            </div>
          </div>

          <h3 className="subheading">Medianowy RSSI [dBm] (wiersz: nadajnik, kolumna: odbiornik)</h3>
          <div className="heatmap">
            <table>
              <thead>
                <tr>
                  <th />
                  {links.node_ids.map((id) => (
                    <th key={id} scope="col">{nodeLabel(scenario.nodes, id)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {links.node_ids.map((from, i) => (
                  <tr key={from}>
                    <th scope="row">{nodeLabel(scenario.nodes, from)}</th>
                    {links.node_ids.map((to, j) => {
                      if (i === j) return <td key={to} className="diagonal">—</td>;
                      const rssi = links.median_rssi_db[i][j];
                      const buildings = links.building_loss_db?.[i]?.[j] ?? 0;
                      const title = [
                        `teren (ITM): ${links.terrain_loss_db?.[i]?.[j]?.toFixed(1) ?? "?"} dB`,
                        `budynki: ${buildings.toFixed(1)} dB (${links.blocked_buildings_count?.[i]?.[j] ?? 0} przeszkód)`,
                        `cieniowanie: ${(links.link_shadowing_db?.[i]?.[j] ?? 0).toFixed(1)} dB`,
                        `odległość: ${(links.distance_m[i][j] / 1000).toFixed(2)} km`,
                      ].join("\n");
                      return (
                        <td key={to} title={title} style={{ background: SIGNAL_COLORS[signalLevel(rssi, sensitivity)] }}>
                          {rssi.toFixed(0)}
                          {buildings > 0 && <span className="building-mark" aria-label="wpływ budynków"> ▲</span>}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="legend">
            <span><i className="signal" style={{ background: SIGNAL_COLORS.strong }} /> zapas ≥ 20 dB</span>
            <span><i className="signal" style={{ background: SIGNAL_COLORS.medium }} /> 10–20 dB</span>
            <span><i className="signal" style={{ background: SIGNAL_COLORS.weak }} /> 0–10 dB</span>
            <span><i className="signal" style={{ background: SIGNAL_COLORS.none }} /> poniżej czułości ({sensitivity} dBm)</span>
            <span>▲ strata na budynkach</span>
          </div>
          <div className="form-actions">
            <button className="button primary" onClick={onNext}>
              Dalej: symulacja →
            </button>
          </div>
        </>
      )}
    </div>
  );
}
