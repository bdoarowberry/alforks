// Combined view: B's All-Activities header + A's compact per-activity rows.
// Each activity row is collapsible and shows a multi-year monthly chart with
// toggleable metric (count / distance / duration / ascent or descent).

const { useState: useStateV, useMemo: useMemoV } = React;

function ActiveDaysRibbon({ active, dominant, daysBack = 365, height = 36 }) {
  const today = new Date(TODAY + 'T00:00:00');
  const cells = [];
  for (let i = daysBack - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(d.getDate() - i);
    const iso = d.toISOString().slice(0, 10);
    cells.push({ iso, on: active.has(iso), type: dominant.get(iso) || null, dow: d.getDay() });
  }
  const [hover, setHover] = useStateV(null);

  return (
    <div>
      <div style={{ display: 'flex', gap: 1, height, alignItems: 'stretch' }}>
        {cells.map((c, i) => {
          const a = c.type ? ACTIVITY_DEFS[c.type] : null;
          const isHover = hover === i;
          return (
            <div key={c.iso}
              onMouseEnter={() => c.on && setHover(i)}
              onMouseLeave={() => setHover(null)}
              style={{
                flex: 1,
                background: c.on ? a.accent : 'rgba(255,255,255,0.04)',
                borderRadius: 1,
                opacity: c.on ? 1 : 1,
                outline: isHover ? '1px solid #fff' : 'none',
                cursor: c.on ? 'pointer' : 'default',
              }} />
          );
        })}
      </div>
      <div style={{
        marginTop: 8, display: 'flex', justifyContent: 'space-between',
        fontSize: 10, color: 'rgba(255,255,255,0.4)', fontFamily: 'var(--mono)', minHeight: 14,
      }}>
        {hover != null && cells[hover].on ? (
          <>
            <span>{cells[hover].iso}</span>
            <span style={{ color: ACTIVITY_DEFS[cells[hover].type].accent }}>
              {ACTIVITY_DEFS[cells[hover].type].label}
            </span>
          </>
        ) : (
          <>
            <span>{daysBack} days back</span>
            <span>today →</span>
          </>
        )}
      </div>
    </div>
  );
}

