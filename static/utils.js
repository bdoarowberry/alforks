// Shared formatting utilities used across all pages.

function fmtDuration(sec, { showSeconds = false } = {}) {
  if (sec == null) return '--';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  if (showSeconds) return m > 0 ? `${m}m ${String(s).padStart(2, '0')}s` : `${s}s`;
  return `${m}m`;
}

function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-CA', { year: 'numeric', month: 'short', day: 'numeric' });
}

function fmtNum(n, d = 0) {
  return n != null ? n.toLocaleString('en-CA', { maximumFractionDigits: d, minimumFractionDigits: d }) : '--';
}

// Round-and-localize for elevation values. Null-safe so callers can pipe
// raw stats through without pre-checking.
function fmtElev(m) {
  if (m == null) return '—';
  return Math.round(m).toLocaleString();
}

// Escape user-supplied strings before interpolating into innerHTML
// templates. The five chars handled are sufficient for HTML body /
// attribute contexts; anything stricter (script context, etc.) needs a
// different escape policy.
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

// Activity-type abbreviation — exactly 3 characters, used as the badge
// text wherever activities are listed (Logs row, Compare picker, Setup
// swatch, Training totals, Summary, etc.). Accepts either a type object
// or a type id; if the type object carries an explicit `glyph` field
// (set on the Setup page), that wins. Otherwise falls back to a hand-
// tuned override map for built-in types, then to a 3-char uppercase
// slice of the id.
const TYPE_GLYPH_OVERRIDES = {
  mtb:        'MTB',
  snowboard:  'SNO',
  ski:        'SKI',
  hike:       'HIK',
  fat_biking: 'FAT',
  other:      'UNK',
};
function typeGlyph(input) {
  if (!input) return '?';
  if (typeof input === 'object') {
    if (input.glyph) return String(input.glyph).toUpperCase().slice(0, 3);
    return typeGlyph(input.id);
  }
  if (TYPE_GLYPH_OVERRIDES[input]) return TYPE_GLYPH_OVERRIDES[input];
  return String(input).replace(/_/g, '').slice(0, 3).toUpperCase();
}

// Lazy mini-map manager — shared by the /logs and /routes activity lists.
// Each page lazily inits a Leaflet map per list row as it scrolls into view
// (via IntersectionObserver) and caps the number of live maps with an LRU
// eviction that only drops maps scrolled out of view. The page owns the
// per-row init (tiles, colours, geometry); this owns the instance cache +
// eviction. Typical use inside a page's init function:
//
//   const maps = createLazyMapManager({ maxActive: 20 });
//   if (maps.has(key)) { maps.touch(key); return; }   // already live
//   ... build Leaflet map `m` ...; el.dataset.mapInit = '1';
//   maps.register(key, m);                              // cache + evict-if-over
//
// Eviction drops the least-recently-touched maps that are off-screen
// (~200px margin) once `maxActive` is exceeded, clearing each evicted map's
// container `data-map-init` so it re-inits when scrolled back.
function createLazyMapManager({ maxActive }) {
  const instances = new Map();   // key → Leaflet map
  const access    = new Map();   // key → last-touch timestamp (LRU order)

  function evictIfOver() {
    if (instances.size <= maxActive) return;
    const sorted = [...access.entries()].sort((a, b) => a[1] - b[1]);
    for (const [key] of sorted) {
      if (instances.size <= maxActive) break;
      const m = instances.get(key);
      if (!m) continue;
      const el = m._container;
      const r = el.getBoundingClientRect();
      const visible = r.bottom > -200 && r.top < (window.innerHeight + 200);
      if (visible) continue;
      m.remove();
      instances.delete(key);
      access.delete(key);
      el.dataset.mapInit = '';
    }
  }

  return {
    instances,                                  // exposed for page-specific sweeps
    has:   (key) => instances.has(key),
    get:   (key) => instances.get(key),
    touch: (key) => access.set(key, Date.now()),
    register(key, map) {
      instances.set(key, map);
      access.set(key, Date.now());
      evictIfOver();
    },
    remove(key) {
      const m = instances.get(key);
      if (m) { try { m.remove(); } catch (_e) {} }
      instances.delete(key);
      access.delete(key);
    },
    evictIfOver,
    destroyAll() {
      for (const m of instances.values()) { try { m.remove(); } catch (_e) {} }
      instances.clear();
      access.clear();
    },
    invalidateAll() {
      for (const m of instances.values()) { try { m.invalidateSize(); } catch (_e) {} }
    },
  };
}

