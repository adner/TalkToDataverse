"use client";

import type { CSSProperties } from "react";

import { bucketLabel, compactNumber, donutSlices, linePath, type ChartSpec } from "@/lib/chart";
import { tableLabel } from "@/lib/hold-state";

// Donut slices: the card's thread colour first, then colours that read well on the dark stage.
const SLICE_COLORS = ["var(--thread)", "#a78bfa", "#facc15", "#4ade80", "#fb923c", "#60a5fa"];

function caption(chart: ChartSpec, total: number): string {
  const what = chart.fn === "count" ? `${compactNumber(total)} records` : `${chart.fn} of ${(chart.measure ?? "").replace(/^cb_/, "")}`;
  const per = chart.grain ? ` · per ${chart.grain}` : "";
  const folded = chart.folded ? ` · ${chart.folded} folded` : "";
  return `${what}${per}${folded}`;
}

function Bars({ chart }: { chart: ChartSpec }) {
  const max = Math.max(0, ...chart.points.map((point) => point.value));
  return (
    <ol className="chart-bars">
      {chart.points.map((point, index) => (
        <li key={`${point.label}-${index}`} style={{ "--i": index } as CSSProperties}>
          <span className="chart-label" title={point.label}>{bucketLabel(point.label, chart.grain)}</span>
          <span className="chart-track">
            <i style={{ width: `${max > 0 ? Math.max(1.5, (point.value / max) * 100) : 0}%` }} />
          </span>
          <span className="chart-value">{compactNumber(point.value)}</span>
        </li>
      ))}
    </ol>
  );
}

function Donut({ chart, total }: { chart: ChartSpec; total: number }) {
  const slices = donutSlices(chart.points);
  // r = 15.915 makes the circumference 100, so dash lengths are percentages.
  return (
    <div className="chart-donut">
      <svg viewBox="0 0 42 42" role="img" aria-label={chart.title}>
        <circle className="donut-hole" cx="21" cy="21" r="15.915" />
        {slices.map((slice, index) =>
          slice.length > 0 ? (
            <circle
              className="donut-slice"
              cx="21"
              cy="21"
              key={`${slice.label}-${index}`}
              r="15.915"
              stroke={SLICE_COLORS[index % SLICE_COLORS.length]}
              strokeDasharray={`${slice.length * 100} ${100 - slice.length * 100}`}
              strokeDashoffset={25 - slice.start * 100}
              style={{ "--i": index } as CSSProperties}
            />
          ) : null,
        )}
        <text className="donut-total" x="21" y="22.6" textAnchor="middle">
          {compactNumber(total)}
        </text>
      </svg>
      <ul className="chart-legend">
        {slices.map((slice, index) => (
          <li key={`${slice.label}-${index}`}>
            <i style={{ background: SLICE_COLORS[index % SLICE_COLORS.length] }} />
            <span className="chart-label" title={slice.label}>{slice.label}</span>
            <span className="chart-value">{compactNumber(slice.value)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Line({ chart }: { chart: ChartSpec }) {
  const xy = linePath(chart.points);
  const path = xy.map(([x, y], index) => `${index ? "L" : "M"}${x},${y}`).join(" ");
  const max = Math.max(0, ...chart.points.map((point) => point.value));
  // Label at most six buckets, always the first and the last.
  const every = Math.max(1, Math.ceil(chart.points.length / 6));
  return (
    <div className="chart-line">
      <span className="chart-max">{compactNumber(max)}</span>
      {/* The SVG stretches to the box, so the stroke keeps its width and the dots are HTML. */}
      <div className="line-plot">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={chart.title}>
          <line className="line-axis" x1="0" x2="100" y1="100" y2="100" />
          <path className="line-area" d={`${path} L${xy.at(-1)?.[0] ?? 0},100 L${xy[0]?.[0] ?? 0},100 Z`} />
          <path className="line-path" d={path} />
        </svg>
        {xy.map(([x, y], index) => (
          <i
            className="line-dot"
            key={index}
            style={{ left: `${x}%`, top: `${y}%`, "--i": index } as CSSProperties}
            title={`${bucketLabel(chart.points[index].label, chart.grain)}: ${compactNumber(chart.points[index].value)}`}
          />
        ))}
      </div>
      <div className="line-ticks">
        {chart.points.map((point, index) => (
          <span
            key={`${point.label}-${index}`}
            style={{ left: `${xy[index]?.[0] ?? 0}%` }}
            data-shown={index % every === 0 || index === chart.points.length - 1 ? "true" : "false"}
          >
            {/* The year only on the first month and on January keeps the ticks apart. */}
            {bucketLabel(point.label, chart.grain, index === 0 || point.label.endsWith("-01"))}
          </span>
        ))}
      </div>
    </div>
  );
}

export function FlightChart({ chart }: { chart: ChartSpec }) {
  const total = chart.points.reduce((sum, point) => sum + point.value, 0);
  return (
    <figure className="flight-chart" data-type={chart.type}>
      <figcaption>
        <strong>{chart.title}</strong>
        <small>
          {chart.table ? `${tableLabel(chart.table)} · ` : ""}
          {caption(chart, total)}
        </small>
      </figcaption>
      {chart.points.length === 0 ? (
        <p className="chart-empty">No records to chart.</p>
      ) : chart.type === "donut" ? (
        <Donut chart={chart} total={total} />
      ) : chart.type === "line" ? (
        <Line chart={chart} />
      ) : (
        <Bars chart={chart} />
      )}
    </figure>
  );
}
