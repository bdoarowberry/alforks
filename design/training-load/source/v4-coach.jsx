// V4 — Coach view. Fitness-led.
// Hero: 28-day Z2 HR trend (lower = fitter). Below: training load,
// HR-zone composition, week-by-week ramp, then activity totals as
// supporting context. Records collapsed into a single ranked column.

const { useState: useStateV4, useMemo: useMemoV4 } = React;

const V4_BG = '#080a0c';
const V4_PANEL = '#0d1115';
const V4_PANEL_HI = '#11161a';
const V4_LINE = 'rgba(255,255,255,0.07)';
const V4_DIM = 'rgba(255,255,255,0.5)';
const V4_DIMMER = 'rgba(255,255,255,0.32)';
const V4_GREEN = 'oklch(0.78 0.15 145)';
const V4_RED   = 'oklch(0.7 0.2 25)';
const V4_BLUE  = 'oklch(0.78 0.13 230)';
const V4_AMBER = 'oklch(0.78 0.16 60)';
const V4_PURPLE= 'oklch(0.72 0.13 305)';
const V4_INK   = 'oklch(0.96 0 0)';

// Training-load metrics derived from FITNESS_WEEKS
function deriveCoachStats() {
  const data = FITNESS_WEEKS;
  const z2 = data.map(d => d.z2_avg);
  const minZ2 = Math.min(...z2);
  const maxZ2 = Math.max(...z2);
  const lastZ2 = z2[z2.length - 1];
  const firstZ2 = z2[0];
  const z2Delta = lastZ2 - firstZ2;
  const z2DeltaPct = (z2Delta / firstZ2) * 100;
  // CTL/ATL approximation: ATL = 7-day rolling avg of weekly volume; CTL = 28-day
  const volumes = data.map(d => d.volume_h);
  const last4 = volumes.slice(-4);
  const last8 = volumes.slice(-8);
  const atl = last4.reduce((s, v) => s + v, 0) / 4;
  const ctl = last8.reduce((s, v) => s + v, 0) / 8;
  const tsb = ctl - atl; // freshness; positive = fresh
  const totalH = volumes.reduce((s, v) => s + v, 0);
  const totalClimb = data.reduce((s, d) => s + d.climbing_m, 0);
  // Zone share (last 4 weeks)
  const zoneTotals = [0, 0, 0, 0, 0];
  data.slice(-4).forEach(d => d.hr_zones.forEach((v, i) => zoneTotals[i] += v));
  const zoneSum = zoneTotals.reduce((s, v) => s + v, 0) || 1;
  const zoneShare = zoneTotals.map(v => v / zoneSum);

  return {
    z2, minZ2, maxZ2, lastZ2, firstZ2, z2Delta, z2DeltaPct,
    atl, ctl, tsb, totalH, totalClimb, zoneShare,
  };
}

