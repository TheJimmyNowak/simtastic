import { useMemo, useState } from "react";
import type { LinkAttempt, Packets, PacketTx, Scenario } from "../api";
import { nodeLabel } from "../model";
import { Callout } from "../ui";

type Props = { scenario: Scenario; packets: Packets | null };

const time = (iso: string) => new Date(iso).toLocaleTimeString("pl-PL", { hour12: false });
const portLabel = (portnum: string | null) => (portnum ?? "?").replace(/_APP$/, "");

function hops(limit: number | null, start: number | null) {
  if (limit === null || start === null) return "—";
  return `${start - limit} z ${start}`;
}

export function PacketsStep({ scenario, packets }: Props) {
  const [showAttempts, setShowAttempts] = useState(false);
  const nodes = scenario.nodes;
  const transmissions = packets?.transmissions ?? [];
  const attempts = packets?.recent_attempts ?? [];

  /** Reception attempts of one transmission: same packet and transmitter, within 1 s. */
  const outcomeOf = useMemo(() => {
    const byKey = new Map<string, LinkAttempt[]>();
    for (const a of attempts) {
      const key = `${a.packet_id}:${a.tx_node}`;
      byKey.set(key, [...(byKey.get(key) ?? []), a]);
    }
    return (t: PacketTx) => {
      const sent = Date.parse(t.timestamp);
      const own = (byKey.get(`${t.packet_id}:${t.tx_node}`) ?? []).filter(
        (a) => Math.abs(Date.parse(a.timestamp) - sent) < 1000
      );
      return own.length ? { ok: own.filter((a) => a.delivered).length, total: own.length } : null;
    };
  }, [attempts]);

  const delivered = attempts.filter((a) => a.delivered).length;
  const relays = transmissions.filter((t) => t.is_rebroadcast).length;
  const running = scenario.status === "running" || scenario.status === "starting";

  if (!packets || transmissions.length === 0) {
    return (
      <div className="panel">
        {running ? (
          <Callout kind="info">
            Czekam na ruch radiowy. Pierwsze pakiety (NodeInfo, Position, harmonogram ruchu) pojawiają się zwykle w ciągu
            kilkunastu sekund od startu.
          </Callout>
        ) : (
          <Callout kind="info">Brak zarejestrowanych transmisji. Uruchom symulację w kroku „Symulacja”.</Callout>
        )}
      </div>
    );
  }

  return (
    <div className="panel">
      <div className="kpis">
        <div><span>Transmisje</span><strong>{transmissions.length}</strong></div>
        <div><span>W tym retransmisje</span><strong>{relays}</strong></div>
        <div><span>Próby odbioru</span><strong>{attempts.length}</strong></div>
        <div>
          <span>Udane próby</span>
          <strong>{attempts.length ? `${Math.round((delivered / attempts.length) * 100)}%` : "—"}</strong>
        </div>
      </div>
      <p className="muted">
        Ostatnie {transmissions.length} transmisji{running ? ", odświeżane na bieżąco" : ""}. Autor to węzeł, który
        utworzył pakiet; nadajnik to węzeł, który go wysłał w eter (różny przy retransmisji).
      </p>

      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Czas</th>
              <th>Pakiet</th>
              <th>Typ</th>
              <th>Autor</th>
              <th>Nadajnik</th>
              <th>Do</th>
              <th>Przeskok</th>
              <th>Odebrało</th>
            </tr>
          </thead>
          <tbody>
            {[...transmissions].reverse().map((t) => {
              const result = outcomeOf(t);
              return (
                <tr key={`${t.packet_id}-${t.tx_node}-${t.timestamp}`} className={t.is_rebroadcast ? "relay" : undefined}>
                  <td>{time(t.timestamp)}</td>
                  <td className="mono">{t.packet_id.toString(16)}</td>
                  <td>{portLabel(t.portnum)}</td>
                  <td>{nodeLabel(nodes, t.origin_node)}</td>
                  <td>
                    {nodeLabel(nodes, t.tx_node)}
                    {t.is_rebroadcast && <span className="tag">retransmisja</span>}
                  </td>
                  <td>{t.to_node === null ? "wszyscy" : nodeLabel(nodes, t.to_node)}</td>
                  <td>{hops(t.hop_limit, t.hop_start)}</td>
                  <td>{result ? `${result.ok} z ${result.total}` : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <button className="button ghost" onClick={() => setShowAttempts(!showAttempts)}>
        {showAttempts ? "Ukryj" : "Pokaż"} próby odbioru ({attempts.length})
      </button>
      {showAttempts && (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Czas</th>
                <th>Pakiet</th>
                <th>Nadajnik → odbiornik</th>
                <th>RSSI [dBm]</th>
                <th>Fading [dB]</th>
                <th>Budynki [dB]</th>
                <th>Wynik</th>
              </tr>
            </thead>
            <tbody>
              {[...attempts].reverse().map((a, i) => (
                <tr key={i}>
                  <td>{time(a.timestamp)}</td>
                  <td className="mono">{a.packet_id.toString(16)}</td>
                  <td>
                    {nodeLabel(nodes, a.tx_node)} → {nodeLabel(nodes, a.rx_node)}
                  </td>
                  <td>{a.rssi_db.toFixed(1)}</td>
                  <td>{a.shadow_db >= 0 ? "+" : ""}{a.shadow_db.toFixed(1)}</td>
                  <td>{a.building_diffraction_db ? a.building_diffraction_db.toFixed(1) : "—"}</td>
                  <td className={a.delivered ? "ok" : "fail"}>{a.delivered ? "odebrany" : "utracony"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