// Leaflet basemap tile layers for the full interactive maps (compare, heatmap).
// Street = OpenStreetMap, Satellite = Esri World Imagery; both maxZoom 19 with
// attribution. Returns a FRESH layer each call — a Leaflet layer instance can
// only belong to one map, so the dual-map compare view needs its own per map.
function makeStreetLayer() {
  return L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '© <a href="https://openstreetmap.org/copyright">OpenStreetMap</a>',
  });
}
function makeSatelliteLayer() {
  return L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 19, attribution: '© <a href="https://www.esri.com/">Esri</a> World Imagery',
  });
}

// Shared Mapbox GL 3D terrain map (satellite-streets style + DEM-exaggerated
// terrain + atmospheric sky), used by the compare and heatmap 3D views. Once
// the base terrain/sky have loaded, `onLoad(map)` runs so the page can add its
// own data sources/layers and mark itself ready. Returns the map synchronously
// — sources added inside onLoad don't exist until the load event fires.
function createTerrain3DMap(containerId, token, onLoad) {
  mapboxgl.accessToken = token;
  const map = new mapboxgl.Map({
    container: containerId,
    style: 'mapbox://styles/mapbox/satellite-streets-v12',
    center: [-114.74, 50.89], zoom: 10, pitch: 55, bearing: -15, antialias: true,
  });
  map.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), 'top-right');
  map.on('load', () => {
    map.addSource('mapbox-dem', { type: 'raster-dem', url: 'mapbox://mapbox.mapbox-terrain-dem-v1', tileSize: 512, maxzoom: 14 });
    map.setTerrain({ source: 'mapbox-dem', exaggeration: 1.5 });
    map.addLayer({ id: 'sky', type: 'sky', paint: { 'sky-type': 'atmosphere', 'sky-atmosphere-sun': [0.0, 90.0], 'sky-atmosphere-sun-intensity': 15 } });
    onLoad(map);
  });
  return map;
}

// Toggle the browser Fullscreen API on a map wrapper element (by id), so just
// the map area expands. Exits if already fullscreen. Each page's own
// `fullscreenchange` listener refreshes map canvas sizes after the transition.
function toggleMapFullscreen(targetId) {
  const target = document.getElementById(targetId);
  if (document.fullscreenElement) document.exitFullscreen?.();
  else                            target.requestFullscreen?.();
}

// Swap a Leaflet map's basemap between street and satellite in place. `view`
// of 'map' selects the street layer, anything else the satellite layer. Both
// layers must already exist (see makeStreetLayer / makeSatelliteLayer).
function swapLeafletBasemap(map, view, streetLayer, satelliteLayer) {
  // Preserve the camera across the swap. invalidateSize() is needed in case the
  // map was hidden (e.g. returning from the 3D view), but with the leaflet-rotate
  // plugin active it can otherwise snap the map back to its initial view — which
  // is the "map jumps to default location on style change" bug. Capture the
  // center/zoom/bearing and restore them after.
  const center  = map.getCenter();
  const zoom    = map.getZoom();
  const bearing = (typeof map.getBearing === 'function') ? map.getBearing() : null;
  map.removeLayer(view === 'map' ? satelliteLayer : streetLayer);
  (view === 'map' ? streetLayer : satelliteLayer).addTo(map);
  map.invalidateSize();
  map.setView(center, zoom, { animate: false });
  if (bearing !== null && typeof map.setBearing === 'function') map.setBearing(bearing);
}

// A non-interactive Leaflet mini-map on element `el`: every pan/zoom/keyboard
// interaction and the default controls are disabled so it renders as a static
// thumbnail. Used by the logs + routes list mini-maps; the caller adds the tile
// layer, polylines, and fitBounds.
function makeStaticMiniMap(el) {
  return L.map(el, {
    zoomControl: false, attributionControl: false, dragging: false,
    scrollWheelZoom: false, doubleClickZoom: false, boxZoom: false,
    keyboard: false, touchZoom: false,
  });
}

// Open-Meteo weather code → emoji glyph. Used by the activity header
// weather strip and the per-row weather summary on /logs.
function weatherIcon(code) {
  if (code == null) return '';
  if (code === 0) return '☀';
  if (code <= 3)  return '⛅';
  if (code <= 48) return '🌫';
  if (code <= 67) return '🌧';
  if (code <= 77) return '❄';
  if (code <= 82) return '🌧';
  if (code <= 86) return '❄';
  return '⛈';
}