function V4Z2Hero({ units }) {
  const s = useMemoV4(deriveCoachStats, []);
  const data = FITNESS_WEEKS;
  const labels = data.map(d => d.wk);
  const w = 720, h = 220, padL = 44, padR = 16, padT = 16, padB = 28;
  const range = s.maxZ2 - s.minZ2 || 1;
  // Visual padding within range
  const lo = s.minZ2 - range * 0.1;
  const hi = s.maxZ2 + range * 0.1;
  const span = hi - lo;
  const points = s.z2.map((v, i) => {
    const x = padL + (i / (s.z2.length - 1)) * (w - padL - padR);
    const y = h - padB - ((v - lo) / span) * (h - padT - padB);
    return [x, y, v];
  });
  const path = points.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(' ');
  const area = `${path} L${points[points.length-1][0]},${h - padB} L${points[0][0]},${h - padB} Z`;
  const goodTrend = s.z2Delta < 0;

  return (
    <div style={{
      background: V4_PANEL, border: `1px solid ${V4_LINE}`,
      borderLeft: `3px solid ${V4_RED}`, borderRadius: 4,
      padding: '22px 28px',
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 18 }}>
        <div>
          <div style={{
            fontSize: 9, color: V4_DIMMER,
            letterSpacing: '0.22em', textTransform: 'uppercase', fontWeight: 600,
          }}>Aerobic fitness</div>
          <h2 style={{
            fontSize: 22, fontWeight: 500, color: '#fff',
            letterSpacing: '-0.02em', margin: '6px 0 0',
          }}>28-day Z2 heart-rate average <span style={{ color: V4_DIMMER, fontSize: 13, fontWeight: 400, marginLeft: 6 }}>lower = fitter</span></h2>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{
            fontSize: 9, color: V4_DIMMER,
            letterSpacing: '0.18em', textTransform: 'uppercase', marginBottom: 4,
          }}>Latest</div>
          <div style={{
            fontSize: 36, color: V4_INK, fontFamily: 'var(--mono)',
            fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.02em', lineHeight: 1,
          }}>{s.lastZ2}<span style={{ color: V4_DIMMER, fontSize: 16, marginLeft: 3 }}>bpm</span></div>
          <div style={{
            fontSize: 11, color: goodTrend ? V4_GREEN : V4_RED, fontFamily: 'var(--mono)',
            marginTop: 6,
          }}>
            {goodTrend ? '↓' : '↑'} {Math.abs(s.z2Delta)}bpm vs 12wk ago ({s.z2DeltaPct.toFixed(1)}%)
          </div>
        </div>
      </div>
      <svg width="100%" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ display: 'block', height: 220 }}>
        <defs>
          <linearGradient id="v4z2grad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={V4_RED} stopOpacity="0.28" />
            <stop offset="100%" stopColor={V4_RED} stopOpacity="0" />
          </linearGradient>
        </defs>
        {/* Y axis ticks */}
        {[s.minZ2, Math.round((s.minZ2 + s.maxZ2) / 2), s.maxZ2].map((v, i) => {
          const y = h - padB - ((v - lo) / span) * (h - padT - padB);
          return (
            <g key={i}>
              <line x1={padL} x2={w - padR} y1={y} y2={y} stroke="rgba(255,255,255,0.05)" strokeDasharray="2,4" />
              <text x={padL - 8} y={y + 3} fill={V4_DIMMER} fontSize="10" fontFamily="var(--mono)" textAnchor="end">{v}</text>
            </g>
          );
        })}
        <path d={area} fill="url(#v4z2grad)" />
        <path d={path} fill="none" stroke={V4_RED} strokeWidth="2" />
        {points.map((p, i) => (
          <circle key={i} cx={p[0]} cy={p[1]} r="3" fill={V4_RED} stroke={V4_PANEL} strokeWidth="2" />
        ))}
        {labels.map((l, i) => (
          <text key={i} x={points[i][0]} y={h - 8} fill={V4_DIMMER} fontSize="9"
            fontFamily="var(--mono)" textAnchor="middle">{l}</text>
        ))}
      </svg>
    </div>
  );
}

function V4LoadCards({ units }) {
  const s = useMemoV4(deriveCoachStats, []);
  const tsbState = s.tsb > 1 ? { label: 'Fresh', color: V4_GREEN } : s.tsb < -1 ? { label: 'Fatigued', color: V4_RED } : { label: 'Steady', color: V4_BLUE };
  const cards = [
    { label: 'Acute load',   value: s.atl.toFixed(1), unit: 'h/wk', sub: 'last 4 weeks',  accent: V4_AMBER },
    { label: 'Chronic load', value: s.ctl.toFixed(1), unit: 'h/wk', sub: 'last 8 weeks',  accent: V4_PURPLE },
    { label: 'Form',         value: s.tsb >= 0 ? `+${s.tsb.toFixed(1)}` : s.tsb.toFixed(1), unit: 'h', sub: tsbState.label, accent: tsbState.color },
    { label: 'Volume',       value: s.totalH.toFixed(0), unit: 'h', sub: '12 weeks total', accent: V4_BLUE },
    { label: 'Climbing',     value: s.totalClimb.toLocaleString(), unit: 'm', sub: '12 weeks total', accent: V4_AMBER },
  ];
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 8 }}>
      {cards.map((c, i) => (
        <div key={i} style={{
          background: V4_PANEL, border: `1px solid ${V4_LINE}`,
          borderRadius: 4, padding: '16px 18px',
        }}>
          <div style={{
            fontSize: 9, color: V4_DIMMER,
            letterSpacing: '0.18em', textTransform: 'uppercase', fontWeight: 600,
            marginBottom: 10,
          }}>{c.label}</div>
          <div style={{
            fontSize: 24, color: c.accent, fontFamily: 'var(--mono)',
            fontVariantNumeric: 'tabular-nums', letterSpacing: '-0.01em', lineHeight: 1,
          }}>{c.value}<span style={{ color: V4_DIMMER, fontSize: 12, marginLeft: 3 }}>{c.unit}</span></div>
          {c.sub && (
            <div style={{ fontSize: 10, color: V4_DIMMER, marginTop: 8, fontFamily: 'var(--mono)' }}>{c.sub}</div>
          )}
        </div>
      ))}
    </div>
  );
}

