import type { Scenario } from "../api";
import { STATUS_LABELS } from "../model";
import { Callout, Progress } from "../ui";

type Props = {
  scenario: Scenario;
  hasLinks: boolean;
  dirty: boolean;
  /** Another scenario is currently running. */
  otherActiveId: string | null;
  onStart: () => void;
  onStop: () => void;
  onCleanup: () => void;
  onGoToLinks: () => void;
  onShowPackets: () => void;
};

export function SimulationStep(props: Props) {
  const { scenario, hasLinks, dirty, otherActiveId, onStart, onStop, onCleanup, onGoToLinks, onShowPackets } = props;
  const { status } = scenario;
  const canStart = hasLinks && !dirty && !otherActiveId && (status === "links_ready" || status === "error");
  const canStop = status === "starting" || status === "running" || status === "error";

  return (
    <div className="panel">
      <div className="simulation-actions">
        <button className="button primary large" onClick={onStart} disabled={!canStart}>
          {status === "starting" ? "Uruchamianie…" : "Start"}
        </button>
        <button className="button danger large" onClick={onStop} disabled={!canStop}>
          Stop
        </button>
        {status === "running" && (
          <button className="button large" onClick={onShowPackets}>
            Zobacz pakiety →
          </button>
        )}
      </div>

      {!hasLinks && (
        <Callout kind="info">
          Najpierw oblicz łącza.{" "}
          <button className="link" onClick={onGoToLinks}>
            Przejdź do kroku „Łącza ITM”
          </button>
        </Callout>
      )}
      {dirty && <Callout kind="warning">Masz niezapisane zmiany — zapisz je przed uruchomieniem.</Callout>}
      {otherActiveId && otherActiveId !== scenario.id && (
        <Callout kind="warning">Działa już symulacja scenariusza „{otherActiveId}”. Zatrzymaj ją, aby uruchomić tę.</Callout>
      )}
      {status === "error" && scenario.error_message && (
        <Callout kind="error">
          <strong>Uruchomienie nie powiodło się</strong>
          <pre>{scenario.error_message}</pre>
        </Callout>
      )}

      <div className="run-summary">
        <div>
          <span>Status</span>
          <strong>{STATUS_LABELS[status]}</strong>
        </div>
        <div>
          <span>Eksperyment</span>
          <strong>{scenario.environment.experiment_id}</strong>
        </div>
        <div>
          <span>Węzły</span>
          <strong>{scenario.nodes.length}</strong>
        </div>
      </div>

      {status === "starting" && (
        <>
          <Progress done={scenario.progress_done} total={scenario.progress_total} unit="węzłów gotowych" />
          <p className="muted">
            Każdy węzeł to kontener <code>meshtasticd</code>: konfiguracja (region, preset, rola, limit przeskoków),
            restart i weryfikacja. Ruch testowy rusza kilka sekund po gotowości wszystkich węzłów.
          </p>
        </>
      )}

      <p className="muted">
        Ruch: harmonogram <code>data/scenarios/{scenario.id}/traffic.yaml</code> (tworzony automatycznie przy pierwszym
        starcie: dwa broadcasty i traceroute). Firmware dodatkowo nadaje własne pakiety NodeInfo i Position.
      </p>

      <details className="tools">
        <summary>Narzędzia awaryjne</summary>
        <p className="muted">
          Usuwa wszystkie kontenery symulatora (etykieta <code>mesh-sim</code>), np. po awarii backendu.
        </p>
        <button className="button ghost" onClick={onCleanup}>
          Usuń kontenery symulatora
        </button>
      </details>
    </div>
  );
}
