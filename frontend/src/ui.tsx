import { ReactNode, useEffect, useId, useState } from "react";

type FieldProps = { label: string; hint?: string; children: (id: string) => ReactNode };

export function Field({ label, hint, children }: FieldProps) {
  const id = useId();
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {children(id)}
      {hint && <small className="hint">{hint}</small>}
    </div>
  );
}

type NumberFieldProps = {
  label: string;
  value: number | null;
  onChange: (value: number | null) => void;
  hint?: string;
  min?: number;
  max?: number;
  step?: number | string;
  /** Allow an empty field (value null), e.g. for an optional seed. */
  optional?: boolean;
  disabled?: boolean;
};

/** Number input that can be cleared while typing; commits only valid numbers. */
export function NumberField({ label, value, onChange, hint, min, max, step, optional, disabled }: NumberFieldProps) {
  const [text, setText] = useState(value === null ? "" : String(value));
  useEffect(() => {
    if (Number(text) !== value || (value === null && text !== "")) setText(value === null ? "" : String(value));
  }, [value]);

  const invalid =
    (text.trim() === "" && !optional) ||
    (text.trim() !== "" &&
      (!Number.isFinite(Number(text)) ||
        (min !== undefined && Number(text) < min) ||
        (max !== undefined && Number(text) > max)));

  return (
    <Field label={label} hint={invalid ? rangeHint(min, max) : hint}>
      {(id) => (
        <input
          id={id}
          type="number"
          className={invalid ? "invalid" : undefined}
          value={text}
          min={min}
          max={max}
          step={step}
          disabled={disabled}
          onChange={(e) => {
            const raw = e.target.value;
            setText(raw);
            if (raw.trim() === "") {
              if (optional) onChange(null);
              return;
            }
            const parsed = Number(raw);
            if (Number.isFinite(parsed) && (min === undefined || parsed >= min) && (max === undefined || parsed <= max)) {
              onChange(parsed);
            }
          }}
        />
      )}
    </Field>
  );
}

function rangeHint(min?: number, max?: number) {
  if (min !== undefined && max !== undefined) return `Podaj liczbę z zakresu ${min}–${max}.`;
  if (min !== undefined) return `Podaj liczbę ≥ ${min}.`;
  return "Podaj liczbę.";
}

type CalloutProps = { kind: "success" | "error" | "warning" | "info"; children: ReactNode; onClose?: () => void };

export function Callout({ kind, children, onClose }: CalloutProps) {
  const icon = { success: "✓", error: "!", warning: "!", info: "i" }[kind];
  return (
    <div className={`callout ${kind}`} role={kind === "error" ? "alert" : "status"}>
      <span>{icon}</span>
      <div className="callout-body">{children}</div>
      {onClose && (
        <button aria-label="Zamknij" onClick={onClose}>
          ×
        </button>
      )}
    </div>
  );
}

export function Progress({ done, total, unit }: { done: number; total: number; unit: string }) {
  if (total <= 0) return null;
  const percent = Math.round((done / total) * 100);
  return (
    <div className="progress-block">
      <div>
        <span>Postęp</span>
        <strong>{percent}%</strong>
      </div>
      <progress value={done} max={total} />
      <small>
        {done} z {total} {unit}
      </small>
    </div>
  );
}