// Big stacked HR-zone bar chart with totals
function V4HRStack() {
  const data = FITNESS_WEEKS;
  const totals = data.map(d => d.hr_zones.reduce((s, x) => s + x, 0));
  const max = Math.max(...totals, 1);
  return (
    <div style={{
      background: V4_PANEL, border: `1px solid ${V4_LINE}`,
      borderRadius: 4, padding: '20px 22px',
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 16,
      }}>
        <div>
          <div style={{
            fontSize: 9, color: V4_DIMMER,
            letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 600,
          }}>Weekly HR-zone time</div>
          <div style={{ fontSize: 14, color: '#fff', marginTop: 4 }}>Where you spend your minutes</div>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 14, fontSize: 10, fontFamily: 'var(--mono)', color: V4_DIM, justifyContent: 'flex-end', maxWidth: 380 }}>
          {HR_ZONE_LABELS.map((l, i) => (
            <span key={i} style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 9, height: 9, background: HR_ZONE_COLORS[i], borderRadius: 1 }} />
              <span>{l}</span>
            </span>
          ))}
        </div>
      </div>
      <div style={{
        position: 'relative', height: 200, display: 'flex', alignItems: 'flex-end', gap: 6,
        paddingLeft: 36, paddingBottom: 22,
      }}>
        <div style={{
          position: 'absolute', left: 0, top: 0, bottom: 22,
          display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
          fontSize: 10, color: V4_DIMMER, fontFamily: 'var(--mono)',
        }}>
          {[max, max*0.66, max*0.33, 0].map((v, i) => (
            <span key={i}>{v.toFixed(1)}h</span>
          ))}
        </div>
        {data.map((d, i) => (
          <div key={i} style={{
            flex: 1, height: '100%',
            display: 'flex', flexDirection: 'column-reverse', gap: 1,
          }}>
            {d.hr_zones.map((v, zi) => (
              <div key={zi} title={`${d.wk} · ${HR_ZONE_LABELS[zi]}: ${v.toFixed(1)}h`} style={{
                width: '100%',
                height: `${(v / max) * 100}%`,
                background: HR_ZONE_COLORS[zi],
                opacity: 0.95,
              }} />
            ))}
          </div>
        ))}
        <div style={{
          position: 'absolute', left: 36, right: 0, bottom: 0,
          display: 'flex', justifyContent: 'space-between',
          fontSize: 10, color: V4_DIMMER, fontFamily: 'var(--mono)',
        }}>
          {data.map((d, i) => i % 2 === 0 ? <span key={i}>{d.wk}</span> : <span key={i} />)}
        </div>
      </div>
    </div>
  );
}

