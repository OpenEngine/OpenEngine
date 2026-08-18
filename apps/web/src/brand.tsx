/** The parts every screen wears: the mark, the rail's head and foot, and the
 *  measured strip that runs under a page heading. */

import type { PropsWithChildren, ReactNode } from "react";

/** The openengine V, traced from the mark. Inherits `currentColor` so it can be
 *  laid on the flame tile in the rail and on anything else that needs it. */
export function Mark({ title }: { title?: string }) {
  return (
    <svg
      className="mark"
      viewBox="0 0 100 100"
      fill="currentColor"
      fillRule="evenodd"
      role={title ? "img" : "presentation"}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      <path d="M24.28 11.73 L25.1 11.94 L29.2 17.98 L3.79 35.4 L0.0 28.84 L24.28 11.83 Z M75.1 11.73 L99.9 28.74 L99.9 29.56 L96.11 35.4 L70.8 17.98 L75.1 11.83 Z M28.59 20.85 L49.49 53.02 L50.51 52.92 L71.21 20.85 L93.34 36.01 L73.05 66.24 L73.05 69.83 L59.84 88.27 L39.86 88.17 L26.84 69.83 L26.74 66.03 L6.66 36.32 L28.59 20.95 Z M26.02 29.66 L14.55 37.76 L16.6 40.83 L28.28 32.84 L26.13 29.66 Z M73.57 29.66 L71.62 32.84 L83.3 40.83 L85.35 37.76 L73.67 29.66 Z M31.05 37.35 L19.67 45.24 L21.62 48.41 L33.3 40.52 L31.15 37.35 Z M68.65 37.35 L66.7 40.52 L78.28 48.41 L80.23 45.24 L68.75 37.35 Z M36.07 45.03 L24.69 52.92 L26.74 56.1 L38.22 48.21 L36.17 45.03 Z M63.73 45.03 L61.68 48.21 L73.16 56.1 L75.2 52.92 L63.83 45.03 Z M41.16 70.51a8.82 8.82 0 1 0 17.64 0a8.82 8.82 0 1 0 -17.64 0Z" />
    </svg>
  );
}

export function RailBrand({ href = "/" }: { href?: string }) {
  return (
    <a className="rail-brand" href={href}>
      <span className="rail-mark">
        <Mark />
      </span>
      <span className="rail-wordmark">OpenEngine</span>
    </a>
  );
}

export function RailFoot() {
  return (
    <p className="rail-foot">
      <span className="rail-pip" aria-hidden="true" />
      Local openengine
    </p>
  );
}

/** A measured line under a heading: label above, figure below, one cell each.
 *
 *  Every cell is something the browser can actually count from what it was
 *  sent. Nothing here is estimated, so nothing here can be wrong. */
export function StatStrip({ children }: PropsWithChildren) {
  return <div className="stats">{children}</div>;
}

export function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: ReactNode;
  /** `alert` prints the figure in flame, for counts that want an eye on them. */
  tone?: "alert" | "live";
}) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value" data-tone={tone}>
        {value}
      </span>
    </div>
  );
}
