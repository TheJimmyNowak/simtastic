import { useCallback, useEffect, useRef, useState } from "react";
import type { Links, LoraPreset, Packets, Scenario, ScenarioDraft } from "./api";
import { scenarioApi } from "./api";
import { BUSY_STATUSES, STATUS_LABELS, defaultDraft, draftErrors, draftOf, isBusy, sameDraft } from "./model";
import { EnvironmentStep } from "./steps/EnvironmentStep";
import { LinksStep } from "./steps/LinksStep";
import { NodesStep } from "./steps/NodesStep";
import { PacketsStep } from "./steps/PacketsStep";
import { SimulationStep } from "./steps/SimulationStep";
import { Callout } from "./ui";

const STEPS = [
  { label: "Środowisko", hint: "Preset LoRa i parametry modelu" },
  { label: "Węzły", hint: "Rozmieszczenie i konfiguracja" },
  { label: "Łącza ITM", hint: "Teren, budynki, zasięg" },
  { label: "Symulacja", hint: "Firmware Meshtastic w Dockerze" },
  { label: "Pakiety", hint: "Transmisje i odbiory" },
];

const LINK_STATUSES = new Set(["links_ready", "starting", "running", "stopping", "stopped", "error"]);

type Message = { kind: "success" | "error"; text: string } | null;

