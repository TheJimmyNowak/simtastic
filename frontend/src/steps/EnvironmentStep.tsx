import type { Environment, LoraPreset, ScenarioDraft } from "../api";
import { Field, NumberField } from "../ui";

type Props = {
  draft: ScenarioDraft;
  presets: Record<string, LoraPreset>;
  disabled: boolean;
  onChange: (draft: ScenarioDraft) => void;
  onNext: () => void;
};

export function EnvironmentStep({ draft, presets, disabled, onChange, onNext }: Props) {
  const env = draft.environment;
  const setEnv = (patch: Partial<Environment>) => onChange({ ...draft, environment: { ...env, ...patch } });
  const preset = presets[env.preset];

  return (
    <div className="panel grid2">
      <fieldset disabled={disabled}>
        <Field label="Nazwa scenariusza">
          {(id) => (
            <input
              id={id}
              value={draft.name}
              maxLength={120}
              className={draft.name.trim() ? undefined : "invalid"}
              onChange={(e) => onChange({ ...draft, name: e.target.value })}
            />
          )}
        </Field>
        <Field label="Identyfikator eksperymentu" hint="Tag w InfluxDB/Grafanie — nadaj inny dla każdej serii pomiarów.">
          {(id) => (
            <input
              id={id}
              value={env.experiment_id}
              className={env.experiment_id.trim() ? undefined : "invalid"}
              onChange={(e) => setEnv({ experiment_id: e.target.value })}
            />
          )}
        </Field>
        <Field label="Preset LoRa">
          {(id) => (
            <select id={id} value={env.preset} onChange={(e) => setEnv({ preset: e.target.value })}>
              {Object.keys(presets).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          )}
        </Field>
        <div className="form-grid">
          <NumberField
            label="Fading σ [dB]"
            value={env.fading_sigma_db}
            min={0}
            step={0.5}
            hint="Losowa zmienność sygnału na każdą transmisję."
            onChange={(v) => setEnv({ fading_sigma_db: v ?? 0 })}
          />
          <NumberField
            label="Seed fadingu"
            value={env.random_seed}
            optional
            step={1}
            hint="Puste = losowy przy każdym uruchomieniu."
            onChange={(v) => setEnv({ random_seed: v })}
          />
          <NumberField
            label="Próg sąsiedztwa [dBm]"
            value={env.connectivity_threshold_dbm}
            step={1}
            hint="Pary o medianowym RSSI poniżej progu są pomijane."
            onChange={(v) => setEnv({ connectivity_threshold_dbm: v ?? -140 })}
          />
          <NumberField
            label="Wymagany prześwit Fresnela"
            value={env.fresnel_clearance_ratio}
            min={0}
            max={1}
            step={0.05}
            hint="Część 1. strefy Fresnela, która musi być wolna."
            onChange={(v) => setEnv({ fresnel_clearance_ratio: v ?? 0.6 })}
          />
          <NumberField
            label="Strata otoczenia [dB]"
            value={env.clutter_loss_db}
            min={0}
            step={1}
            hint="Doliczana do każdego łącza: zabudowa i roślinność spoza SRTM, anteny w budynkach."
            onChange={(v) => setEnv({ clutter_loss_db: v ?? 0 })}
          />
          <NumberField
            label="Cieniowanie łączy σ [dB]"
            value={env.link_shadowing_sigma_db}
            min={0}
            step={1}
            hint="Trwała różnica między łączami, losowana raz na parę węzłów przy obliczaniu łączy."
            onChange={(v) => setEnv({ link_shadowing_sigma_db: v ?? 0 })}
          />
          <NumberField
            label="Seed cieniowania"
            value={env.link_shadowing_seed}
            optional
            step={1}
            hint="Puste = nowe losowanie przy każdym obliczeniu łączy."
            onChange={(v) => setEnv({ link_shadowing_seed: v })}
          />
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={env.building_model_enabled}
            onChange={(e) => setEnv({ building_model_enabled: e.target.checked })}
          />
          Uwzględnij budynki LoD1 (dyfrakcja wg ITU-R P.526)
        </label>
        <div className="form-actions">
          <button type="button" className="button" onClick={onNext}>
            Dalej: węzły →
          </button>
        </div>
      </fieldset>
      <aside className="info-card">
        <h3>Preset {env.preset}</h3>
        {preset ? (
          <dl className="facts">
            <dt>Częstotliwość</dt>
            <dd>{preset.frequency_mhz} MHz</dd>
            <dt>Pasmo / SF / CR</dt>
            <dd>
              {preset.bandwidth_khz} kHz / SF{preset.spreading_factor} / 4/{preset.coding_rate}
            </dd>
            <dt>Moc nadajnika</dt>
            <dd>{preset.tx_power_dbm} dBm</dd>
            <dt>Czułość odbiornika</dt>
            <dd>{preset.sensitivity_dbm} dBm</dd>
            <dt>Szum odbiornika</dt>
            <dd>{preset.noise_floor_dbm} dBm</dd>
          </dl>
        ) : (
          <p>Brak opisu presetu.</p>
        )}
        <p>
          Ten sam preset i region EU_868 są ustawiane w firmware każdego węzła, więc czas nadawania w symulacji zgadza
          się z modelem radiowym.
        </p>
        <p>
          Zmiana presetu, modelu budynków, prześwitu Fresnela, straty otoczenia, cieniowania łączy lub progu sąsiedztwa wymaga ponownego obliczenia łączy.
          Identyfikator eksperymentu, fading i seed można zmieniać bez przeliczania.
        </p>
      </aside>
    </div>
  );
}
