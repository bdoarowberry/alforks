// Shared UI atoms used across all variations.
// All components are exported to window.

const { useState, useMemo, useRef, useEffect } = React;

// ---------- Activity selector (segmented control) ----------
function ActivitySelector({ value, onChange, includeAll = true, style = 'pill', dense = false }) {
  const items = [
    ...(includeAll ? [{ id: 'all', label: 'All', short: 'ALL', accent: 'oklch(0.92 0 0)' }] : []),
    ...ACTIVITY_ORDER.map(k => ACTIVITY_DEFS[k]),
  ];
  if (style === 'tabs') {
    return (
      <div style={{ display: 'flex', gap: 0, borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
        {items.map(it => {
          const active = value === it.id;
          return (
            <button
              key={it.id}
              onClick={() => onChange(it.id)}
              style={{
                background: 'transparent',
                color: active ? '#fff' : 'rgba(255,255,255,0.55)',
                border: 'none',
                borderBottom: active ? `2px solid ${it.accent}` : '2px solid transparent',
                padding: dense ? '8px 14px' : '12px 18px',
                fontSize: dense ? 12 : 13,
                fontWeight: 500,
                letterSpacing: '0.04em',
                textTransform: 'uppercase',
                cursor: 'pointer',
                fontFamily: 'inherit',
                marginBottom: -1,
              }}
            >
              {it.label}
            </button>
          );
        })}
      </div>
    );
  }
  // pill
  return (
    <div style={{
      display: 'inline-flex', gap: 4, padding: 4,
      background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 999,
    }}>
      {items.map(it => {
        const active = value === it.id;
        return (
          <button
            key={it.id}
            onClick={() => onChange(it.id)}
            style={{
              background: active ? (it.accent || '#fff') : 'transparent',
              color: active ? '#0b0d0c' : 'rgba(255,255,255,0.7)',
              border: 'none',
              padding: dense ? '5px 12px' : '7px 16px',
              fontSize: dense ? 11 : 12,
              fontWeight: 600,
              letterSpacing: '0.04em',
              textTransform: 'uppercase',
              cursor: 'pointer',
              borderRadius: 999,
              transition: 'all 120ms',
              fontFamily: 'inherit',
            }}
          >
            {it.short || it.label}
          </button>
        );
      })}
    </div>
  );
}

// ---------- Range selector ----------
function RangeSelector({ value, onChange, options = ['week', 'month', 'year', 'all'], dense = false }) {
  return (
    <div style={{
      display: 'inline-flex', gap: 0,
      background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 6,
      overflow: 'hidden',
    }}>
      {options.map((opt, i) => {
        const active = value === opt;
        return (
          <button
            key={opt}
            onClick={() => onChange(opt)}
            style={{
              background: active ? 'rgba(255,255,255,0.08)' : 'transparent',
              color: active ? '#fff' : 'rgba(255,255,255,0.5)',
              border: 'none',
              borderLeft: i > 0 ? '1px solid rgba(255,255,255,0.06)' : 'none',
              padding: dense ? '5px 10px' : '7px 14px',
              fontSize: dense ? 10 : 11,
              fontWeight: 500,
              letterSpacing: '0.06em',
              textTransform: 'uppercase',
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            {opt}
          </button>
        );
      })}
    </div>
  );
}

// ---------- Stat (number + label) ----------
function Stat({ label, value, unit, sub, accent, size = 'md', mono = true }) {
  const sizes = {
    sm: { val: 22, lbl: 10, sub: 10 },
    md: { val: 32, lbl: 11, sub: 11 },
    lg: { val: 48, lbl: 12, sub: 12 },
    xl: { val: 72, lbl: 13, sub: 12 },
  };
  const s = sizes[size];
  return (
    <div>
      <div style={{
        fontSize: s.lbl, color: 'rgba(255,255,255,0.45)',
        letterSpacing: '0.1em', textTransform: 'uppercase',
        fontWeight: 500, marginBottom: 6,
      }}>{label}</div>
      <div style={{
        fontSize: s.val, fontWeight: 500, color: '#fff',
        lineHeight: 1, letterSpacing: '-0.02em',
        fontFamily: mono ? 'var(--mono)' : 'inherit',
        fontVariantNumeric: 'tabular-nums',
      }}>
        {value}
        {unit && (
          <span style={{
            fontSize: Math.round(s.val * 0.42), color: 'rgba(255,255,255,0.45)',
            marginLeft: 4, fontWeight: 400,
          }}>{unit}</span>
        )}
      </div>
      {sub && (
        <div style={{
          fontSize: s.sub, color: accent || 'rgba(255,255,255,0.55)',
          marginTop: 6, fontFamily: 'var(--mono)',
        }}>{sub}</div>
      )}
    </div>
  );
}

// ---------- Sparkline / bar chart ----------
function MiniBars({ data, accent, height = 60, gap = 2, hoverable = true, labels = null, formatHover = null }) {
  const [hover, setHover] = useState(null);
  const max = Math.max(...data, 1);
  return (
    <div style={{ position: 'relative' }}>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap, height }}>
        {data.map((v, i) => {
          const h = max > 0 ? (v / max) * height : 0;
          const isHover = hover === i;
          return (
            <div
              key={i}
              onMouseEnter={hoverable ? () => setHover(i) : undefined}
              onMouseLeave={hoverable ? () => setHover(null) : undefined}
              style={{
                flex: 1,
                height: Math.max(h, v > 0 ? 2 : 1),
                background: v === 0 ? 'rgba(255,255,255,0.05)' : (isHover ? '#fff' : accent),
                opacity: hover === null || isHover ? 1 : 0.55,
                borderRadius: 1,
                transition: 'opacity 80ms, background 80ms',
                cursor: hoverable ? 'crosshair' : 'default',
              }}
            />
          );
        })}
      </div>
      {hover !== null && labels && (
        <div style={{
          position: 'absolute', top: -32,
          left: `${(hover + 0.5) / data.length * 100}%`,
          transform: 'translateX(-50%)',
          background: '#15191a', color: '#fff',
          fontSize: 10, padding: '4px 8px',
          borderRadius: 4, whiteSpace: 'nowrap',
          fontFamily: 'var(--mono)',
          border: '1px solid rgba(255,255,255,0.1)',
          pointerEvents: 'none',
        }}>
          <span style={{ color: 'rgba(255,255,255,0.5)' }}>{labels[hover]}</span>
          {' · '}
          <span>{formatHover ? formatHover(data[hover]) : data[hover]}</span>
        </div>
      )}
    </div>
  );
}

// ---------- Line chart ----------
function LineChart({ data, accent, height = 100, hoverable = true, labels = null, formatHover = null }) {
  const ref = useRef(null);
  const [hover, setHover] = useState(null);
  const [width, setWidth] = useState(400);

  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(entries => {
      for (const e of entries) setWidth(e.contentRect.width);
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const pad = 4;
  const points = data.map((v, i) => {
    const x = (i / (data.length - 1)) * (width - pad * 2) + pad;
    const y = height - pad - ((v - min) / (max - min || 1)) * (height - pad * 2);
    return [x, y];
  });
  const path = points.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(' ');
  const areaPath = `${path} L${points[points.length-1][0]},${height} L${points[0][0]},${height} Z`;

  function handleMove(e) {
    if (!hoverable) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const idx = Math.max(0, Math.min(data.length - 1, Math.round((x - pad) / (width - pad * 2) * (data.length - 1))));
    setHover(idx);
  }

  return (
    <div ref={ref} style={{ position: 'relative', width: '100%' }}>
      <svg
        width={width} height={height}
        onMouseMove={handleMove}
        onMouseLeave={() => setHover(null)}
        style={{ display: 'block', cursor: hoverable ? 'crosshair' : 'default' }}
      >
        <defs>
          <linearGradient id={`grad-${accent.replace(/[^a-z0-9]/gi,'')}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={accent} stopOpacity="0.3" />
            <stop offset="100%" stopColor={accent} stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={areaPath} fill={`url(#grad-${accent.replace(/[^a-z0-9]/gi,'')})`} />
        <path d={path} fill="none" stroke={accent} strokeWidth="1.5" strokeLinejoin="round" />
        {hover !== null && (
          <>
            <line x1={points[hover][0]} x2={points[hover][0]} y1={0} y2={height}
              stroke="rgba(255,255,255,0.2)" strokeWidth="1" strokeDasharray="2,2" />
            <circle cx={points[hover][0]} cy={points[hover][1]} r="4" fill="#fff" />
          </>
        )}
      </svg>
      {hover !== null && labels && (
        <div style={{
          position: 'absolute', top: -28,
          left: points[hover][0],
          transform: 'translateX(-50%)',
          background: '#15191a', color: '#fff',
          fontSize: 10, padding: '4px 8px',
          borderRadius: 4, whiteSpace: 'nowrap',
          fontFamily: 'var(--mono)',
          border: '1px solid rgba(255,255,255,0.1)',
          pointerEvents: 'none',
        }}>
          <span style={{ color: 'rgba(255,255,255,0.5)' }}>{labels[hover]}</span>
          {' · '}
          <span>{formatHover ? formatHover(data[hover]) : data[hover]}</span>
        </div>
      )}
    </div>
  );
}

// ---------- Donut / activity-share chart ----------
function StackedBar({ segments, height = 8, rounded = true }) {
  const total = segments.reduce((s, x) => s + x.value, 0);
  return (
    <div style={{
      display: 'flex', height, width: '100%',
      borderRadius: rounded ? height / 2 : 0,
      overflow: 'hidden', gap: 1,
      background: 'rgba(255,255,255,0.04)',
    }}>
      {segments.map((seg, i) => (
        <div key={i} style={{
          width: `${(seg.value / total) * 100}%`,
          background: seg.color,
        }} />
      ))}
    </div>
  );
}

// ---------- Week heatmap (26 weeks × 1 row, or grid) ----------
function WeekHeatmap({ data, accent, weeks = 26, label = 'WEEKLY ACTIVITY' }) {
  return (
    <div>
      <div style={{
        fontSize: 10, color: 'rgba(255,255,255,0.4)',
        letterSpacing: '0.1em', textTransform: 'uppercase',
        marginBottom: 8, display: 'flex', justifyContent: 'space-between',
      }}>
        <span>{label}</span>
        <span style={{ fontFamily: 'var(--mono)' }}>26W</span>
      </div>
      <div style={{ display: 'flex', gap: 2 }}>
        {data.map((v, i) => {
          const intensity = v === 0 ? 0 : 0.25 + Math.min(v / 4, 1) * 0.75;
          return (
            <div key={i}
              title={`Week ${weeks - i} ago: ${v} activities`}
              style={{
                flex: 1, height: 28,
                background: v === 0 ? 'rgba(255,255,255,0.04)' : accent,
                opacity: v === 0 ? 1 : intensity,
                borderRadius: 2,
              }}
            />
          );
        })}
      </div>
    </div>
  );
}

// ---------- Card wrapper ----------
function Card({ children, style, padding = 24, accent }) {
  return (
    <div style={{
      background: '#0f1313',
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 4,
      padding,
      position: 'relative',
      ...style,
    }}>
      {accent && (
        <div style={{
          position: 'absolute', top: 0, left: 0, width: 2, height: '100%',
          background: accent,
        }} />
      )}
      {children}
    </div>
  );
}

// ---------- Activity glyph chip ----------
function ActivityChip({ type, size = 24 }) {
  const def = ACTIVITY_DEFS[type];
  if (!def) return null;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
      width: size, height: size,
      background: def.accentSoft,
      color: def.accent,
      borderRadius: 4,
      fontSize: size * 0.5,
      fontWeight: 700,
      fontFamily: 'var(--mono)',
      letterSpacing: 0,
    }}>{def.glyph}</span>
  );
}

// ---------- Section label (small caps, divider) ----------
function SectionLabel({ children, right }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      fontSize: 10, color: 'rgba(255,255,255,0.4)',
      letterSpacing: '0.14em', textTransform: 'uppercase',
      fontWeight: 500, marginBottom: 16,
    }}>
      <span>{children}</span>
      {right}
    </div>
  );
}

Object.assign(window, {
  ActivitySelector, RangeSelector, Stat, MiniBars, LineChart, StackedBar,
  WeekHeatmap, Card, ActivityChip, SectionLabel,
});