function V4VolumeAndClimb() {
  const data = FITNESS_WEEKS;
  const maxH = Math.max(...data.map(d => d.volume_h), 1);
  const maxC = Math.max(...data.map(d => d.climbing_m), 1);
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
      <div style={{
        background: V4_PANEL, border: `1px solid ${V4_LINE}`,
        borderRadius: 4, padding: '20px 22px',
      }}>
        <div style={{
          fontSize: 9, color: V4_DIMMER,
          letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 600,
          marginBottom: 14,
        }}>Weekly volume</div>
        <div style={{
          position: 'relative', height: 140, display: 'flex', alignItems: 'flex-end', gap: 6,
          paddingLeft: 32, paddingBottom: 20,
        }}>
          <div style={{
            position: 'absolute', left: 0, top: 0, bottom: 20,
            display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
            fontSize: 9, color: V4_DIMMER, fontFamily: 'var(--mono)',
          }}>
            {[maxH, maxH*0.66, maxH*0.33, 0].map((v, i) => <span key={i}>{v.toFixed(0)}h</span>)}
          </div>
          {data.map((d, i) => (
            <div key={i} style={{ flex: 1, height: '100%', display: 'flex', alignItems: 'flex-end' }}>
              <div title={`${d.wk}: ${d.volume_h.toFixed(1)}h`} style={{
                width: '100%', height: `${(d.volume_h / maxH) * 100}%`,
                background: V4_PURPLE, opacity: 0.9, minHeight: 1,
              }} />
            </div>
          ))}
          <div style={{
            position: 'absolute', left: 32, right: 0, bottom: 0,
            display: 'flex', justifyContent: 'space-between',
            fontSize: 9, color: V4_DIMMER, fontFamily: 'var(--mono)',
          }}>
            {data.map((d, i) => i % 2 === 0 ? <span key={i}>{d.wk}</span> : <span key={i} />)}
          </div>
        </div>
      </div>
      <div style={{
        background: V4_PANEL, border: `1px solid ${V4_LINE}`,
        borderRadius: 4, padding: '20px 22px',
      }}>
        <div style={{
          fontSize: 9, color: V4_DIMMER,
          letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 600,
          marginBottom: 14,
        }}>Weekly climbing</div>
        <div style={{
          position: 'relative', height: 140, display: 'flex', alignItems: 'flex-end', gap: 6,
          paddingLeft: 36, paddingBottom: 20,
        }}>
          <div style={{
            position: 'absolute', left: 0, top: 0, bottom: 20,
            display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
            fontSize: 9, color: V4_DIMMER, fontFamily: 'var(--mono)',
          }}>
            {[maxC, maxC*0.66, maxC*0.33, 0].map((v, i) => <span key={i}>{Math.round(v)}m</span>)}
          </div>
          {data.map((d, i) => (
            <div key={i} style={{ flex: 1, height: '100%', display: 'flex', alignItems: 'flex-end' }}>
              <div title={`${d.wk}: ${d.climbing_m}m`} style={{
                width: '100%', height: `${(d.climbing_m / maxC) * 100}%`,
                background: V4_AMBER, opacity: 0.9, minHeight: 1,
              }} />
            </div>
          ))}
          <div style={{
            position: 'absolute', left: 36, right: 0, bottom: 0,
            display: 'flex', justifyContent: 'space-between',
            fontSize: 9, color: V4_DIMMER, fontFamily: 'var(--mono)',
          }}>
            {data.map((d, i) => i % 2 === 0 ? <span key={i}>{d.wk}</span> : <span key={i} />)}
          </div>
        </div>
      </div>
    </div>
  );
}

function V4ZoneDonut() {
  const s = useMemoV4(deriveCoachStats, []);
  return (
    <div style={{
      background: V4_PANEL, border: `1px solid ${V4_LINE}`,
      borderRadius: 4, padding: '20px 22px',
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{
        fontSize: 9, color: V4_DIMMER,
        letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 600,
        marginBottom: 14,
      }}>Last 4 weeks · zone composition</div>
      {/* Stacked horizontal bar */}
      <div style={{ display: 'flex', height: 16, borderRadius: 2, overflow: 'hidden', gap: 1, marginBottom: 14 }}>
        {s.zoneShare.map((sh, i) => sh > 0 && (
          <div key={i} title={`${HR_ZONE_LABELS[i]}: ${(sh*100).toFixed(0)}%`} style={{
            width: `${sh * 100}%`, background: HR_ZONE_COLORS[i],
          }} />
        ))}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, fontSize: 11, fontFamily: 'var(--mono)' }}>
        {s.zoneShare.map((sh, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 10, height: 10, background: HR_ZONE_COLORS[i], borderRadius: 1 }} />
            <span style={{ color: V4_DIM }}>{HR_ZONE_LABELS[i].split(' ')[0]}</span>
            <span style={{ flex: 1 }} />
            <span style={{ color: '#fff', fontVariantNumeric: 'tabular-nums' }}>{(sh*100).toFixed(0)}%</span>
          </div>
        ))}
      </div>
      <div style={{
        marginTop: 14, paddingTop: 12, borderTop: `1px solid ${V4_LINE}`,
        fontSize: 11, color: V4_DIM, lineHeight: 1.5,
      }}>
        Z2 share is <span style={{ color: '#fff', fontFamily: 'var(--mono)' }}>{(s.zoneShare[1]*100).toFixed(0)}%</span> — aim for 60–80% in base season.
      </div>
    </div>
  );
}