function MetricToggle({ options, value, onChange, accent }) {
  return (
    <div style={{
      display: 'inline-flex',
      background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 4, overflow: 'hidden',
    }}>
      {options.map((opt, i) => {
        const active = value === opt.key;
        return (
          <button key={opt.key}
            onClick={() => onChange(opt.key)}
            style={{
              all: 'unset',
              cursor: 'pointer',
              padding: '5px 12px',
              fontSize: 10, letterSpacing: '0.06em', textTransform: 'uppercase',
              fontFamily: 'var(--mono)',
              background: active ? accent : 'transparent',
              color: active ? '#0b0d0c' : 'rgba(255,255,255,0.55)',
              borderLeft: i > 0 ? '1px solid rgba(255,255,255,0.06)' : 'none',
              fontWeight: active ? 600 : 400,
            }}>
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function ChartModeToggle({ value, onChange }) {
  return (
    <div style={{
      display: 'inline-flex',
      background: 'rgba(255,255,255,0.04)',
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 4, overflow: 'hidden',
    }}>
      {[
        { key: 'grouped', label: '▮▮' },
        { key: 'overlay', label: '⌇' },
      ].map((opt, i) => {
        const active = value === opt.key;
        return (
          <button key={opt.key}
            onClick={() => onChange(opt.key)}
            title={opt.key}
            style={{
              all: 'unset', cursor: 'pointer',
              padding: '5px 10px', fontSize: 11,
              fontFamily: 'var(--mono)',
              background: active ? 'rgba(255,255,255,0.1)' : 'transparent',
              color: active ? '#fff' : 'rgba(255,255,255,0.4)',
              borderLeft: i > 0 ? '1px solid rgba(255,255,255,0.06)' : 'none',
            }}>
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function ActivityRow({ k, units, expanded, onToggle, defaultMetric = 'count', defaultMode = 'grouped' }) {
  const a = ACTIVITY_DEFS[k];
  const r = ROLLUPS[k];
  const recent = RECENT.filter(x => x.type === k);
  const [metric, setMetric] = useStateV(defaultMetric);
  const [mode, setMode] = useStateV(defaultMode);
  const lastDate = r.last_date;
  const daysSince = lastDate ? daysBetween(lastDate, TODAY) : null;

  // Activity-aware headline stats
  const headlineMap = {
    days:     { label: 'days',     value: r.days, unit: null },
    distance: { label: 'distance', value: fmtDist(r.distance_km, units),    unit: distUnit(units) },
    ascent:   { label: 'ascent',   value: fmtElev(r.elev_gain_m, units),    unit: elevUnit(units) },
    descent:  { label: 'descent',  value: fmtElev(r.elev_loss_m, units),    unit: elevUnit(units) },
    moving:   { label: 'moving',   value: fmtHours(r.moving_h),             unit: null },
  };
  const headline = a.metrics.map(m => headlineMap[m]);

  // YoY mini-summary: this year vs last year for the selected metric (cumulative, same months elapsed)
  const yoy = useMemoV(() => {
    const thisYear = HISTORY[k][2026][metric];
    const lastYear = HISTORY[k][2025][metric];
    const monthsElapsed = thisYear.filter(v => v != null).length;
    const sumThis = thisYear.slice(0, monthsElapsed).reduce((s,v)=>s+(v||0), 0);
    const sumLast = lastYear.slice(0, monthsElapsed).reduce((s,v)=>s+(v||0), 0);
    if (sumLast === 0) return null;
    const pct = ((sumThis - sumLast) / sumLast) * 100;
    return { sumThis, sumLast, pct, monthsElapsed };
  }, [k, metric]);

  return (
    <div style={{
      borderTop: '1px solid rgba(255,255,255,0.08)',
      borderLeft: `2px solid ${a.accent}`,
      background: '#0a0c0c',
    }}>
      {/* Collapsed header */}
      <button onClick={onToggle} style={{
        all: 'unset',
        cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 18,
        padding: '14px 18px', width: '100%', boxSizing: 'border-box',
      }}>
        <span style={{
          width: 28, height: 28, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          background: a.accentSoft, color: a.accent,
          borderRadius: 4, fontFamily: 'var(--mono)', fontWeight: 700, fontSize: 13,
        }}>{a.glyph}</span>
        <span style={{ fontSize: 14, color: '#fff', fontWeight: 500 }}>{a.label}</span>
        <span style={{ flex: 1 }} />

        {/* Activity-aware compact stats in header */}
        {headline.map((s, i) => (
          <span key={i} style={{
            fontSize: 11, color: 'rgba(255,255,255,0.6)',
            fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums',
            minWidth: 80, textAlign: 'right',
          }}>
            <span style={{ color: '#fff' }}>{s.value}</span>
            {s.unit && <span style={{ color: 'rgba(255,255,255,0.4)' }}> {s.unit}</span>}
            <span style={{ color: 'rgba(255,255,255,0.3)', marginLeft: 6, fontSize: 9, letterSpacing: '0.1em', textTransform: 'uppercase' }}>{s.label}</span>
          </span>
        ))}

        {lastDate && (
          <span style={{
            fontSize: 10, color: 'rgba(255,255,255,0.45)',
            fontFamily: 'var(--mono)', minWidth: 60, textAlign: 'right',
          }}>
            {daysSince}d ago
          </span>
        )}
        <span style={{
          color: 'rgba(255,255,255,0.4)', fontSize: 16, fontFamily: 'var(--mono)',
          width: 16, textAlign: 'center',
          transform: expanded ? 'rotate(90deg)' : 'none',
          transition: 'transform 120ms',
        }}>›</span>
      </button>

      {expanded && (
        <div style={{ padding: '4px 18px 22px' }}>
          {/* Chart toolbar */}
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            marginBottom: 14, gap: 12, flexWrap: 'wrap',
          }}>
            <div style={{
              fontSize: 10, color: 'rgba(255,255,255,0.4)',
              letterSpacing: '0.14em', textTransform: 'uppercase',
            }}>
              {a.chartMetrics.find(m => m.key === metric).label} · year over year
              {yoy && (
                <span style={{
                  marginLeft: 12, color: yoy.pct >= 0 ? 'oklch(0.78 0.15 145)' : 'oklch(0.74 0.14 55)',
                  fontFamily: 'var(--mono)', letterSpacing: 0,
                }}>
                  {yoy.pct >= 0 ? '↑' : '↓'} {Math.abs(yoy.pct).toFixed(0)}% vs '25 ({yoy.monthsElapsed}mo)
                </span>
              )}
            </div>
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <MetricToggle options={a.chartMetrics} value={metric} onChange={setMetric} accent={a.accent} />
              <ChartModeToggle value={mode} onChange={setMode} />
            </div>
          </div>

          <YearOverYearChart activity={k} metric={metric} units={units} mode={mode} height={140} yearsToShow={4} />

          {/* Body: detail stats + recent log + PRs */}
          <div style={{
            display: 'grid', gridTemplateColumns: '1fr 1.4fr 1fr', gap: 28,
            marginTop: 24,
          }}>
            <DetailStats activity={k} units={units} />
            <RecentLog activity={k} units={units} />
            <PRList activity={k} accent={a.accent} />
          </div>
        </div>
      )}
    </div>
  );
}

function DetailStats({ activity, units }) {
  const r = ROLLUPS[activity];
  const a = ACTIVITY_DEFS[activity];
  // Activity-aware ordering of "performance" detail rows
  const rows = activity === 'snow'
    ? [
        ['avg speed',   `${fmtSpeed(r.avg_speed_kmh, units)} ${speedUnit(units)}`],
        ['top speed',   `${fmtSpeed(r.max_speed_kmh, units)} ${speedUnit(units)}`],
        ['avg HR',      `${r.avg_hr} bpm`],
        ['max HR',      `${r.max_hr} bpm`],
        ['streak',      `${r.current_streak}d / ${r.longest_streak}d`],
      ]
    : [
        ['avg speed',   `${fmtSpeed(r.avg_speed_kmh, units)} ${speedUnit(units)}`],
        ['top speed',   `${fmtSpeed(r.max_speed_kmh, units)} ${speedUnit(units)}`],
        ['avg HR',      `${r.avg_hr} bpm`],
        ['max HR',      `${r.max_hr} bpm`],
        ['avg power',   r.avg_power_w ? `${r.avg_power_w} w` : '—'],
        ['streak',      `${r.current_streak}d / ${r.longest_streak}d`],
      ];
  return (
    <div>
      <div style={{
        fontSize: 10, color: 'rgba(255,255,255,0.4)',
        letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 12,
      }}>performance</div>
      {rows.map((row, i) => (
        <div key={i} style={{
          display: 'flex', justifyContent: 'space-between',
          padding: '6px 0', fontSize: 12,
          borderBottom: i < rows.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none',
        }}>
          <span style={{ color: 'rgba(255,255,255,0.55)' }}>{row[0]}</span>
          <span style={{ color: '#fff', fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums' }}>{row[1]}</span>
        </div>
      ))}
    </div>
  );
}

function RecentLog({ activity, units }) {
  const a = ACTIVITY_DEFS[activity];
  const recent = RECENT.filter(x => x.type === activity).slice(0, 5);
  const showElev = activity !== 'snow';
  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        fontSize: 10, color: 'rgba(255,255,255,0.4)',
        letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 12,
      }}>
        <span>recent</span>
        <span style={{ color: 'rgba(255,255,255,0.3)', fontFamily: 'var(--mono)' }}>{recent.length}</span>
      </div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: showElev ? '60px 1fr 60px 50px 50px' : '60px 1fr 60px 60px 50px',
        gap: 8, fontSize: 9, color: 'rgba(255,255,255,0.3)',
        letterSpacing: '0.08em', textTransform: 'uppercase',
        paddingBottom: 6, borderBottom: '1px solid rgba(255,255,255,0.06)',
        fontFamily: 'var(--mono)',
      }}>
        <span>date</span><span>name</span>
        <span style={{textAlign:'right'}}>dist</span>
        <span style={{textAlign:'right'}}>{showElev ? '↑' : '↓'}</span>
        <span style={{textAlign:'right'}}>dur</span>
      </div>
      {recent.map(x => (
        <div key={x.id} style={{
          display: 'grid',
          gridTemplateColumns: showElev ? '60px 1fr 60px 50px 50px' : '60px 1fr 60px 60px 50px',
          gap: 8, padding: '6px 0', fontSize: 11,
          fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums',
          borderBottom: '1px dotted rgba(255,255,255,0.05)',
          cursor: 'pointer',
        }}>
          <span style={{ color: 'rgba(255,255,255,0.5)' }}>{fmtDate(x.date)}</span>
          <span style={{ color: '#fff', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: 'var(--sans)', fontSize: 12 }}>{x.name}</span>
          <span style={{ textAlign: 'right' }}>{fmtDist(x.dist, units)}</span>
          <span style={{ textAlign: 'right', color: 'rgba(255,255,255,0.65)' }}>{fmtElev(x.elev, units)}</span>
          <span style={{ textAlign: 'right', color: 'rgba(255,255,255,0.65)' }}>{x.dur}</span>
        </div>
      ))}
    </div>
  );
}

function PRList({ activity, accent }) {
  const r = ROLLUPS[activity];
  return (
    <div>
      <div style={{
        fontSize: 10, color: 'rgba(255,255,255,0.4)',
        letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 12,
      }}>records</div>
      {r.prs.map((pr, i) => (
        <div key={i} style={{
          display: 'grid', gridTemplateColumns: '14px 1fr auto',
          gap: 10, padding: '8px 0', fontSize: 12,
          borderBottom: i < r.prs.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none',
        }}>
          <span style={{ color: accent }}>★</span>
          <div>
            <div style={{ color: 'rgba(255,255,255,0.7)', fontSize: 11 }}>{pr.label}</div>
            <div style={{ color: 'rgba(255,255,255,0.35)', fontSize: 9, fontFamily: 'var(--mono)', marginTop: 2 }}>{pr.date}</div>
          </div>
          <span style={{ color: '#fff', fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums', alignSelf: 'center' }}>{pr.value}</span>
        </div>
      ))}
    </div>
  );
}

function VCombined({ units = 'metric', daysBack = 365 }) {
  const totals = totalRollup();
  const { set: active, dominant } = useMemoV(() => buildActiveDays(daysBack), [daysBack]);
  const [openSet, setOpenSet] = useStateV(new Set(ACTIVITY_ORDER));
  const toggle = (k) => {
    const s = new Set(openSet);
    s.has(k) ? s.delete(k) : s.add(k);
    setOpenSet(s);
  };

  const rangeLabel = daysBack === 365 ? 'Rolling 365 days' :
                     daysBack === 90  ? 'Last 90 days' :
                     daysBack === 30  ? 'Last 30 days' : `${daysBack}d`;

  return (
    <div style={{
      width: '100%', minHeight: '100%',
      background: '#0a0c0c', color: '#fff',
      fontFamily: 'var(--sans)',
      padding: '32px 36px',
      display: 'flex', flexDirection: 'column', gap: 22,
      boxSizing: 'border-box',
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between' }}>
        <div>
          <div style={{
            fontSize: 11, color: 'rgba(255,255,255,0.4)',
            letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 8,
          }}>{rangeLabel}</div>
          <h1 style={{
            fontSize: 32, fontWeight: 500, margin: 0,
            letterSpacing: '-0.02em',
          }}>
            {totals.days} <span style={{ color: 'rgba(255,255,255,0.45)', fontWeight: 300 }}>days outside</span>
          </h1>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{
            fontSize: 10, color: 'rgba(255,255,255,0.4)',
            letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 6,
          }}>Last activity</div>
          <div style={{ fontSize: 16, fontFamily: 'var(--mono)' }}>
            {fmtDate(totals.last_date)}
            <span style={{
              color: totals.days_since <= 1 ? 'oklch(0.78 0.15 145)' : totals.days_since <= 7 ? '#fff' : 'oklch(0.74 0.14 55)',
              marginLeft: 10, fontSize: 13,
            }}>
              · {totals.days_since}d ago
            </span>
          </div>
        </div>
      </div>

      {/* All-Activities stat row */}
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 1,
        background: 'rgba(255,255,255,0.06)',
        border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: 4, overflow: 'hidden',
      }}>
        {[
          ['Total days',     totals.days,                                    null],
          ['Current streak', `${totals.current_streak}d`,                    `max ${totals.longest_streak}d`],
          ['Total ascent',   fmtElev(totals.elev_gain_m, units),             elevUnit(units)],
          ['Total descent',  fmtElev(totals.elev_loss_m, units),             elevUnit(units)],
          ['Moving time',    fmtHours(totals.moving_h),                      null],
        ].map((s, i) => (
          <div key={i} style={{ background: '#0f1313', padding: '20px 22px' }}>
            <div style={{
              fontSize: 10, color: 'rgba(255,255,255,0.4)',
              letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 10,
            }}>{s[0]}</div>
            <div style={{
              fontSize: 30, color: '#fff', fontWeight: 500,
              fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums',
              letterSpacing: '-0.02em', lineHeight: 1,
            }}>
              {s[1]}
              {s[2] && (
                <span style={{ fontSize: 13, color: 'rgba(255,255,255,0.4)', marginLeft: 4, fontWeight: 400 }}>
                  {s[2]}
                </span>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Active-day ribbon (rolling N days, color-coded by activity) */}
      <div style={{
        background: '#0f1313',
        border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: 4, padding: '20px 22px',
      }}>
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: 14,
        }}>
          <div style={{
            fontSize: 10, color: 'rgba(255,255,255,0.4)',
            letterSpacing: '0.14em', textTransform: 'uppercase',
          }}>Active days · {rangeLabel.toLowerCase()}</div>
          <div style={{ display: 'flex', gap: 14 }}>
            {ACTIVITY_ORDER.map(k => {
              const a = ACTIVITY_DEFS[k];
              return (
                <span key={k} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 10, fontFamily: 'var(--mono)' }}>
                  <span style={{ width: 10, height: 10, background: a.accent, borderRadius: 2 }} />
                  <span style={{ color: 'rgba(255,255,255,0.6)' }}>{a.short}</span>
                </span>
              );
            })}
          </div>
        </div>
        <ActiveDaysRibbon active={active} dominant={dominant} daysBack={daysBack} height={36} />
      </div>

      {/* By Activity */}
      <div style={{
        fontSize: 10, color: 'rgba(255,255,255,0.4)',
        letterSpacing: '0.18em', textTransform: 'uppercase',
        marginTop: 4,
      }}>By activity · all-time</div>

      <div style={{
        background: '#0a0c0c',
        border: '1px solid rgba(255,255,255,0.06)',
        borderTop: 'none',
        borderRadius: 4,
      }}>
        {ACTIVITY_ORDER.map(k => (
          <ActivityRow
            key={k}
            k={k}
            units={units}
            expanded={openSet.has(k)}
            onToggle={() => toggle(k)}
            defaultMetric={k === 'snow' ? 'descent' : 'count'}
            defaultMode="grouped"
          />
        ))}
      </div>
    </div>
  );
}

window.VCombined = VCombined;