export default function App() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [server, setServer] = useState<Scenario | null>(null);
  const [draft, setDraft] = useState<ScenarioDraft | null>(null);
  const [saved, setSaved] = useState<ScenarioDraft | null>(null);
  const [links, setLinks] = useState<Links | null>(null);
  const [packets, setPackets] = useState<Packets | null>(null);
  const [presets, setPresets] = useState<Record<string, LoraPreset>>({});
  const [activeId, setActiveId] = useState<string | null>(null);
  const [step, setStep] = useState(0);
  const [selectedNode, setSelectedNode] = useState(0);
  const [message, setMessage] = useState<Message>(null);
  const [working, setWorking] = useState(false);

  const dirty = draft !== null && saved !== null && !sameDraft(draft, saved);
  const errors = draft ? draftErrors(draft) : [];
  const status = server?.status;

  const fail = (e: unknown) => setMessage({ kind: "error", text: e instanceof Error ? e.message : String(e) });
  const ok = (text: string) => setMessage({ kind: "success", text });

  // Success messages disappear on their own; errors stay until dismissed.
  useEffect(() => {
    if (message?.kind !== "success") return;
    const timer = window.setTimeout(() => setMessage(null), 6000);
    return () => window.clearTimeout(timer);
  }, [message]);

  /** Run a user action: one at a time, errors shown in the message bar. */
  const run = async (action: () => Promise<void>) => {
    setWorking(true);
    setMessage(null);
    try {
      await action();
    } catch (e) {
      fail(e);
    } finally {
      setWorking(false);
    }
  };

  const refreshList = useCallback(() => scenarioApi.list().then(setScenarios), []);

  /** Server state of the selected scenario; keeps the status in the scenario list in sync. */
  const applyServer = (scenario: Scenario) => {
    setServer(scenario);
    setScenarios((list) => list.map((s) => (s.id === scenario.id ? { ...s, status: scenario.status } : s)));
  };

  const load = (scenario: Scenario) => {
    applyServer(scenario);
    setDraft(draftOf(scenario));
    setSaved(draftOf(scenario));
  };

  useEffect(() => {
    scenarioApi.presets().then(setPresets).catch(fail);
    refreshList().catch(fail);
  }, [refreshList]);

  // Warn before closing the tab with unsaved edits.
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // Single polling loop for the selected scenario: status, progress and packets.
  const serverId = server?.id;
  const statusRef = useRef(status);
  statusRef.current = status;
  useEffect(() => {
    if (!serverId) return;
    let timer: number | undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const [latest, sim] = await Promise.all([scenarioApi.get(serverId), scenarioApi.status(serverId)]);
        if (cancelled) return;
        // Only server-owned fields; the user's draft is never overwritten by polling.
        setServer((prev) => (prev && prev.id === latest.id ? { ...prev, ...latest } : prev));
        setActiveId(sim.active_scenario_id);
        setScenarios((list) => list.map((s) => (s.id === latest.id ? { ...s, status: latest.status } : s)));
        if (latest.status === "running" || latest.status === "starting") {
          const p = await scenarioApi.packets(serverId);
          if (!cancelled) setPackets(p);
        }
      } catch {
        /* transient; the next poll retries */
      }
      if (!cancelled) {
        const busy = statusRef.current && BUSY_STATUSES.includes(statusRef.current) && statusRef.current !== "running";
        timer = window.setTimeout(poll, busy ? 1000 : statusRef.current === "running" ? 2000 : 4000);
      }
    };
    poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [serverId]);

  // Links follow the scenario status: loaded when valid, dropped when invalidated.
  useEffect(() => {
    if (!serverId || !status) return;
    if (!LINK_STATUSES.has(status)) {
      setLinks(null);
      return;
    }
    if (links === null) scenarioApi.links(serverId).then(setLinks).catch(() => setLinks(null));
  }, [serverId, status, links]);

  // --- actions ------------------------------------------------------------------

  const confirmDiscard = () => !dirty || window.confirm("Masz niezapisane zmiany. Porzucić je?");

  const selectScenario = (id: string) =>
    confirmDiscard() &&
    run(async () => {
      setLinks(null);
      setPackets(null);
      setSelectedNode(0);
      setStep(0);
      if (!id) {
        setServer(null);
        setDraft(null);
        setSaved(null);
        return;
      }
      load(await scenarioApi.get(id));
    });

  const createScenario = () =>
    confirmDiscard() &&
    run(async () => {
      const created = await scenarioApi.create(defaultDraft(`Scenariusz ${scenarios.length + 1}`));
      setLinks(null);
      setPackets(null);
      setSelectedNode(0);
      setStep(0);
      load(created);
      await refreshList();
      ok("Utworzono scenariusz z trzema przykładowymi węzłami.");
    });

  const save = async () => {
    if (!server || !draft) return null;
    const result = await scenarioApi.save(server.id, draft);
    load(result);
    await refreshList();
    return result;
  };

  const saveScenario = () =>
    run(async () => {
      const hadLinks = links !== null;
      const result = await save();
      if (!result) return;
      if (hadLinks && result.status !== "links_ready") {
        setLinks(null);
        ok("Zapisano. Zmiany dotyczą modelu propagacji — oblicz łącza ponownie.");
      } else {
        ok("Zapisano zmiany.");
      }
    });

  const precompute = () =>
    run(async () => {
      if (!server) return;
      if (dirty) await save();
      setLinks(null);
      await scenarioApi.precompute(server.id);
      applyServer({ ...server, status: "precomputing", progress_done: 0, progress_total: 0 });
    });

  const start = () =>
    run(async () => {
      if (!server) return;
      await scenarioApi.start(server.id);
      setPackets(null);
      applyServer({ ...server, status: "starting", progress_done: 0, progress_total: server.nodes.length });
    });

  const stop = () =>
    run(async () => {
      if (!server) return;
      await scenarioApi.stop(server.id);
      applyServer(await scenarioApi.get(server.id));
      ok("Symulacja zatrzymana, kontenery węzłów usunięte.");
    });

  const remove = () =>
    server &&
    window.confirm(`Usunąć scenariusz „${server.name}”? Tej operacji nie można cofnąć.`) &&
    run(async () => {
      await scenarioApi.remove(server.id);
      setServer(null);
      setDraft(null);
      setSaved(null);
      setLinks(null);
      setPackets(null);
      await refreshList();
      ok("Scenariusz usunięty.");
    });

  const cleanup = () =>
    run(async () => {
      const result = await scenarioApi.cleanupAll();
      ok(result.containers_removed.length ? `Usunięto kontenery: ${result.containers_removed.length}.` : "Brak kontenerów do usunięcia.");
    });

  // --- view -----------------------------------------------------------------------

  const locked = status ? isBusy(status) : false;
  const done = [
    true,
    (draft?.nodes.length ?? 0) >= 2,
    links !== null,
    status === "running",
    (packets?.transmissions.length ?? 0) > 0,
  ];

  return (
    <div className="app">
      <header className="hero">
        <div>
          <span className="eyebrow">ŚRODOWISKO BADAWCZE</span>
          <h1>Meshtastic Lab</h1>
          <p>Zbuduj scenariusz, oblicz propagację i obserwuj routing prawdziwego firmware.</p>
        </div>
        <div className={`status-badge status-${status ?? "idle"}`}>
          <span className="status-dot" />
          {status ? STATUS_LABELS[status] : "Brak scenariusza"}
        </div>
      </header>

      <section className="scenario-bar">
        <div className="scenario-picker">
          <label htmlFor="scenario-select">Scenariusz</label>
          <select id="scenario-select" value={server?.id ?? ""} onChange={(e) => selectScenario(e.target.value)}>
            <option value="">Wybierz scenariusz…</option>
            {scenarios.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} — {STATUS_LABELS[s.status] ?? s.status}
              </option>
            ))}
          </select>
        </div>
        {server && (
          <>
            {dirty ? (
              <button className="button primary" onClick={saveScenario} disabled={working || locked || errors.length > 0}>
                Zapisz zmiany
              </button>
            ) : (
              <span className="saved-state">✓ Zapisano</span>
            )}
            {dirty && (
              <button className="button ghost" onClick={() => saved && setDraft(saved)} disabled={working}>
                Odrzuć zmiany
              </button>
            )}
          </>
        )}
        <button className="button" onClick={createScenario} disabled={working}>
          + Nowy scenariusz
        </button>
        {server && (
          <button className="button danger ghost" onClick={remove} disabled={working || locked}>
            Usuń
          </button>
        )}
      </section>

      {message && (
        <Callout kind={message.kind} onClose={() => setMessage(null)}>
          <pre>{message.text}</pre>
        </Callout>
      )}
      {dirty && errors.length > 0 && (
        <Callout kind="warning">
          Nie można zapisać: {errors.join(" ")}
        </Callout>
      )}
      {locked && status !== "running" && step < 2 && (
        <Callout kind="info">Trwa operacja w tle — edycja jest zablokowana do jej zakończenia.</Callout>
      )}
      {status === "running" && step < 2 && (
        <Callout kind="info">Symulacja działa — zatrzymaj ją, aby edytować scenariusz.</Callout>
      )}

      {!server || !draft ? (
        <section className="empty-state panel">
          <div className="empty-icon">⌁</div>
          <h2>Zacznij od scenariusza</h2>
          <p>Utwórz nowy eksperyment albo wybierz istniejący z listy powyżej.</p>
          <button className="button primary large" onClick={createScenario} disabled={working}>
            Utwórz scenariusz
          </button>
        </section>
      ) : (
        <>
          <nav className="wizard" aria-label="Etapy">
            {STEPS.map((s, index) => (
              <button
                key={s.label}
                className={[step === index ? "active" : "", done[index] ? "done" : ""].join(" ")}
                aria-current={step === index ? "step" : undefined}
                onClick={() => setStep(index)}
              >
                <span className="step-number">{done[index] && step !== index ? "✓" : index + 1}</span>
                <span>
                  <strong>{s.label}</strong>
                  <small>{s.hint}</small>
                </span>
              </button>
            ))}
          </nav>

          {step === 0 && (
            <EnvironmentStep draft={draft} presets={presets} disabled={locked} onChange={setDraft} onNext={() => setStep(1)} />
          )}
          {step === 1 && (
            <NodesStep
              draft={draft}
              selected={Math.min(selectedNode, Math.max(0, draft.nodes.length - 1))}
              disabled={locked}
              onSelect={setSelectedNode}
              onChange={setDraft}
              onNext={() => setStep(2)}
            />
          )}
          {step === 2 && (
            <LinksStep
              scenario={{ ...server, nodes: draft.nodes }}
              links={links}
              preset={presets[draft.environment.preset]}
              dirty={dirty}
              onPrecompute={precompute}
              onNext={() => setStep(3)}
            />
          )}
          {step === 3 && (
            <SimulationStep
              scenario={server}
              hasLinks={links !== null}
              dirty={dirty}
              otherActiveId={activeId && activeId !== server.id ? activeId : null}
              onStart={start}
              onStop={stop}
              onCleanup={cleanup}
              onGoToLinks={() => setStep(2)}
              onShowPackets={() => setStep(4)}
            />
          )}
          {step === 4 && <PacketsStep scenario={server} packets={packets} />}
        </>
      )}
    </div>
  );
}