function V4ActivityTotals({ units }) {
  return (
    <div style={{
      background: V4_PANEL, border: `1px solid ${V4_LINE}`,
      borderRadius: 4, padding: '20px 22px',
    }}>
      <div style={{
        fontSize: 9, color: V4_DIMMER,
        letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 600,
        marginBottom: 16,
      }}>Activity totals · this season</div>
      <div style={{ display: 'grid', gap: 14 }}>
        {ACTIVITY_ORDER.map(k => {
          const def = ACTIVITY_DEFS[k];
          const r = ROLLUPS[k];
          return (
            <div key={k} style={{
              display: 'grid', gridTemplateColumns: 'auto 1fr auto auto auto',
              gap: 18, alignItems: 'baseline',
              padding: '6px 0',
              borderBottom: `1px solid ${V4_LINE}`,
            }}>
              <span style={{
                width: 22, height: 22, background: def.accentSoft, color: def.accent,
                borderRadius: 3, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 11, fontFamily: 'var(--mono)', fontWeight: 700,
              }}>{def.glyph}</span>
              <span style={{ fontSize: 13, color: '#fff' }}>{def.label}</span>
              <span style={{
                fontSize: 13, color: V4_DIM,
                fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums',
                minWidth: 70, textAlign: 'right',
              }}>{r.days}d</span>
              <span style={{
                fontSize: 13, color: '#fff',
                fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums',
                minWidth: 90, textAlign: 'right',
              }}>{fmtHours(r.moving_h)}</span>
              <span style={{
                fontSize: 13, color: V4_AMBER,
                fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums',
                minWidth: 90, textAlign: 'right',
              }}>{fmtElev(k === 'snow' ? r.elev_loss_m : r.elev_gain_m, units)}{elevUnit(units)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Derive personal records scoped to the visible FITNESS_WEEKS window
// (best in window — not lifetime PRs).
function deriveWindowPRs(units) {
  const first = FITNESS_WEEKS[0].wk;            // e.g. '02-09'
  const last  = FITNESS_WEEKS[FITNESS_WEEKS.length - 1].wk;  // e.g. '04-27'
  const year  = TODAY.slice(0, 4);
  const startIso = `${year}-${first}`;
  const endIso   = `${year}-${last}`;
  const inWin = RECENT.filter(r => r.date >= startIso && r.date <= endIso);
  if (inWin.length === 0) return [];

  const durToH = (s) => {
    const [h, m] = s.split(':').map(Number);
    return h + m / 60;
  };

  const longest    = inWin.reduce((b, r) => r.dist > b.dist ? r : b, inWin[0]);
  const mostClimb  = inWin.reduce((b, r) => r.elev > b.elev ? r : b, inWin[0]);
  const longestDur = inWin.reduce((b, r) => durToH(r.dur) > durToH(b.dur) ? r : b, inWin[0]);
  const fastest    = inWin.reduce((b, r) => r.max > b.max ? r : b, inWin[0]);
  const highestHR  = inWin.reduce((b, r) => r.hr > b.hr ? r : b, inWin[0]);

  return [
    { label: 'Longest ride',    value: `${fmtDist(longest.dist, units)} ${distUnit(units)}`,    loc: longest.name,    date: longest.date,    type: longest.type },
    { label: 'Most climbing',   value: `${fmtElev(mostClimb.elev, units)} ${elevUnit(units)}`,  loc: mostClimb.name,  date: mostClimb.date,  type: mostClimb.type },
    { label: 'Longest duration',value: longestDur.dur,                                          loc: longestDur.name, date: longestDur.date, type: longestDur.type },
    { label: 'Top speed',       value: `${fmtSpeed(fastest.max, units)} ${speedUnit(units)}`,   loc: fastest.name,    date: fastest.date,    type: fastest.type },
    { label: 'Highest avg HR',  value: `${highestHR.hr} bpm`,                                   loc: highestHR.name,  date: highestHR.date,  type: highestHR.type },
  ];
}

function V4Records({ units }) {
  const prs = useMemoV4(() => deriveWindowPRs(units), [units]);
  const first = FITNESS_WEEKS[0].wk;
  const last  = FITNESS_WEEKS[FITNESS_WEEKS.length - 1].wk;
  return (
    <div style={{
      background: V4_PANEL, border: `1px solid ${V4_LINE}`,
      borderRadius: 4, padding: '20px 22px',
    }}>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
        marginBottom: 14,
      }}>
        <div style={{
          fontSize: 9, color: V4_DIMMER,
          letterSpacing: '0.2em', textTransform: 'uppercase', fontWeight: 600,
        }}>Personal records · this window</div>
        <div style={{
          fontSize: 9, color: V4_DIMMER, fontFamily: 'var(--mono)',
        }}>{first} → {last}</div>
      </div>
      <div style={{ display: 'grid', gap: 0 }}>
        {prs.map((pr, i) => {
          const def = ACTIVITY_DEFS[pr.type];
          return (
            <div key={i} style={{
              display: 'grid', gridTemplateColumns: '1fr auto',
              alignItems: 'baseline', gap: 16,
              padding: '10px 0',
              borderBottom: i < prs.length - 1 ? `1px solid ${V4_LINE}` : 'none',
            }}>
              <div>
                <div style={{ fontSize: 12, color: '#fff' }}>{pr.label}</div>
                <div style={{
                  fontSize: 10, color: V4_DIMMER, marginTop: 3, fontFamily: 'var(--mono)',
                  display: 'flex', alignItems: 'center', gap: 6,
                }}>
                  <span style={{ width: 6, height: 6, background: def.accent, borderRadius: '50%' }} />
                  {pr.loc} · {pr.date}
                </div>
              </div>
              <div style={{
                fontSize: 18, color: V4_AMBER,
                fontFamily: 'var(--mono)', fontVariantNumeric: 'tabular-nums',
                letterSpacing: '-0.01em',
              }}>{pr.value}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function V4Coach({ units = 'metric' }) {
  return (
    <div style={{
      width: '100%', minHeight: '100%',
      background: V4_BG, color: '#fff',
      fontFamily: 'var(--sans)',
      padding: 20,
      boxSizing: 'border-box',
      display: 'flex', flexDirection: 'column', gap: 12,
    }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', padding: '4px 4px 8px' }}>
        <div>
          <div style={{
            fontSize: 9, color: V4_RED,
            letterSpacing: '0.3em', textTransform: 'uppercase', fontWeight: 700,
          }}>Coach view</div>
          <div style={{
            fontSize: 22, fontWeight: 500, color: '#fff',
            letterSpacing: '-0.02em', marginTop: 4,
          }}>Training load · {fmtDate(TODAY)} 2026</div>
        </div>
        <div style={{ fontSize: 11, color: V4_DIMMER, fontFamily: 'var(--mono)', textAlign: 'right' }}>
          12 weeks of data · ramp into MTB season
        </div>
      </div>

      {/* Hero Z2 */}
      <V4Z2Hero units={units} />
      {/* Load + form cards */}
      <V4LoadCards units={units} />
      {/* HR zones */}
      <V4HRStack />
      <div style={{ display: 'grid', gridTemplateColumns: '1.6fr 1fr', gap: 8 }}>
        <V4VolumeAndClimb />
        <V4ZoneDonut />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1.4fr 1fr', gap: 8 }}>
        <V4Records units={units} />
        <V4ActivityTotals units={units} />
      </div>
    </div>
  );
}

window.V4Coach = V4Coach;
