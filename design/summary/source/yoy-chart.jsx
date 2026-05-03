// Multi-year monthly chart. Compares the last N years for one metric.
// Two display modes: "grouped" (year bars per month) or "overlay" (line per year).

const { useState: useStateY, useMemo: useMemoY } = React;

function YearOverYearChart({
  activity,
  metric,        // 'count' | 'distance' | 'duration' | 'ascent' | 'descent'
  yearsToShow = 4,
  units,
  height = 140,
  mode = 'grouped',
}) {
  const a = ACTIVITY_DEFS[activity];
  const allYears = YEARS;
  const years = allYears.slice(-yearsToShow);
  const series = years.map(y => ({ year: y, data: HISTORY[activity][y][metric] }));

  // Find max across all visible years, ignoring nulls
  const max = Math.max(
    1,
    ...series.flatMap(s => s.data.map(v => v == null ? 0 : v))
  );

  const [hover, setHover] = useStateY(null); // [yearIdx, monthIdx]

  // Year color shading: most recent = full accent, older = less saturated
  const yearColor = (yi) => {
    const t = yi / Math.max(1, years.length - 1); // 0 = oldest, 1 = newest
    const opacity = 0.32 + t * 0.68;
    return { color: a.accent, opacity };
  };

  if (mode === 'overlay') {
    // Line chart: one line per year
    const w = 100; // percent-based viewbox
    const pad = 2;
    return (
      <div>
        <div style={{ position: 'relative', height: height + 4 }}>
          <svg width="100%" height={height} preserveAspectRatio="none" viewBox={`0 0 ${w} ${height}`}>
            {series.map((s, yi) => {
              const { color, opacity } = yearColor(yi);
              const isLatest = yi === series.length - 1;
              const points = s.data
                .map((v, mi) => {
                  if (v == null) return null;
                  const x = pad + (mi / 11) * (w - pad * 2);
                  const y = height - 4 - (v / max) * (height - 8);
                  return [x, y, mi, v];
                })
                .filter(Boolean);
              if (points.length === 0) return null;
              const path = points.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(' ');
              return (
                <g key={s.year}>
                  <path
                    d={path}
                    fill="none"
                    stroke={color}
                    strokeOpacity={opacity}
                    strokeWidth={isLatest ? 2 : 1.2}
                    vectorEffect="non-scaling-stroke"
                  />
                  {isLatest && points.map(p => (
                    <circle key={p[2]} cx={p[0]} cy={p[1]} r="1.6" fill={color} vectorEffect="non-scaling-stroke" />
                  ))}
                </g>
              );
            })}
          </svg>
        </div>
        <MonthAxis />
        <YearLegend years={years} accent={a.accent} />
      </div>
    );
  }

  // Grouped bars
  const groupGap = 3; // px between months
  const barGap = 1;   // px between year bars

  return (
    <div>
      <div style={{ position: 'relative' }}>
        <div style={{
          display: 'grid', gridTemplateColumns: 'repeat(12, 1fr)',
          gap: groupGap, height,
          alignItems: 'flex-end',
        }}>
          {Array.from({ length: 12 }).map((_, mi) => (
            <div key={mi} style={{ display: 'flex', gap: barGap, height: '100%', alignItems: 'flex-end' }}>
              {series.map((s, yi) => {
                const v = s.data[mi];
                const isLatest = yi === series.length - 1;
                const isHover = hover && hover[0] === yi && hover[1] === mi;
                const { color, opacity } = yearColor(yi);
                const h = v == null ? 0 : (v / max) * height;
                return (
                  <div key={s.year}
                    onMouseEnter={() => v != null && setHover([yi, mi])}
                    onMouseLeave={() => setHover(null)}
                    style={{
                      flex: 1,
                      height: v == null ? 1 : Math.max(1, h),
                      background: v == null
                        ? 'rgba(255,255,255,0.04)'
                        : color,
                      opacity: v == null ? 1 : (isHover ? 1 : opacity),
                      borderRadius: 1,
                      cursor: v != null ? 'crosshair' : 'default',
                      transition: 'opacity 80ms',
                      borderTop: isLatest && v != null && !isHover ? `1px solid ${color}` : 'none',
                    }} />
                );
              })}
            </div>
          ))}
        </div>
        {hover && (
          <div style={{
            position: 'absolute',
            top: -28,
            left: `${(hover[1] + 0.5) / 12 * 100}%`,
            transform: 'translateX(-50%)',
            background: '#15191a',
            border: '1px solid rgba(255,255,255,0.1)',
            color: '#fff',
            fontSize: 10, padding: '4px 8px',
            borderRadius: 3,
            fontFamily: 'var(--mono)',
            whiteSpace: 'nowrap',
            pointerEvents: 'none',
          }}>
            <span style={{ color: 'rgba(255,255,255,0.5)' }}>{MONTH_LABELS[hover[1]]} {series[hover[0]].year}</span>
            {' · '}
            <span>{fmtMetric(series[hover[0]].data[hover[1]], metric, units)}</span>
          </div>
        )}
      </div>
      <MonthAxis />
      <YearLegend years={years} accent={a.accent} />
    </div>
  );
}

function MonthAxis() {
  return (
    <div style={{
      display: 'grid', gridTemplateColumns: 'repeat(12, 1fr)', gap: 3,
      marginTop: 6,
      fontSize: 9, color: 'rgba(255,255,255,0.3)',
      fontFamily: 'var(--mono)',
    }}>
      {MONTH_LABELS.map((m, i) => (
        <div key={i} style={{ textAlign: 'center' }}>{m}</div>
      ))}
    </div>
  );
}

function YearLegend({ years, accent, size = 9 }) {
  return (
    <div style={{
      display: 'flex', gap: 12, marginTop: 8, justifyContent: 'flex-end',
      fontSize: size, fontFamily: 'var(--mono)',
    }}>
      {years.map((y, i) => {
        const t = i / Math.max(1, years.length - 1);
        const op = 0.32 + t * 0.68;
        const isLatest = i === years.length - 1;
        return (
          <span key={y} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            <span style={{
              width: 10, height: 2,
              background: accent, opacity: op,
              borderRadius: 1,
            }} />
            <span style={{ color: isLatest ? '#fff' : 'rgba(255,255,255,0.5)' }}>{y}</span>
          </span>
        );
      })}
    </div>
  );
}

window.YearOverYearChart = YearOverYearChart;
