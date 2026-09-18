import { statusTone } from "../lib/format";

interface StatusPillProps {
  value: string;
  label?: string;
}

export function StatusPill({ value, label }: StatusPillProps) {
  const tone = statusTone(value);
  return (
    <span className={`status-pill status-pill--${tone}`}>
      <span className="status-pill__dot" aria-hidden="true" />
      {label ?? value}
    </span>
  );
}
