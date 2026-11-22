/**
 * Lab Monitor Dashboard - Frontend Logic
 * Upgraded with Plotly.js for interactive charts
 *
 * HTML safety rule: markup is built only with the html`` tag below, and every
 * ${...} inside it is escapeHtml(...) for strings, a numeric formatter
 * (safeFixed / fmtInt / fmtWidth / formatBytes) for numbers, a class name from
 * levelClass() held in a *Cls variable, or a fragment held in a *Html variable
 * that was built by the same rule.  tests/dashboard/test_dashboard_xss.py
 * enforces this.
 */

let refreshInterval = 30000;
let staleMs         = 15 * 60 * 1000;       // amber: no system_metrics for 15 min
let offlineMs       = 2 * 60 * 60 * 1000;   // red: no system_metrics for 2 h
let diskStaleMs     = 36 * 60 * 60 * 1000;  // disk scans are daily; flag after 36 h
let maxChartPoints  = 2000;
let dataTimeoutMs   = 25000;                // /api/data request that hasn't answered = server down

// Warning/danger thresholds (percent, compared with >=).  Shared by the
// summary strip and the full cards so both always agree.
const THRESHOLDS = {
    cpu:  { warn: 50, danger: 75 },
    ram:  { warn: 50, danger: 75 },
    disk: { warn: 75, danger: 90 },
    gpu:  { warn: 70, danger: 90 },
    vram: { warn: 75, danger: 90 },
    diskBusy: { warn: 60, danger: 85 },    // busiest physical disk, % of the interval busy
};

// Device-type grouping for the summary strip and the card grid
const GROUP_ORDER  = ['server', 'nas', 'nas-instrument', 'nas-backup'];
const GROUP_LABELS = {
    'server':         'Servers',
    'nas':            'NAS',
    'nas-instrument': 'NAS Instrument',
    'nas-backup':     'NAS Backup',
    'other':          'Other',
};
const NAS_TYPES = new Set(['nas', 'nas-instrument', 'nas-backup']);

// Base-unit → display parameters.  The collector declares what unit it
// measures in; this table says how to present that unit on a chart.
// Falls back to METRIC_UNITS below for legacy/unknown fields.
const BASE_UNIT_DISPLAY = {
    '%':     { unit: '%',     scale: 1,             decimals: 1, beginAtZero: true  },
    's':     { unit: ' h',    scale: 1 / 3600,      decimals: 1, beginAtZero: false },
    'Mbps':  { unit: ' Mbps', scale: 1,             decimals: 2, beginAtZero: true  },
    'bytes': { unit: ' GiB',  scale: 1 / 1073741824,decimals: 2, beginAtZero: false },
    'B/s':   { unit: ' MB/s', scale: 1e-6,          decimals: 2, beginAtZero: true  },
    'ops/s': { unit: ' IOPS', scale: 1,             decimals: 1, beginAtZero: true  },
    'ms':    { unit: ' ms',   scale: 1,             decimals: 1, beginAtZero: true  },
    'count': { unit: '',      scale: 1,             decimals: 0, beginAtZero: true  },
    '°C':    { unit: '°C',    scale: 1,             decimals: 1, beginAtZero: false },
    'h':     { unit: ' h',    scale: 1,             decimals: 1, beginAtZero: false },
    'cores': { unit: ' cores',scale: 1,             decimals: 2, beginAtZero: true  },
};

// Field-name fallback for metrics that predate unit fields in the collector
const METRIC_UNITS = {
    cpu_percent:                { yLabel: 'CPU Usage (%)',    unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
    ram_percent:                { yLabel: 'RAM Usage (%)',    unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
    uptime_seconds:             { yLabel: 'Uptime (hours)',   unit: ' h',    decimals: 1, scale: 1 / 3600,      beginAtZero: false },
    network_bandwidth_in_mbps:  { yLabel: 'Download (Mbps)', unit: ' Mbps', decimals: 2, scale: 1,             beginAtZero: true  },
    network_bandwidth_out_mbps: { yLabel: 'Upload (Mbps)',   unit: ' Mbps', decimals: 2, scale: 1,             beginAtZero: true  },
    network_bytes_in:           { yLabel: 'Data In (GiB)',    unit: ' GiB',  decimals: 2, scale: 1 / 1073741824, beginAtZero: false },
    network_bytes_out:          { yLabel: 'Data Out (GiB)',   unit: ' GiB',  decimals: 2, scale: 1 / 1073741824, beginAtZero: false },
    cpu_iowait_percent:         { yLabel: 'I/O Wait (%)',     unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
    cpu_steal_percent:          { yLabel: 'CPU Steal (%)',    unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
    proc_cpu_coverage_percent:  { yLabel: 'CPU Coverage (%)', unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
    disk_read_bytes_per_s:      { yLabel: 'Disk Read (MB/s)', unit: ' MB/s', decimals: 2, scale: 1e-6,          beginAtZero: true  },
    disk_write_bytes_per_s:     { yLabel: 'Disk Write (MB/s)',unit: ' MB/s', decimals: 2, scale: 1e-6,          beginAtZero: true  },
    disk_read_iops:             { yLabel: 'Read IOPS',        unit: ' IOPS', decimals: 1, scale: 1,             beginAtZero: true  },
    disk_write_iops:            { yLabel: 'Write IOPS',       unit: ' IOPS', decimals: 1, scale: 1,             beginAtZero: true  },
    disk_await_ms:              { yLabel: 'Disk Latency (ms)',unit: ' ms',   decimals: 1, scale: 1,             beginAtZero: true  },
    disk_busy_max_percent:      { yLabel: 'Busiest Disk (%)', unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
};

// Chart titles for fields whose card item has no .metric-label (or a terse one)
const FIELD_LABELS = {
    cpu_percent:            'CPU',
    cpu_cores:              'CPU cores',
    cpu_seconds:            'CPU-seconds',
    ram_bytes:              'RAM',
    gpu_util_percent:       'GPU busy',
    gpu_vram_bytes:         'GPU VRAM',
    gpu_busy_seconds:       'GPU busy time',
    gpu_held_seconds:       'GPU held time',
    cpu_iowait_percent:     'I/O wait',
    disk_read_bytes_per_s:  'Disk read',
    disk_write_bytes_per_s: 'Disk write',
    disk_busy_max_percent:  'Busiest disk',
    disk_await_ms:          'Disk latency',
};

// "All Available" asks for everything since this date; the Manager (or the
// dashboard server, for older Managers) downsamples to maxChartPoints.
const ALL_TIME_FROM = '2000-01-01T00:00:00Z';

// -------------------------------------------------------------------------
// State
// -------------------------------------------------------------------------

let pollTimer    = null;
let loadInFlight = false;
let lastGoodData = null;          // last successful /api/data payload, kept during outages
const cardsByName = new Map();    // system name -> { el, html } (cards are updated in place)

// The chart callout shows one selection at a time.  Every request bumps
// chart.seq; a response whose seq is no longer current is dropped, so a slow
// earlier request can never overwrite a newer selection.
const chart = {
    seq:        0,
    controller: null,   // AbortController of the in-flight history request
    type:       null,   // 'metric' | 'volume'
    dataType:   null,   // history data type when not the default (e.g. 'user_metrics')
    entity:     null,   // one user of user_metrics, or null
    system:     null,
    field:      null,   // metric field, or volume path without leading slash
    label:      null,
    range:      null,   // data-range key of the active range button
    data:       null,   // points currently plotted (source for CSV export)
    meta:       null,   // { downsampled, truncated, band, resolution } of the plotted data
};

// What the Manager supports, from /api/data "features": false = the Manager
// predates that endpoint (its section is hidden), true = it answered,
// null = the call failed this time.
let managerFeatures = {};

// Expanded parts of the cards, kept across refreshes
const expandedOwners = new Set();   // systems whose full "Top storage users" list is open
const folderOwners   = new Map();   // "system\0path" -> owner list loaded from a folder's cold-data bar

// Which part of each system a Manager endpoint supplies (app.py
// get_all_systems_data).  /api/devices is not listed: when it fails the whole
// poll fails (HTTP 502) and handleLoadFailure keeps the last good data.
const ENDPOINT_SECTIONS = {
    '/api/metrics/all':           'metrics',
    '/api/usage/all':             'disk',
    '/api/user_metrics/all':      'users',
    '/api/totals':                'totals',
    '/api/sessions/all':          'sessions',
    '/api/storage/health/all':    'storage_health',
    '/api/storage/breakdown/all': 'breakdown',
    '/api/user_metrics/usage':    'usage',
};
const SECTION_LABELS = {
    metrics: 'metrics', disk: 'storage', users: 'user activity',
    sessions: 'connections', storage_health: 'storage health', breakdown: 'storage owners', usage: 'usage totals',
};
// Parts of the user-activity block, which NAS cards don't show
const SERVER_ONLY_SECTIONS = new Set(['users', 'usage']);

// Ageing of the Connected list (list_confirmed_at) and counts (snapshot timestamp)
const SESSIONS_AGED_MS = 15 * 60 * 1000;   // greyed out, "as of hh:mm"
const SESSIONS_HIDE_MS = 60 * 60 * 1000;   // list hidden, "not reported since ..."
const HEALTH_STALE_MS  = 3 * 3600 * 1000;  // health line marked stale (hourly heartbeat missed 3 times)
const OWNERS_SHOWN     = 5;                // "Top storage users" rows shown before the +N more toggle

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

function initDashboard(config) {
    config = config || {};
    refreshInterval = config.refreshInterval || 30000;
    if (config.staleMinutes)   staleMs        = config.staleMinutes   * 60 * 1000;
    if (config.offlineMinutes) offlineMs      = config.offlineMinutes * 60 * 1000;
    if (config.diskStaleHours) diskStaleMs    = config.diskStaleHours * 60 * 60 * 1000;
    if (config.maxChartPoints) maxChartPoints = config.maxChartPoints;
    if (config.dataTimeoutMs)  dataTimeoutMs  = config.dataTimeoutMs;

    setupChartCallout();
    setupCardClicks();
    document.addEventListener('visibilitychange', onVisibilityChange);
    loadData();
    startPolling();
    console.log(`Dashboard initialized. Refresh: ${refreshInterval}ms`);
}

function setupChartCallout() {
    const callout = document.getElementById('chartCallout');
    const closeBtn = document.querySelector('.chart-close');

    closeBtn.onclick = closeChart;
    callout.onclick = (e) => {
        if (e.target === callout) closeChart();
    };

    document.querySelectorAll('.range-btn[data-range]').forEach(btn => {
        btn.addEventListener('click', () => applyRange(btn.dataset.range));
    });

    // Initialize date pickers to sensible defaults
    const today = new Date();
    const thirtyDaysAgo = new Date(today.getTime() - 30 * 24 * 60 * 60 * 1000);
    document.getElementById('customTo').valueAsDate = today;
    document.getElementById('customFrom').valueAsDate = thirtyDaysAgo;
}

// One delegated listener for every card (cards are re-rendered in place, so
// per-element listeners would have to be re-attached on each refresh).
// History stays clickable even when a system is offline.
function setupCardClicks() {
    document.getElementById('nasGrid').addEventListener('click', (e) => {
        const card = e.target.closest('.nas-card');
        if (!card) return;
        const system = card.dataset.systemName;

        if (e.target.closest('[data-expand]')) {
            toggleOwnerList(system);
            return;
        }
        const folderEl = e.target.closest('[data-folder]');
        if (folderEl) {
            toggleFolderOwners(system, folderEl.dataset.folder);
            return;
        }

        const metricEl = e.target.closest('[data-metric]');
        if (metricEl) {
            const metric   = metricEl.dataset.metric;
            const dataType = metricEl.dataset.type || null;     // default: system_metrics
            const entity   = metricEl.dataset.entity || null;   // one user of user_metrics
            let label = chartLabelFor(metric, dataType, entity)
                     || metricEl.querySelector('.metric-label')?.textContent || metric;
            // For GPU metrics, prefix the GPU index for clarity
            const gpu = metric.match(/^gpu_(\d+)_/);
            if (gpu) label = `GPU ${gpu[1]} - ${label}`;
            showMetricChart(system, metric, label, dataType, entity);
            return;
        }

        const volumeEl = e.target.closest('[data-volume]');
        if (volumeEl) {
            const volume = volumeEl.dataset.volume;
            const label  = volumeEl.querySelector('.volume-btn-name')?.textContent || volume;
            showVolumeChart(system, volume, label);
        }
    });
}

// -------------------------------------------------------------------------
// Polling (paused while the tab is hidden)
// -------------------------------------------------------------------------

function startPolling() {
    stopPolling();
    if (!document.hidden) pollTimer = setInterval(loadData, refreshInterval);
}

function stopPolling() {
    if (pollTimer !== null) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function onVisibilityChange() {
    if (document.hidden) {
        stopPolling();
    } else {
        loadData();       // refresh immediately when the tab comes back
        startPolling();
    }
}

// -------------------------------------------------------------------------
// Data loading
// -------------------------------------------------------------------------

async function loadData() {
    if (loadInFlight) return;
    loadInFlight = true;
    // A request that never settles (dropped keep-alive connection, hung
    // server) would block every later poll and leave "Connected" on screen,
    // so give up after dataTimeoutMs and report it.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), dataTimeoutMs);
    try {
        let resp;
        let body = null;
        try {
            resp = await fetch('/api/data', { cache: 'no-store', signal: controller.signal });
            try { body = await resp.json(); } catch (e) { body = null; }
        } catch (err) {
            if (!controller.signal.aborted) {
                // Network-level failure: the dashboard server itself is unreachable
                console.error('Error contacting dashboard server:', err);
                handleLoadFailure('Dashboard server unreachable', []);
                return;
            }
        }
        if (controller.signal.aborted) {
            console.error(`No answer from /api/data within ${dataTimeoutMs} ms`);
            handleLoadFailure('Dashboard server not responding', []);
            return;
        }

        if (signInRequired(resp, body)) {
            goToSignIn();
            return;
        }

        if (!resp.ok || !body || body.success !== true) {
            const what = describeHttpFailure(resp.status, body);
            const errors = (body && Array.isArray(body.errors)) ? body.errors : [];
            console.error('Error loading data:', what, errors);
            handleLoadFailure(what, errors);
            return;
        }

        const data = body.degraded ? mergeLastGood(body, lastGoodData) : body;
        lastGoodData = data;
        applyData(data);
    } finally {
        clearTimeout(timer);
        loadInFlight = false;
    }
}

// The dashboard server answers 401 with {"login": "/login"} when this browser
// is not signed in (any more): the remembered sign-in expired, or the password
// was changed.  Other 401s (a proxy's, say) are ordinary failures.
function signInRequired(resp, body) {
    return resp.status === 401 && !!body && body.login === '/login';
}

// Stop polling and open the sign-in page; it comes back to this page afterwards.
function goToSignIn() {
    stopPolling();
    const here = window.location.pathname + window.location.search;
    window.location.assign('/login?next=' + encodeURIComponent(here));
}

// Only the dashboard server's own JSON says the Manager is the problem.  A
// reverse proxy in front of a stopped dashboard also answers 502 (with HTML).
function describeHttpFailure(status, body) {
    if (body && (body.error === 'Manager unavailable' || Array.isArray(body.errors))) {
        return 'Manager unreachable';
    }
    if (!body && status >= 502 && status <= 504) {
        return `Dashboard server unreachable (HTTP ${status})`;
    }
    return `Dashboard error (HTTP ${status})`;
}

// For a degraded poll, keep each failed endpoint's part of every system from
// the last good data instead of dropping it (a slow /api/metrics/all would
// otherwise make every system look offline).  Marks each system with
//   staleSections: parts carried over from an earlier poll (shown greyed out)
//   unavailable:   parts that failed with no earlier copy (first load, or a
//                  system not seen before)
function mergeLastGood(data, prev) {
    const failed      = failedEndpoints(data);
    const prevSystems = (prev && prev.systems) || {};
    for (const [name, sys] of Object.entries(data.systems || {})) {
        const old = Object.prototype.hasOwnProperty.call(prevSystems, name) ? prevSystems[name] : null;
        sys.staleSections = [];
        sys.unavailable   = [];
        for (const [endpoint, key] of Object.entries(ENDPOINT_SECTIONS)) {
            if (!failed.has(endpoint)) continue;
            if (!old) {
                sys.unavailable.push(key);
            } else if (old[key] !== null && old[key] !== undefined) {
                sys[key] = old[key];
                sys.staleSections.push(key);
            }
        }
    }
    if (failed.has('/api/totals') && prev && prev.global_totals) data.global_totals = prev.global_totals;
    // A failed optional call (null) says nothing about support: keep the last answer
    const features = Object.assign({}, data.features || {});
    for (const [key, on] of Object.entries((prev && prev.features) || {})) {
        if (features[key] === null || features[key] === undefined) features[key] = on;
    }
    data.features = features;
    return data;
}

function failedEndpoints(data) {
    return new Set(((data && data.errors) || []).map(e => e && e.endpoint));
}

function applyData(data) {
    setStale(false);
    if (data.degraded) {
        showBanner('warning',
            'Some Manager data is unavailable. Those parts of the page show the last values received '
            + '(greyed out) or are marked unavailable: ' + describeErrors(data.errors));
        updateStatus('degraded');
    } else {
        showBanner(null);
        updateStatus('connected');
    }
    document.getElementById('lastUpdate').textContent = 'Last updated: ' + formatClock(data.timestamp);

    // A rendering bug must not look like a connection problem
    try {
        updateDashboard(data);
        showRenderNotice(null);
    } catch (err) {
        console.error('Error rendering dashboard data:', err);
        showRenderNotice('Some data could not be displayed (details in the browser console).');
    }
}

// Keep the last good data on screen (marked stale) instead of wiping the grid
function handleLoadFailure(what, errors) {
    updateStatus('error', what);
    const detail = errors && errors.length ? ' (' + describeErrors(errors) + ')' : '';
    if (lastGoodData) {
        setStale(true);
        const asOf = parseTs(lastGoodData.timestamp);
        const age  = asOf === null ? '' : ', ' + formatAge(Date.now() - asOf) + ' ago';
        showBanner('error', what + detail + '. Showing the last data received at '
            + formatClock(lastGoodData.timestamp) + age + '. Retrying…');
    } else {
        showBanner('error', what + detail + '. Retrying…');
        setGridMessage('⚠️ Unable to reach Manager. Retrying...');
    }
}

function describeErrors(errors) {
    return (errors || [])
        .map(e => String((e && e.endpoint) || '?') + ': ' + String((e && e.error) || 'error'))
        .join('; ');
}

function showBanner(level, message) {
    const el = document.getElementById('connectionBanner');
    if (!level) {
        el.hidden = true;
        el.textContent = '';
        el.className = 'conn-banner';
        return;
    }
    el.className = 'conn-banner ' + level;
    el.textContent = message;
    el.hidden = false;
}

function showRenderNotice(message) {
    const el = document.getElementById('renderNotice');
    el.textContent = message || '';
    el.hidden = !message;
}

function setStale(isStale) {
    for (const id of ['nasGrid', 'systemSummaryStrip', 'summarySection']) {
        document.getElementById(id).classList.toggle('stale-data', isStale);
    }
}

// -------------------------------------------------------------------------
// Dashboard update
// -------------------------------------------------------------------------

function updateDashboard(data) {
    const systems      = data.systems       || {};
    const globalTotals = data.global_totals || {};
    const names        = Object.keys(systems);
    managerFeatures    = data.features      || {};

    // Total storage: sum used and capacity across all volumes on all systems
    let totalStorageUsed     = 0;
    let totalStorageCapacity = 0;
    for (const sys of Object.values(systems)) {
        const vols = sumVolumes(sys);
        totalStorageUsed     += vols.used;
        totalStorageCapacity += vols.total;
    }
    updateSummary(globalTotals, totalStorageUsed, totalStorageCapacity, failedEndpoints(data));

    if (names.length === 0) {
        document.getElementById('systemSummaryStrip').replaceChildren();
        renderCards([]);
        setGridMessage('No systems reporting yet');
        return;
    }

    // Bucket systems by device type in fixed order; unknown types go under
    // 'other'.  Cards are sorted alphabetically within each group.
    const grouped = {};
    for (const [name, sys] of Object.entries(systems)) {
        const key = (sys.device_type || '').toLowerCase();
        const bucket = GROUP_ORDER.includes(key) ? key : 'other';
        (grouped[bucket] = grouped[bucket] || []).push([name, sys]);
    }
    const ordered = [];
    for (const key of [...GROUP_ORDER, 'other']) {
        if (!grouped[key]) continue;
        grouped[key].sort((a, b) => a[0].localeCompare(b[0]));
        ordered.push([key, grouped[key]]);
    }

    renderSummaryStrip(ordered);
    renderCards(ordered.flatMap(([, items]) => items));
}

function updateSummary(globalTotals, storageUsed, storageCap, failed) {
    // Global totals come from server-side accumulation (survives reboots)
    setTransfer('lifetimeTransferIn',  globalTotals.total_bytes_in,  'in');
    setTransfer('lifetimeTransferOut', globalTotals.total_bytes_out, 'out');

    document.getElementById('totalStorage').textContent =
        storageCap > 0 ? `${formatBytes(storageUsed)} of ${formatBytes(storageCap)}` : '—';

    // Grey out totals that come from an earlier poll (degraded refresh)
    document.getElementById('totalStorage').classList.toggle('stale-data', failed.has('/api/usage/all'));
    for (const id of ['lifetimeTransferIn', 'lifetimeTransferOut']) {
        document.getElementById(id).classList.toggle('stale-data', failed.has('/api/totals'));
    }
}

function setTransfer(id, bytes, label) {
    const el = document.getElementById(id);
    const span = document.createElement('span');
    span.className = 'transfer-label';
    span.textContent = label;
    el.replaceChildren(document.createTextNode(formatBytes(bytes) + ' '), span);
}

// Sum used/capacity over the volumes that report both values
function sumVolumes(sys) {
    let used = 0;
    let total = 0;
    for (const vol of ((sys.disk || {}).volumes || [])) {
        const u = toNumber(vol.usage_bytes);
        const t = toNumber(vol.total_bytes);
        if (u !== null && t !== null && t > 0) {
            used  += u;
            total += t;
        }
    }
    return { used, total };
}

// ok / stale / offline, from the age of the last system_metrics report.
// 'unknown' when the Manager's metrics call failed and there is no earlier
// copy: a failed call is no evidence that the system is offline.
function systemState(sys) {
    if (!sys.metrics && (sys.unavailable || []).includes('metrics')) return { state: 'unknown', ageMs: null };
    const ts = parseTs(sys.metrics ? sys.metrics.timestamp : null);
    if (ts === null) return { state: 'offline', ageMs: null };
    const ageMs = Date.now() - ts;
    if (ageMs > offlineMs) return { state: 'offline', ageMs };
    if (ageMs > staleMs)   return { state: 'stale',   ageMs };
    return { state: 'ok', ageMs };
}

function setGridMessage(text) {
    const grid = document.getElementById('nasGrid');
    let msg = grid.querySelector(':scope > .loading');
    if (!msg) {
        msg = document.createElement('div');
        msg.className = 'loading';
        grid.prepend(msg);
    }
    msg.textContent = text;
}

function clearGridMessage() {
    document.querySelectorAll('#nasGrid > .loading').forEach(el => el.remove());
}

// -------------------------------------------------------------------------
// System summary buttons (compact strip above the full cards)
// -------------------------------------------------------------------------

function renderSummaryStrip(orderedGroups) {
    const strip = document.getElementById('systemSummaryStrip');
    const rows = [];
    for (const [key, items] of orderedGroups) {
        const row = document.createElement('div');
        row.className = 'sys-btn-group';
        const lbl = document.createElement('div');
        lbl.className = 'sys-btn-group-label';
        lbl.textContent = GROUP_LABELS[key] || key;
        row.appendChild(lbl);
        for (const [name, sys] of items) row.appendChild(createSummaryButton(name, sys));
        rows.push(row);
    }
    strip.replaceChildren(...rows);
}

function createSummaryButton(systemName, sys) {
    const btn = document.createElement('div');
    const st  = systemState(sys);

    let level;
    if (NAS_TYPES.has((sys.device_type || '').toLowerCase())) {
        const vols = sumVolumes(sys);
        level = levelClass(vols.total > 0 ? (vols.used / vols.total) * 100 : null, THRESHOLDS.disk);
    } else {
        const m = sys.metrics || {};
        const cpuLevel = levelClass(m.cpu_percent, THRESHOLDS.cpu);
        const ramLevel = levelClass(m.ram_percent, THRESHOLDS.ram);
        level = [cpuLevel, ramLevel].includes('danger') ? 'danger'
              : [cpuLevel, ramLevel].includes('warning') ? 'warning' : '';
    }

    btn.className = ['sys-btn', level, st.state === 'ok' ? '' : st.state].filter(Boolean).join(' ');
    btn.title = st.state === 'unknown' ? 'Metrics unavailable right now (Manager call failed)'
              : st.ageMs === null      ? 'No reports yet'
              : 'Last report ' + formatAge(st.ageMs) + ' ago';
    btn.innerHTML = buildSummaryButtonHtml(systemName, sys, st);

    // Click scrolls to the full card
    btn.onclick = () => {
        const entry = cardsByName.get(systemName);
        if (entry) entry.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    return btn;
}

function buildSummaryButtonHtml(systemName, sys, st) {
    const badgeHtml = st.state === 'offline' ? '<span class="sys-btn-badge offline">Offline</span>'
                    : st.state === 'stale'   ? '<span class="sys-btn-badge stale">Stale</span>'
                    : st.state === 'unknown' ? '<span class="sys-btn-badge unknown">Unknown</span>'
                    : '';
    let bodyHtml;

    if (NAS_TYPES.has((sys.device_type || '').toLowerCase())) {
        // --- NAS: aggregate bar + used/total label ---
        // Same structure with or without data, for consistent sizing
        const vols   = sumVolumes(sys);
        const hasCap = vols.total > 0;
        const pct    = hasCap ? (vols.used / vols.total) * 100 : null;
        const barCls = levelClass(pct, THRESHOLDS.disk);
        bodyHtml = html`
            <div class="sys-btn-bar-row">
                <div class="usage-bar ${barCls}"><div class="usage-fill" style="width:${fmtWidth(pct)}%"></div></div>
                <span class="sys-btn-pct">${escapeHtml(fmtValue(pct, 0, '%'))}</span>
            </div>
            <div class="sys-btn-stat">${formatBytes(hasCap ? vols.used : null)} / ${formatBytes(hasCap ? vols.total : null)}</div>`;
    } else {
        // --- Server: CPU + RAM sub-tiles (show last readings even if offline) ---
        const m      = sys.metrics || {};
        const cpuCls = levelClass(m.cpu_percent, THRESHOLDS.cpu);
        const ramCls = levelClass(m.ram_percent, THRESHOLDS.ram);
        bodyHtml = html`
            <div class="sys-btn-metrics">
                <div class="sys-btn-metric ${cpuCls}">
                    <div class="sys-btn-metric-label">CPU</div>
                    <div class="sys-btn-metric-val">${escapeHtml(fmtValue(m.cpu_percent, 1, '%'))}</div>
                    <div class="usage-bar ${cpuCls}"><div class="usage-fill" style="width:${fmtWidth(m.cpu_percent)}%"></div></div>
                </div>
                <div class="sys-btn-metric ${ramCls}">
                    <div class="sys-btn-metric-label">RAM</div>
                    <div class="sys-btn-metric-val">${escapeHtml(fmtValue(m.ram_percent, 1, '%'))}</div>
                    <div class="usage-bar ${ramCls}"><div class="usage-fill" style="width:${fmtWidth(m.ram_percent)}%"></div></div>
                </div>
            </div>`;
    }

    const healthHtml = buildHealthGlyphHtml(sys.storage_health);
    return html`<div class="sys-btn-header">${badgeHtml}<div class="sys-btn-name">${escapeHtml(systemName)}</div>${healthHtml}</div>${bodyHtml}`;
}

// Storage-health warning (⚠) or critical (✖) mark next to the name in the strip
function buildHealthGlyphHtml(h) {
    if (!h || managerFeatures.storage_health === false) return '';
    const status = h.overall_status;
    if (status !== 'warning' && status !== 'critical') return '';
    const glyph   = status === 'critical' ? '✖' : '⚠';
    const reasons = Array.isArray(h.status_reasons) ? h.status_reasons.map(String) : [];
    const tip     = ['Storage health: ' + HEALTH_LABELS[status], ...reasons.slice(0, 3)].join('\n');
    const glyphCls = status;
    return html`<span class="sys-btn-health ${glyphCls}" title="${escapeHtml(tip)}">${escapeHtml(glyph)}</span>`;
}

// -------------------------------------------------------------------------
// System cards (updated in place, keyed by system name)
// -------------------------------------------------------------------------

function renderCards(entries) {
    const grid = document.getElementById('nasGrid');
    if (entries.length) clearGridMessage();

    const seen = new Set();
    let prev = null;
    for (const [name, sys] of entries) {
        seen.add(name);
        const st = systemState(sys);

        let entry = cardsByName.get(name);
        if (!entry) {
            const el = document.createElement('div');
            el.dataset.systemName = name;
            entry = { el, html: null };
            cardsByName.set(name, entry);
        }
        entry.el.className = 'nas-card' + (st.state === 'ok' ? '' : ' ' + st.state);

        // Only touch the DOM when the card's content changed; when it did,
        // keep the scroll position of its scrollable lists.
        const cardHtml = buildSystemCardHtml(name, sys, st);
        if (cardHtml !== entry.html) {
            const scroll = saveScrollPositions(entry.el);
            entry.el.innerHTML = cardHtml;
            restoreScrollPositions(entry.el, scroll);
            entry.html = cardHtml;
        }

        // Keep grid order without moving nodes that are already in place
        const expected = prev ? prev.nextSibling : grid.firstChild;
        if (expected !== entry.el) grid.insertBefore(entry.el, expected);
        prev = entry.el;
    }

    for (const [name, entry] of cardsByName) {
        if (!seen.has(name)) {
            entry.el.remove();
            cardsByName.delete(name);
        }
    }
}

function saveScrollPositions(card) {
    return [...card.querySelectorAll('.folder-list, .user-list')].map(el => el.scrollTop);
}

function restoreScrollPositions(card, positions) {
    card.querySelectorAll('.folder-list, .user-list').forEach((el, i) => {
        if (positions[i]) el.scrollTop = positions[i];
    });
}

function buildSystemCardHtml(systemName, sys, st) {
    const deviceType = sys.device_type || 'unknown';
    const isServer   = !NAS_TYPES.has(deviceType.toLowerCase());
    const metricsTs  = sys.metrics ? sys.metrics.timestamp : null;
    const timestampText = metricsTs ? 'Last report: ' + formatTimestamp(metricsTs)
                        : st.state === 'unknown' ? 'Metrics unavailable' : 'No reports yet';
    const tsCls = st.state === 'offline' ? 'offline-timestamp' : st.state === 'stale' ? 'stale-timestamp' : '';

    // Sections carried over from an earlier poll (degraded refresh) are greyed out
    const stale       = sys.staleSections || [];
    const staleClass  = key => stale.includes(key) ? 'stale-data' : '';
    const metricCls   = staleClass('metrics');
    const usersCls    = staleClass('users');
    const diskCls     = staleClass('disk');
    const sessionsCls = staleClass('sessions');
    const healthCls   = staleClass('storage_health');
    const ownersCls   = staleClass('breakdown');
    const usageCls    = staleClass('usage');

    const stateBannerHtml = buildStateBannerHtml(st);
    const noticeHtml      = buildSectionNoticeHtml(sys, isServer);
    const metricsHtml     = buildMetricsHtml(sys.metrics, metricCls);
    const diskIoHtml      = buildDiskIoHtml(sys.metrics, metricCls);
    const gpuHtml         = isServer ? buildGpuHtml((sys.metrics || {}).gpus || [], metricCls) : '';
    const sessionsHtml    = buildSessionsHtml(sys.sessions, sessionsCls);
    const usersHtml       = isServer ? buildUsersHtml(sys.users || [], sys.metrics, usersCls, sys.usage, usageCls) : '';
    const healthHtml      = buildHealthHtml(sys.storage_health, healthCls);
    const diskHtml        = buildDiskHtml(sys.disk, diskCls, healthHtml, sys.breakdown, systemName);
    const ownersHtml      = buildOwnersHtml(sys.breakdown, ownersCls, systemName);
    const emptyHtml       = !sys.metrics && !sys.disk && !sessionsHtml && !healthHtml && !ownersHtml
                            && !(sys.unavailable || []).length
                          ? '<div class="folder-item">No data yet</div>' : '';

    return html`${stateBannerHtml}${noticeHtml}
        <div class="nas-card-header">
            <div>
                <div class="nas-name">${escapeHtml(systemName)}</div>
                <div class="device-type">${escapeHtml(deviceType)}</div>
            </div>
            <div class="timestamp ${tsCls}">${escapeHtml(timestampText)}</div>
        </div>
        ${metricsHtml}
        ${diskIoHtml}
        ${gpuHtml}
        ${sessionsHtml}
        ${usersHtml}
        ${diskHtml}
        ${ownersHtml}
        ${emptyHtml}
    `;
}

function buildStateBannerHtml(st) {
    if (st.state === 'offline') {
        const why = st.ageMs === null ? 'no reports yet' : 'no report for ' + formatAge(st.ageMs);
        return html`<div class="state-banner offline">⚠️ Offline (${escapeHtml(why)})</div>`;
    }
    if (st.state === 'stale') {
        return html`<div class="state-banner stale">⏳ Stale: last report ${escapeHtml(formatAge(st.ageMs))} ago</div>`;
    }
    if (st.state === 'unknown') {
        return '<div class="state-banner unknown">Metrics unavailable right now (the Manager did not answer)</div>';
    }
    return '';
}

// Parts of the card the Manager didn't return this refresh (degraded poll).
// Missing metrics are covered by the 'unknown' state banner.
function buildSectionNoticeHtml(sys, isServer) {
    const shown = key => Object.prototype.hasOwnProperty.call(SECTION_LABELS, key)
                      && (isServer || !SERVER_ONLY_SECTIONS.has(key));
    const stale   = (sys.staleSections || []).filter(shown).map(k => SECTION_LABELS[k]);
    const missing = (sys.unavailable   || []).filter(k => shown(k) && k !== 'metrics').map(k => SECTION_LABELS[k]);
    let out = '';
    if (stale.length) {
        out += html`<div class="section-notice">⏳ Showing ${escapeHtml(stale.join(', '))} from an earlier refresh (the Manager did not answer this time)</div>`;
    }
    if (missing.length) {
        out += html`<div class="section-notice">⚠️ Unavailable right now: ${escapeHtml(missing.join(', '))} (the Manager did not answer)</div>`;
    }
    return out;
}

// --- Metrics section ---
function buildMetricsHtml(m, sectionCls = '') {
    if (!m) return '';
    const u = m.units || {};
    const cpuCls = levelClass(m.cpu_percent, THRESHOLDS.cpu);
    const ramCls = levelClass(m.ram_percent, THRESHOLDS.ram);
    // cpu_percent is the average over interval_s; without one it is a 1-s sample
    const interval   = toNumber(m.interval_s);
    const cpuNoteHtml = interval !== null && interval > 600
        ? html`<span class="metric-note">(avg ${escapeHtml(formatDuration(interval))})</span>` : '';
    const iowaitHtml = toNumber(m.cpu_iowait_percent) !== null ? html`
                <div class="metric-item" data-metric="cpu_iowait_percent" title="Share of CPU time spent waiting for disk I/O">
                    <span class="metric-label">I/O wait</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.cpu_iowait_percent, 1, '%'))}</span>
                </div>` : '';
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">System</div>
            <div class="metrics-stats">
                <div class="metric-item ${cpuCls}" data-metric="cpu_percent" title="${escapeHtml(cpuTooltip(m))}">
                    <span class="metric-label">CPU</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.cpu_percent, 1, u.cpu_percent || '%'))}</span>
                    ${cpuNoteHtml}
                </div>
                <div class="metric-item ${ramCls}" data-metric="ram_percent">
                    <span class="metric-label">RAM</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.ram_percent, 1, u.ram_percent || '%'))}</span>
                </div>
                ${iowaitHtml}
                <div class="metric-item" data-metric="uptime_seconds">
                    <span class="metric-label">Uptime</span>
                    <span class="metric-value">${escapeHtml(m.uptime_formatted || '—')}</span>
                </div>
                <div class="metric-item" data-metric="network_bandwidth_in_mbps">
                    <span class="metric-label">↓</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.network_bandwidth_in_mbps, 2, u.network_bandwidth_in_mbps || 'Mbps'))}</span>
                </div>
                <div class="metric-item" data-metric="network_bandwidth_out_mbps">
                    <span class="metric-label">↑</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.network_bandwidth_out_mbps, 2, u.network_bandwidth_out_mbps || 'Mbps'))}</span>
                </div>
            </div>
        </div>`;
}

function cpuTooltip(m) {
    const interval = toNumber(m.interval_s);
    const parts = [interval === null ? 'CPU: 1-s sample' : `CPU: average over ${Math.round(interval)} s`];
    const cpus = toNumber(m.cpu_count);
    if (cpus !== null) parts.push(`${Math.round(cpus)} logical CPUs`);
    const steal = toNumber(m.cpu_steal_percent);
    if (steal !== null && steal > 0) parts.push(`steal ${steal.toFixed(1)}%`);
    const coverage = toNumber(m.proc_cpu_coverage_percent);
    if (coverage !== null) parts.push(`${coverage.toFixed(0)}% of it attributed to processes still running`);
    return parts.join(' · ');
}

// --- Disk I/O section (totals over physical disks; hidden when none were measured) ---
function buildDiskIoHtml(m, sectionCls = '') {
    if (!m) return '';
    const fields = ['disk_read_bytes_per_s', 'disk_write_bytes_per_s', 'disk_read_iops', 'disk_write_iops',
                    'disk_await_ms', 'disk_busy_max_percent'];
    if (fields.every(k => toNumber(m[k]) === null)) return '';
    const busyCls = levelClass(m.disk_busy_max_percent, THRESHOLDS.diskBusy);
    const busyName = typeof m.disk_busy_max_name === 'string' ? m.disk_busy_max_name : '';
    const busyNameHtml = busyName ? html`<span class="metric-note">${escapeHtml(busyName)}</span>` : '';
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">Disk I/O</div>
            <div class="metrics-stats">
                <div class="metric-item" data-metric="disk_read_bytes_per_s" title="${escapeHtml(iopsText(m.disk_read_iops, 'read'))}">
                    <span class="metric-label">Read</span>
                    <span class="metric-value">${escapeHtml(formatRate(m.disk_read_bytes_per_s))}</span>
                </div>
                <div class="metric-item" data-metric="disk_write_bytes_per_s" title="${escapeHtml(iopsText(m.disk_write_iops, 'write'))}">
                    <span class="metric-label">Write</span>
                    <span class="metric-value">${escapeHtml(formatRate(m.disk_write_bytes_per_s))}</span>
                </div>
                <div class="metric-item ${busyCls}" data-metric="disk_busy_max_percent" title="Busiest physical disk: share of the interval it was busy">
                    <span class="metric-label">Busiest</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.disk_busy_max_percent, 0, '%'))}</span>
                    ${busyNameHtml}
                </div>
                <div class="metric-item" data-metric="disk_await_ms" title="Average time per read or write request">
                    <span class="metric-label">Latency</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.disk_await_ms, 1, 'ms'))}</span>
                </div>
            </div>
        </div>`;
}

function iopsText(iops, what) {
    return toNumber(iops) === null ? '' : `${safeFixed(iops, 1)} ${what} operations per second`;
}

// --- GPU section (server devices with NVIDIA GPUs only) ---
function buildGpuHtml(gpus, sectionCls = '') {
    if (!gpus.length) return '';
    const rowsHtml = gpus.map((g, idx) => {
        const used    = toNumber(g.vram_used_bytes);
        const total   = toNumber(g.vram_total_bytes);
        const vramPct = used !== null && total > 0 ? (used / total) * 100 : null;
        const gpuCls  = levelClass(g.gpu_percent, THRESHOLDS.gpu);
        const vramCls = levelClass(vramPct, THRESHOLDS.vram);
        const vramText = used !== null
            ? formatBytes(used) + ' / ' + formatBytes(total)
            : (g.vram_used_formatted || '—') + ' / ' + (g.vram_total_formatted || '—');
        const tempHtml  = toNumber(g.temperature_c) !== null ? html` &bull; ${safeFixed(g.temperature_c, 0)}\u00b0C` : '';
        const powerHtml = toNumber(g.power_watts)   !== null ? html` &bull; ${safeFixed(g.power_watts, 0)}W` : '';
        const usersHtml = buildGpuUsersHtml(g);
        // GPU list position matches the collector's flat field names
        // (e.g., "gpu_0_percent", "gpu_0_vram_used")
        return html`
            <div class="gpu-row">
                <div class="gpu-name">${escapeHtml(g.name || 'GPU ' + idx)}${tempHtml}${powerHtml}</div>
                <div class="metrics-stats">
                    <div class="metric-item ${gpuCls}" data-metric="gpu_${fmtInt(idx)}_percent">
                        <span class="metric-label">GPU</span>
                        <span class="metric-value">${escapeHtml(fmtValue(g.gpu_percent, 1, '%'))}</span>
                    </div>
                    <div class="metric-item ${vramCls}" data-metric="gpu_${fmtInt(idx)}_vram_used">
                        <span class="metric-label">VRAM</span>
                        <span class="metric-value">${escapeHtml(vramText)}</span>
                    </div>
                </div>
                ${usersHtml}
            </div>`;
    }).join('');
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">GPU</div>
            ${rowsHtml}
        </div>`;
}

// Who holds a GPU: "alice 20.00 GiB 87% · bob 1.10 GiB 3% · +2"; the tooltip
// lists everyone plus VRAM no user could be found for.  Nothing when the
// collector had no per-process data for this GPU (users null or absent).
function buildGpuUsersHtml(g) {
    const users = Array.isArray(g.users) ? g.users.filter(u => u && typeof u === 'object') : [];
    const other = toNumber(g.unattributed_vram_bytes);
    const truncated = Math.max(0, toNumber(g.users_truncated) || 0);
    if (!users.length && !(other > 0)) return '';

    const describe = (u, name) => {
        const vram = toNumber(u.vram_bytes);
        const util = toNumber(u.util_percent);
        return [name, vram !== null ? formatBytes(vram) : null, util !== null ? util.toFixed(0) + '%' : null]
            .filter(Boolean).join(' ');
    };
    const shown = users.slice(0, 2).map(u => describe(u, shortUser(u.username)));
    const more  = users.length - 2 + truncated;
    if (more > 0) shown.push(`+${more}`);
    if (!users.length) shown.push('other ' + formatBytes(other));

    const tip = users.map(u => describe(u, String(u.username ?? '')));
    if (truncated > 0) tip.push(`and ${truncated} more users`);
    if (other > 0) {
        const procs = toNumber(g.unattributed_processes);
        tip.push('other ' + formatBytes(other) + (procs > 0 ? ` (${Math.round(procs)} processes, owner unknown or excluded)` : ''));
    }
    return html`<div class="gpu-users" title="${escapeHtml(tip.join('\n'))}">${escapeHtml(shown.join(' · '))}</div>`;
}

// --- User activity section (server devices only) ---
function buildUsersHtml(users, metrics, sectionCls = '', usage = null, usageCls = '') {
    const usageHtml = buildUsageHtml(usage, usageCls);
    if (!users.length) {
        return usageHtml ? html`
            <div class="card-section ${sectionCls}">
                <div class="section-label">User Activity</div>
                ${usageHtml}
            </div>` : '';
    }
    // Sort by CPU usage descending, show all users
    const sorted   = [...users].sort((a, b) => (toNumber(b.cpu_percent) || 0) - (toNumber(a.cpu_percent) || 0));
    const totalRam = toNumber(metrics && metrics.total_ram_bytes);
    // Cores and GPU columns only on hosts whose collector measures them, so
    // cards fed by an older Manager or collector keep their 3-column table
    const showCores = users.some(u => toNumber(u.cpu_cores) !== null || toNumber(u.cpu_seconds) !== null);
    const showGpu   = users.some(u => toNumber(u.gpus_held) !== null);
    const totalVram = ((metrics && Array.isArray(metrics.gpus)) ? metrics.gpus : [])
        .reduce((sum, g) => sum + (toNumber(g && g.vram_total_bytes) || 0), 0);

    const rowsHtml = sorted.map(u => {
        const cpuCls   = levelClass(u.cpu_percent, THRESHOLDS.cpu);
        // Color RAM by its share of total system RAM
        const ramBytes = toNumber(u.ram_bytes);
        const ramCls   = totalRam > 0 && ramBytes !== null ? levelClass((ramBytes / totalRam) * 100, THRESHOLDS.ram) : '';
        const fullName = String(u.username ?? '');
        const ramText  = ramBytes !== null ? formatBytes(ramBytes) : (u.ram_formatted || '—');
        const cpuAttrsHtml   = userChartAttrsHtml('cpu_percent', fullName);
        const coresAttrsHtml = userChartAttrsHtml('cpu_cores', fullName);
        const ramAttrsHtml   = userChartAttrsHtml('ram_bytes', fullName);
        const gpuHtml  = showGpu ? buildUserGpuCellHtml(u, totalVram, fullName) : '';
        const coresHtml = showCores
            ? html`<span class="user-cores" title="${escapeHtml(coresTooltip(u))}"${coresAttrsHtml}>${escapeHtml(fmtValue(u.cpu_cores, 2, ''))}</span>`
            : '';
        return html`
            <div class="user-row">
                <span class="user-name" title="${escapeHtml(fullName)}">${escapeHtml(shortUser(fullName))}</span>
                <span class="user-cpu ${cpuCls}"${cpuAttrsHtml}>${escapeHtml(fmtValue(u.cpu_percent, 1, '%'))}</span>
                ${coresHtml}
                <span class="user-ram ${ramCls}"${ramAttrsHtml}>${escapeHtml(ramText)}</span>
                ${gpuHtml}
            </div>`;
    }).join('');

    const scrollCls = sorted.length > 8 ? 'user-list-scrollable' : '';
    const tableCls  = [showCores ? 'users-cores' : '', showGpu ? 'users-gpu' : ''].filter(Boolean).join(' ');
    const coresHeadHtml = showCores ? '<span>Cores</span>' : '';
    const gpuHeadHtml   = showGpu ? '<span class="user-gpu-head">GPU</span>' : '';
    return html`
        <div class="card-section ${sectionCls} ${tableCls}">
            <div class="section-label">User Activity (${fmtInt(sorted.length)})</div>
            <div class="user-header">
                <span>User</span><span>CPU</span>${coresHeadHtml}<span>RAM</span>${gpuHeadHtml}
            </div>
            <div class="user-list ${scrollCls}">${rowsHtml}</div>
            ${usageHtml}
        </div>`;
}

// data-* attributes that open one user's history chart.  Only offered when
// the Manager supports per-user history (it came with the usage endpoint).
function userChartAttrsHtml(metric, username) {
    if (!username || managerFeatures.usage === false) return '';
    return html` data-type="user_metrics" data-metric="${escapeHtml(metric)}" data-entity="${escapeHtml(username)}"`;
}

function coresTooltip(u) {
    const secs = toNumber(u.cpu_seconds);
    const interval = toNumber(u.interval_s);
    if (secs === null) return 'CPU cores in use (needs a second run to measure)';
    return `${secs.toFixed(1)} CPU-seconds` + (interval !== null ? ` in ${Math.round(interval)} s` : '');
}

// "1× 87% · 20.00 GiB": GPUs held, attributed busy %, VRAM.  — when the
// collector couldn't attribute GPUs for this user, an empty cell when the
// user holds none (a measured zero).
function buildUserGpuCellHtml(u, totalVram, username) {
    const held = toNumber(u.gpus_held);
    if (held === null) return '<span class="user-gpu">—</span>';
    if (held === 0)    return '<span class="user-gpu muted"></span>';
    const util    = toNumber(u.gpu_util_percent);
    const vram    = toNumber(u.gpu_vram_bytes);
    const utilCls = levelClass(util, THRESHOLDS.gpu);
    const vramCls = totalVram > 0 && vram !== null ? levelClass((vram / totalVram) * 100, THRESHOLDS.vram) : '';
    const utilAttrsHtml = userChartAttrsHtml('gpu_util_percent', username);
    const vramAttrsHtml = userChartAttrsHtml('gpu_vram_bytes', username);
    const tip = `${Math.round(held)} GPU(s) held` + (toNumber(u.gpu_processes) !== null ? `, ${Math.round(u.gpu_processes)} compute processes` : '');
    return html`<span class="user-gpu" title="${escapeHtml(tip)}"><span class="${utilCls}"${utilAttrsHtml}>${fmtInt(held)}× ${escapeHtml(fmtValue(util, 0, '%'))}</span> · <span class="${vramCls}"${vramAttrsHtml}>${formatBytes(vram)}</span></span>`;
}

// "Last 24 h  alice 18.2 h busy / 23.9 h held · bob ...": top 5 by GPU busy
// hours, then held hours, when someone used a GPU in the window, otherwise by
// CPU core-hours. GPU hosts report measured zeros for users who held no GPU;
// those rows would only be "0.0 h busy / 0.0 h held", so they don't count as
// GPU use. A user who held a GPU whose use couldn't be attributed (no
// utilisation reading, or a busy GPU shared without process samples) has
// null busy hours: listed as "— busy / 2.0 h held", never left out.
function buildUsageHtml(usage, usageCls = '') {
    if (!usage || managerFeatures.usage === false) return '';
    const users = Array.isArray(usage.users) ? usage.users.filter(u => u && typeof u === 'object') : [];
    const usedGpu = u => toNumber(u.gpu_busy_hours) > 0 || toNumber(u.gpu_held_hours) > 0;
    const gpu   = users.some(usedGpu);
    // descending by key, a null value after every number
    const desc  = (a, b, key) => {
        const x = toNumber(a[key]), y = toNumber(b[key]);
        return (x === null) - (y === null) || (y || 0) - (x || 0);
    };
    const top   = (gpu
        ? users.filter(usedGpu).sort((a, b) => desc(a, b, 'gpu_busy_hours') || desc(a, b, 'gpu_held_hours'))
        : users.filter(u => toNumber(u.cpu_core_hours) !== null).sort((a, b) => desc(a, b, 'cpu_core_hours'))
    ).slice(0, 5);
    if (!top.length) return '';
    const parts = top.map(u => gpu
        ? `${shortUser(u.username)} ${fmtValue(u.gpu_busy_hours, 1, 'h')} busy / ${fmtValue(u.gpu_held_hours, 1, 'h')} held`
        : `${shortUser(u.username)} ${fmtValue(u.cpu_core_hours, 1, '')} core-h`);
    const windowText = typeof usage.window === 'string' ? usage.window.replace(/^(\d+)([a-z]+)$/, '$1 $2') : '24 h';
    const tip = (gpu ? 'GPU busy / held hours' : 'CPU core-hours') + ` per user over the last ${windowText}`
              + (gpu ? '. Busy hours count only the runs whose GPU use could be attributed ("—": none could be)' : '')
              + (usage.complete === false ? ' (history still being summarised; totals may be low)' : '');
    return html`<div class="usage-line ${usageCls}" title="${escapeHtml(tip)}"><span class="usage-label">Last ${escapeHtml(windowText)}</span> ${escapeHtml(parts.join(' · '))}</div>`;
}

// --- Connected section (every device): who is connected right now ---
// Header counts and per-service chips come from every run; the list is sent
// when it changes plus an hourly heartbeat, so it ages on list_confirmed_at.
const SOURCE_NAMES = {
    tcp: 'Connection counts', utmp: 'Login sessions', logind: 'Login sessions', smb: 'SMB user names',
    snmp: 'Service user counts', wts: 'Remote Desktop sessions', netsession: 'SMB sessions',
};
const SOURCE_STATUS_TEXT = {
    ok: 'ok', partial: 'partly available', not_permitted: 'not permitted for the collector account',
    unavailable: 'not available on this system', stale: 'stale', unsafe: 'rejected (unsafe helper file)',
    timeout: 'timed out', error: 'failed', off: 'turned off in the collector config',
};

function buildSessionsHtml(s, sectionCls = '') {
    if (!s || typeof s !== 'object' || managerFeatures.sessions === false) return '';
    const summary = (s.summary && typeof s.summary === 'object') ? s.summary : {};
    const sources = (summary._sources && typeof summary._sources === 'object') ? summary._sources : {};
    const now = Date.now();

    // Header: "Connected — 9 clients · 1 user", greyed out when the counts are old
    const counts = [];
    if (toNumber(summary.clients_total) !== null) counts.push(plural(summary.clients_total, 'client'));
    if (toNumber(summary.users_total) !== null)   counts.push(plural(summary.users_total, 'user'));
    const countsTs  = parseTs(s.timestamp);
    const countsOld = countsTs !== null && now - countsTs > SESSIONS_AGED_MS;
    const countsCls = countsOld ? 'aged' : '';
    const asOfHtml  = countsOld ? html` <span class="as-of">as of ${escapeHtml(formatHm(countsTs))}</span>` : '';
    const sourceTip = Object.entries(sources)
        .map(([src, status]) => `${SOURCE_NAMES[src] || src}: ${SOURCE_STATUS_TEXT[status] || status}`).join('\n');

    const chipsHtml = buildSessionChipsHtml(summary);
    const listHtml  = summary._detail === 'counts' ? '' : buildSessionListHtml(s, sources, now);
    if (!counts.length && !chipsHtml && !listHtml) return '';
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label ${countsCls}" title="${escapeHtml(sourceTip)}">Connected${escapeHtml(counts.length ? ' — ' + counts.join(' · ') : '')}${asOfHtml}</div>
            ${chipsHtml}
            ${listHtml}
        </div>`;
}

// "SMB 7 · NFS 1 · SSH 1": one chip per service, opening its count history
function buildSessionChipsHtml(summary) {
    const services = new Map();    // svc -> { clients, users }
    for (const [key, val] of Object.entries(summary)) {
        const m = /^([a-z][a-z0-9]{0,15})_(clients|users)$/.exec(key);
        if (!m || toNumber(val) === null) continue;
        const entry = services.get(m[1]) || {};
        entry[m[2]] = toNumber(val);
        services.set(m[1], entry);
    }
    const chips = [...services.entries()]
        .sort((a, b) => ((b[1].clients ?? b[1].users) - (a[1].clients ?? a[1].users)) || a[0].localeCompare(b[0]));
    return chips.map(([svc, c]) => {
        const kind   = c.clients !== undefined ? 'clients' : 'users';
        const metric = `${svc}_${kind}`;
        const zeroCls = c[kind] === 0 ? 'zero' : '';
        const tipParts = [];
        if (c.clients !== undefined) tipParts.push(plural(c.clients, 'client'));
        if (c.users !== undefined)   tipParts.push(plural(c.users, 'user'));
        if (svc === 'rdp' && toNumber(summary.rdp_disconnected) > 0) tipParts.push(`${fmtInt(summary.rdp_disconnected)} disconnected`);
        const tip = `${svc.toUpperCase()}: ${tipParts.join(', ')}`;
        return html`<span class="session-chip ${zeroCls}" data-type="sessions" data-metric="${escapeHtml(metric)}" title="${escapeHtml(tip)}">${escapeHtml(svc.toUpperCase())} ${fmtInt(c[kind])}</span>`;
    }).join('');
}

function buildSessionListHtml(s, sources, now) {
    const listTs = parseTs(s.list_confirmed_at) ?? parseTs(s.list_timestamp) ?? parseTs(s.timestamp);
    const age    = listTs === null ? null : now - listTs;
    if (age !== null && age > SESSIONS_HIDE_MS) {
        return html`<div class="session-note">List not reported since ${escapeHtml(formatShortTime(listTs))}</div>`;
    }
    const rows = groupSessionRows(Array.isArray(s.sessions) ? s.sessions : []);
    if (!rows.length) return '';

    // A "—" user: say why names are missing when a source wasn't permitted
    const blocked = Object.entries(sources).filter(([, status]) => status === 'not_permitted').map(([src]) => src);
    const noNameTip = blocked.length
        ? blocked.map(src => `${SOURCE_NAMES[src] || src} need root access, which the collector account does not have`).join('\n')
        : 'No user name for this connection: only the client address is known';

    const rowsHtml = rows.map(g => {
        const userHtml = g.user
            ? html`<span class="user-name" title="${escapeHtml(g.user)}">${escapeHtml(shortUser(g.user))}</span>`
            : html`<span class="user-name muted" title="${escapeHtml(noNameTip)}">—</span>`;
        const viaTip    = plural(g.connections, 'connection');
        // The cells are narrow: the full address and start time are in the tooltips
        const clientTip = [g.client, g.host].filter(Boolean).join(' · ');
        const sinceTip  = g.since === null ? '' : formatDateTime(g.since);
        return html`
            <div class="user-row session-row">
                ${userHtml}
                <span class="session-client" title="${escapeHtml(clientTip)}">${escapeHtml(g.client || g.host || '—')}</span>
                <span class="session-via" title="${escapeHtml(viaTip)}">${escapeHtml(g.via.join(', '))}</span>
                <span class="session-since" title="${escapeHtml(sinceTip)}">${escapeHtml(g.since === null ? '—' : formatSince(g.since))}</span>
            </div>`;
    }).join('');

    const agedCls   = age !== null && age > SESSIONS_AGED_MS ? 'aged' : '';
    const asOfHtml  = agedCls ? html`<div class="session-note">as of ${escapeHtml(formatHm(listTs))}</div>` : '';
    const cutHtml   = (s.summary || {}).sessions_truncated === true
        ? '<div class="session-note">List shortened by the collector; the counts above are complete</div>' : '';
    const scrollCls = rows.length > 8 ? 'user-list-scrollable' : '';
    return html`
        <div class="session-list ${agedCls}">
            ${asOfHtml}
            <div class="user-header session-row">
                <span>User</span><span>Client</span><span>Via</span><span>Since</span>
            </div>
            <div class="user-list ${scrollCls}">${rowsHtml}</div>
            ${cutHtml}
        </div>`;
}

// One row per (user, client): the services used, the earliest login or
// connect time, and the connections folded in.  Named users first.
function groupSessionRows(list) {
    const groups = new Map();
    for (const r of list) {
        if (!r || typeof r !== 'object') continue;
        const user   = typeof r.username    === 'string' ? r.username    : '';
        const client = typeof r.client      === 'string' ? r.client      : '';
        const host   = typeof r.client_host === 'string' ? r.client_host : '';
        const key = [user, client, host].join('\0');
        let g = groups.get(key);
        if (!g) {
            g = { user, client, host, via: [], since: null, connections: 0 };
            groups.set(key, g);
        }
        const svc = (typeof r.service === 'string' && r.service ? r.service : '?').toUpperCase()
                  + (r.state === 'disconnected' ? ' (disc.)' : '');
        if (!g.via.includes(svc)) g.via.push(svc);
        const since = parseTs(r.since);
        if (since !== null && (g.since === null || since < g.since)) g.since = since;
        g.connections += toNumber(r.connections) || 0;
    }
    return [...groups.values()].sort((a, b) =>
        (!a.user - !b.user) || a.user.localeCompare(b.user) || a.client.localeCompare(b.client));
}

// --- Storage sections: volume buttons, then shared folders ---
// healthHtml (the storage-health line) goes at the top of the first storage
// section; breakdown adds a cold-data bar to each folder it measured.
function buildDiskHtml(d, sectionCls = '', healthHtml = '', breakdown = null, systemName = '') {
    if (!d) {
        return healthHtml ? html`
            <div class="card-section">
                <div class="section-label">Storage</div>
                ${healthHtml}
            </div>` : '';
    }
    const scanHtml = healthHtml + buildDiskScanHtml(d);

    // Volume buttons - one per volume, "volume1: XX of YY" + progress bar
    let volHtml = '';
    for (const vol of (d.volumes || [])) {
        const path     = String(vol.path ?? '');
        const volLabel = path.replace(/^\/(?=.)/, '');  // strip leading slash; Linux "/" stays "/"
        const used     = toNumber(vol.usage_bytes);
        const total    = toNumber(vol.total_bytes);
        if (total !== null && total > 0) {
            const pct    = used !== null ? Math.min(100, (used / total) * 100) : null;
            const volCls = levelClass(pct, THRESHOLDS.disk);
            volHtml += html`
                <div class="volume-btn" data-volume="${escapeHtml(path)}">
                    <div class="volume-btn-label">
                        <span class="volume-btn-name">${escapeHtml(volLabel)}</span>
                        <span class="volume-btn-usage">${formatBytes(used)} of ${formatBytes(total)}</span>
                    </div>
                    <div class="usage-bar ${volCls}">
                        <div class="usage-fill" style="width:${fmtWidth(pct)}%"></div>
                    </div>
                </div>`;
        } else {
            // Fallback: no capacity data yet (older collector)
            volHtml += html`
                <div class="folder-item">
                    <span class="folder-path">${escapeHtml(path)}</span>
                    <span class="folder-size">${formatBytes(used)}</span>
                </div>`;
        }
    }

    // Shared folders only - sorted largest to smallest, all shown
    const folders = [...(d.folders || [])].sort(
        (a, b) => (toNumber(b.usage_bytes) || 0) - (toNumber(a.usage_bytes) || 0));
    const cold = new Map();    // folder path -> storage_breakdown folder entry
    if (breakdown && managerFeatures.breakdown !== false && Array.isArray(breakdown.folders)) {
        for (const bf of breakdown.folders) if (bf && typeof bf.path === 'string') cold.set(bf.path, bf);
    }
    let folderHtml = '';
    for (const f of folders) {
        const coldHtml   = buildColdBarHtml(cold.get(f.path), breakdown);
        const ownersHtml = buildFolderOwnersHtml(systemName, f.path);
        folderHtml += html`
            <div class="folder-item">
                <span class="folder-path">${escapeHtml(f.path)}</span>
                <span class="folder-size">${formatBytes(f.usage_bytes)}</span>
                ${coldHtml}
            </div>${ownersHtml}`;
    }

    if (!volHtml && !folderHtml) {
        return html`
            <div class="card-section ${sectionCls}">
                <div class="section-label">Storage</div>
                ${scanHtml}
            </div>`;
    }

    // The scan age/warnings go in whichever storage section comes first
    const overviewHtml = volHtml ? html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">Storage Overview</div>
            ${scanHtml}
            ${volHtml}
        </div>` : '';
    const folderScanHtml = volHtml ? '' : scanHtml;
    const foldersHtml = folderHtml ? html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">Storage</div>
            ${folderScanHtml}
            <div class="folder-list">
                ${folderHtml}
            </div>
        </div>` : '';
    return overviewHtml + foldersHtml;
}

// Disk snapshot age (flagged when older than diskStaleMs) plus folders the
// collector couldn't measure (_timed_out / _errors; omitted, not shown as 0)
function buildDiskScanHtml(d) {
    const ts = parseTs(d.timestamp);
    let scanText;
    let scanCls = '';
    if (ts === null) {
        scanText = 'Disk scan time unknown';
        scanCls  = 'old';
    } else {
        const age = Date.now() - ts;
        scanText = 'Disk scan ' + formatAge(age) + ' ago';
        if (age > diskStaleMs) {
            scanText = '⚠️ ' + scanText + ' (expected daily)';
            scanCls  = 'old';
        }
    }
    const titleText = d.timestamp ? formatTimestamp(d.timestamp) : '';

    let warnHtml = '';
    const timedOut = Array.isArray(d.timed_out) ? d.timed_out : [];
    const failed   = Array.isArray(d.errors)    ? d.errors    : [];
    if (timedOut.length) {
        warnHtml += html`<div class="scan-warning" title="${escapeHtml(timedOut.join('\n'))}">⚠️ ${fmtInt(timedOut.length)} folder(s) timed out in the last scan and are not shown: ${escapeHtml(summarizeList(timedOut))}</div>`;
    }
    if (failed.length) {
        warnHtml += html`<div class="scan-warning" title="${escapeHtml(failed.join('\n'))}">⚠️ ${fmtInt(failed.length)} folder(s) could not be scanned and are not shown: ${escapeHtml(summarizeList(failed))}</div>`;
    }
    return html`<div class="disk-scan ${scanCls}" title="${escapeHtml(titleText)}">${escapeHtml(scanText)}</div>${warnHtml}`;
}

function summarizeList(items, max = 3) {
    const shown = items.slice(0, max).map(String).join(', ');
    return items.length > max ? `${shown} and ${items.length - max} more` : shown;
}

// --- Storage health line (top of the Storage section) ---
const HEALTH_LABELS = { ok: 'Healthy', warning: 'Warning', critical: 'Critical', unknown: 'Unknown' };
const HEALTH_SOURCE_NAMES = {
    md: 'RAID (md)', sysfs: 'Disk state', mounts: 'Volumes', hwmon: 'Temperatures', zpool: 'ZFS pools',
    snmp: 'SNMP', smart: 'SMART', windows_storage: 'Windows storage', events: 'Event log',
};

// "Healthy · 4 disks · md2 clean · 34–41 °C", or the status and its first
// reason; up to 5 rows for components that are not ok; disk temperatures
// that open their history.  Greyed out when older than HEALTH_STALE_MS.
function buildHealthHtml(h, sectionCls = '') {
    if (!h || typeof h !== 'object' || managerFeatures.storage_health === false) return '';
    const status  = HEALTH_LABELS[h.overall_status] ? h.overall_status : 'unknown';
    const reasons = Array.isArray(h.status_reasons) ? h.status_reasons.filter(r => typeof r === 'string') : [];
    const rowsOf  = list => (Array.isArray(list) ? list : []).filter(x => x && typeof x === 'object');
    const disks   = rowsOf(h.disks);
    const arrays  = rowsOf(h.arrays);
    const volumes = rowsOf(h.volumes);

    const summary = status !== 'ok' && reasons.length ? reasons[0] : healthSummaryText(disks, arrays);
    const ts      = parseTs(h.timestamp);
    const stale   = ts === null || Date.now() - ts > HEALTH_STALE_MS;
    const staleText = ts === null ? 'time unknown' : 'as of ' + formatShortTime(ts);
    const staleHtml = stale ? html` <span class="as-of">(${escapeHtml(staleText)})</span>` : '';

    const sources = (h.sources && typeof h.sources === 'object') ? h.sources : {};
    const tip = [...reasons,
                 ...Object.entries(sources).map(([src, st]) =>
                     `${HEALTH_SOURCE_NAMES[src] || src}: ${SOURCE_STATUS_TEXT[st] || st}`)].join('\n');

    // Components that are not ok, worst first
    const rank = { critical: 0, warning: 1 };
    const problems = [
        ...arrays.map(a => ({ status: a.status, text: describeArray(a) })),
        ...disks.map(dk => ({ status: dk.status, text: describeDisk(dk) })),
        ...volumes.map(v => ({ status: v.status, text: describeVolume(v) })),
    ].filter(p => p.status in rank).sort((a, b) => rank[a.status] - rank[b.status]);
    const problemsHtml = problems.slice(0, 5).map(p => {
        const rowCls = p.status;
        return html`<div class="health-row ${rowCls}">${escapeHtml(p.text)}</div>`;
    }).join('');

    const tempKeys  = diskTempMetrics(disks);
    const tempsHtml = disks.filter(dk => toNumber(dk.temperature_c) !== null).map(dk => {
        const tempCls = dk.status === 'critical' || dk.status === 'warning' ? dk.status : '';
        const label   = `${String(dk.id ?? '?')} ${safeFixed(dk.temperature_c, 0)}°C`;
        // Only disks the collector kept a history key for open a chart
        const attrsHtml = tempKeys.has(dk)
            ? html` data-type="storage_health" data-metric="${escapeHtml(tempKeys.get(dk))}"` : '';
        return html`<span class="health-temp ${tempCls}"${attrsHtml}>${escapeHtml(label)}</span>`;
    }).join('');
    const tempsRowHtml = tempsHtml ? html`<div class="health-temps">${tempsHtml}</div>` : '';

    const pillCls  = status;
    const staleCls = stale ? 'aged' : '';
    return html`
        <div class="storage-health ${sectionCls} ${staleCls}">
            <div class="health-line" title="${escapeHtml(tip)}">
                <span class="health-pill ${pillCls}">${escapeHtml(HEALTH_LABELS[status])}</span>
                <span class="health-text">${escapeHtml(summary ? ' · ' + summary : '')}</span>${staleHtml}
            </div>
            ${problemsHtml}
            ${tempsRowHtml}
        </div>`;
}

function healthSummaryText(disks, arrays) {
    const parts = [];
    if (disks.length) parts.push(plural(disks.length, 'disk'));
    for (const a of arrays.filter(a => a.role !== 'swap' && a.role !== 'system').slice(0, 3)) {
        const idle = a.sync_action === null || a.sync_action === undefined || a.sync_action === 'idle';
        parts.push(String(a.id ?? '?') + ' ' + (a.status === 'ok' && idle ? 'clean'
            : [a.sync_action || a.state || a.status, fmtValue(a.sync_percent, 1, '%')].filter(x => x && x !== '—').join(' ')));
    }
    const temps = disks.map(dk => toNumber(dk.temperature_c)).filter(t => t !== null);
    if (temps.length) {
        const lo = Math.min(...temps).toFixed(0);
        const hi = Math.max(...temps).toFixed(0);
        parts.push(lo === hi ? `${lo} °C` : `${lo}–${hi} °C`);
    }
    return parts.join(' · ');
}

function describeArray(a) {
    const members = toNumber(a.members_total) !== null && toNumber(a.members_active) !== null
        ? `${fmtInt(a.members_active)} of ${fmtInt(a.members_total)} members` : null;
    const sync = a.sync_action && a.sync_action !== 'idle'
        ? [a.sync_action, fmtValue(a.sync_percent, 1, '%'), toNumber(a.sync_eta_seconds) !== null
            ? 'ETA ' + formatDuration(a.sync_eta_seconds) : null].filter(x => x && x !== '—').join(' ') : null;
    return [`${String(a.id ?? '?')}${a.level ? ' ' + a.level : ''}: ${a.status || 'unknown'}`, a.state, sync, members]
        .filter(Boolean).join(', ');
}

function describeDisk(dk) {
    const counters = [['reallocated_sectors', 'reallocated'], ['pending_sectors', 'pending'],
                      ['uncorrectable_sectors', 'uncorrectable'], ['io_errors', 'I/O errors']]
        .filter(([k]) => toNumber(dk[k]) > 0).map(([k, label]) => `${fmtInt(dk[k])} ${label}`);
    return [`Disk ${String(dk.id ?? '?')}${dk.model ? ' (' + dk.model + ')' : ''}: ${dk.status || 'unknown'}`,
            toNumber(dk.temperature_c) !== null ? safeFixed(dk.temperature_c, 0) + ' °C' : null,
            toNumber(dk.wear_used_percent) !== null ? safeFixed(dk.wear_used_percent, 0) + '% worn' : null,
            ...counters].filter(Boolean).join(', ');
}

function describeVolume(v) {
    return [`Volume ${String(v.id ?? '?')}: ${v.status || 'unknown'}`, v.read_only === true ? 'read-only' : null,
            typeof v.health === 'string' ? v.health : null].filter(Boolean).join(', ');
}

// The collector's flattened history key for a disk temperature
// (storage_health.sid): the id lowercased, anything outside [a-z0-9] as "_",
// leading and trailing "_" stripped, at most 32 characters, "root" if empty
function diskTempMetric(id) {
    const sid = String(id).toLowerCase().replace(/[^a-z0-9]/gu, '_').replace(/^_+|_+$/g, '').slice(0, 32);
    return 'disk_' + (sid || 'root') + '_temperature_c';
}

// Map(disk -> history key) for the disks whose temperature has history, by
// the collector's rules (storage_health.flatten): only the first
// FLAT_DISK_CAP non-USB disks, and a key two disks would share belongs to
// the first of them with a temperature.
const FLAT_DISK_CAP = 48;
function diskTempMetrics(disks) {
    const keys = new Map();
    const taken = new Set();
    for (const dk of disks.filter(d => d.bus !== 'usb').slice(0, FLAT_DISK_CAP)) {
        if (typeof dk.id !== 'string' || toNumber(dk.temperature_c) === null) continue;
        const key = diskTempMetric(dk.id);
        if (taken.has(key)) continue;
        taken.add(key);
        keys.set(dk, key);
    }
    return keys;
}

// --- Storage owners: cold-data bar per folder, owner lists ---

// Thin bar under a folder: share not modified for 1-3 years (mid) and 3+
// years (dark).  No bar when the cold fields weren't measured - never a 0.
function buildColdBarHtml(f, breakdown) {
    if (!f) return '';
    const bytes = toNumber(f.bytes);
    const c1    = toNumber(f.cold_1y_bytes);
    const c3    = toNumber(f.cold_3y_bytes);
    if (bytes === null || bytes <= 0 || c1 === null || c3 === null) return '';
    const [y1, y3] = coldLabels(breakdown);
    const share = n => (n / bytes * 100).toFixed(0) + '%';
    const tip = [`Not modified ≥${y1}: ${formatBytes(c1)} (${share(c1)})`, `≥${y3} ${formatBytes(c3)}`];
    if (typeof f.top_owner === 'string' && toNumber(f.top_owner_bytes) !== null) {
        tip.push(`top owner ${shortUser(f.top_owner)} (${share(toNumber(f.top_owner_bytes))})`);
    }
    if (typeof f.folder_owner === 'string') tip.push(`folder owner ${shortUser(f.folder_owner)}`);
    // Owner rows exist only where the collector recorded owners (not Windows in v1)
    const owners = breakdown && breakdown.owner_source && breakdown.owner_source !== 'none';
    const attrsHtml = owners ? html` data-folder="${escapeHtml(f.path)}"` : '';
    const text = tip.join(' · ') + (owners ? ' · click for owners' : '');
    return html`<div class="cold-bar" title="${escapeHtml(text)}"${attrsHtml}><div class="cold-3y" style="width:${fmtWidth(c3 / bytes * 100)}%"></div><div class="cold-1y" style="width:${fmtWidth(Math.max(0, c1 - c3) / bytes * 100)}%"></div></div>`;
}

// "1y" / "3y" from cold_thresholds_days ([365, 1095] by default)
function coldLabels(breakdown) {
    const days = (breakdown && Array.isArray(breakdown.cold_thresholds_days)) ? breakdown.cold_thresholds_days : [];
    const label = (d, fallback) => {
        const n = toNumber(d);
        if (n === null || n <= 0) return fallback;
        return n % 365 === 0 ? `${n / 365}y` : `${Math.round(n)}d`;
    };
    return [label(days[0], '1y'), label(days[1], '3y')];
}

// Owner list of one folder, loaded by clicking its cold-data bar
function buildFolderOwnersHtml(systemName, path) {
    const entry = folderOwners.get(systemName + '\0' + path);
    if (!entry) return '';
    if (entry.state === 'loading') return '<div class="folder-owners">Loading owners…</div>';
    if (entry.state === 'error') {
        return html`<div class="folder-owners">Owners unavailable: ${escapeHtml(entry.error)}</div>`;
    }
    if (!entry.owners) return '<div class="folder-owners">No owner data for this folder</div>';
    const rowsHtml = buildOwnerRowsHtml(entry.owners, entry.total, null);
    const otherHtml = buildOtherOwnersHtml(entry.other);
    return html`<div class="folder-owners">${rowsHtml}${otherHtml}</div>`;
}

// "Top storage users (scanned folders)": the device rollup, top 5 then all
function buildOwnersHtml(b, sectionCls = '', systemName = '') {
    if (!b || typeof b !== 'object' || managerFeatures.breakdown === false) return '';
    const device = (b.device && typeof b.device === 'object') ? b.device : null;
    const owners = device && Array.isArray(device.owners)
        ? device.owners.filter(o => o && typeof o === 'object' && typeof o.owner === 'string') : [];
    if (!owners.length) return '';
    const sorted = [...owners].sort((x, y) => (toNumber(y.bytes) || 0) - (toNumber(x.bytes) || 0));
    const open   = expandedOwners.has(systemName);
    const rowsHtml  = buildOwnerRowsHtml(open ? sorted : sorted.slice(0, OWNERS_SHOWN), toNumber(device.bytes), 'storage_breakdown');
    const otherHtml = open || sorted.length <= OWNERS_SHOWN ? buildOtherOwnersHtml(device.other) : '';
    const moreHtml  = sorted.length <= OWNERS_SHOWN ? ''
        : open ? '<div class="owners-more" data-expand="owners">Show fewer</div>'
        : html`<div class="owners-more" data-expand="owners">+${fmtInt(sorted.length - OWNERS_SHOWN)} more</div>`;
    const asOf = b.timestamp ? 'Scan of ' + formatTimestamp(b.timestamp) : '';
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label" title="${escapeHtml(asOf)}">Top storage users (scanned folders)</div>
            <div class="owner-list">${rowsHtml}${otherHtml}${moreHtml}</div>
        </div>`;
}

// owner | size (share of total) | share not modified for a year.  With
// dataType set, each row opens that owner's storage history.
function buildOwnerRowsHtml(owners, total, dataType) {
    return owners.map(o => {
        const name  = String(o.owner ?? '');
        const bytes = toNumber(o.bytes);
        const c1    = toNumber(o.cold_1y_bytes);
        const share = total > 0 && bytes !== null ? ` (${(bytes / total * 100).toFixed(0)}%)` : '';
        const coldText = bytes > 0 && c1 !== null ? `${(c1 / bytes * 100).toFixed(0)}% ≥1y` : '—';
        const attrsHtml = dataType
            ? html` data-type="${escapeHtml(dataType)}" data-metric="${escapeHtml('owner/' + name + '/bytes')}"` : '';
        return html`
            <div class="owner-row"${attrsHtml}>
                <span class="user-name" title="${escapeHtml(name)}">${escapeHtml(shortUser(name))}</span>
                <span class="owner-size">${formatBytes(bytes)}${escapeHtml(share)}</span>
                <span class="owner-cold" title="Share not modified for at least a year">${escapeHtml(coldText)}</span>
            </div>`;
    }).join('');
}

function buildOtherOwnersHtml(other) {
    if (!other || typeof other !== 'object' || !(toNumber(other.owner_count) > 0)) return '';
    return html`
        <div class="owner-row muted">
            <span class="user-name">${escapeHtml(plural(other.owner_count, 'other owner'))}</span>
            <span class="owner-size">${formatBytes(other.bytes)}</span>
            <span class="owner-cold"></span>
        </div>`;
}

function toggleOwnerList(systemName) {
    if (!expandedOwners.delete(systemName)) expandedOwners.add(systemName);
    rerenderCards();
}

// Click on a folder's cold-data bar: load its owners (or close the list)
function toggleFolderOwners(systemName, path) {
    const key = systemName + '\0' + path;
    if (folderOwners.delete(key)) {
        rerenderCards();
        return;
    }
    const entry = { state: 'loading' };
    folderOwners.set(key, entry);
    rerenderCards();
    const qs = new URLSearchParams({ folder: path });
    fetch(`/api/storage/breakdown/${encodeURIComponent(systemName)}?${qs}`, { cache: 'no-store' })
        .then(async r => {
            let body = null;
            try { body = await r.json(); } catch (e) { body = null; }
            if (signInRequired(r, body)) {
                goToSignIn();
                throw new Error('Sign-in required');
            }
            if (!r.ok || !body || body.error) throw new Error((body && body.error) || `HTTP ${r.status}`);
            return body;
        })
        .then(body => {
            if (folderOwners.get(key) !== entry) return;    // closed (or reopened) meanwhile
            folderOwners.set(key, { state: 'ok', ...folderOwnersFrom(body, path) });
            rerenderCards();
        })
        .catch(err => {
            if (folderOwners.get(key) !== entry) return;
            folderOwners.set(key, { state: 'error', error: err.message });
            rerenderCards();
        });
}

// The folder's entry in a /api/storage/breakdown/<sys>?folder= answer
// (the latest run in the message's own shape: folders[] with owner rows)
function folderOwnersFrom(body, path) {
    const folders = Array.isArray(body.folders) ? body.folders.filter(f => f && typeof f === 'object') : [];
    const f = folders.find(x => x.path === path) || (folders.length === 1 ? folders[0] : null);
    if (!f) return { owners: null, other: null, total: null };
    const owners = Array.isArray(f.owners)
        ? f.owners.filter(o => o && typeof o === 'object' && typeof o.owner === 'string')
                  .sort((x, y) => (toNumber(y.bytes) || 0) - (toNumber(x.bytes) || 0))
        : null;
    return { owners, other: f.other, total: toNumber(f.bytes) };
}

// Re-render the cards from the data on screen (after expanding a list)
function rerenderCards() {
    if (!lastGoodData) return;
    try {
        updateDashboard(lastGoodData);
    } catch (err) {
        console.error('Error rendering dashboard data:', err);
    }
}

// -------------------------------------------------------------------------
// History charts (Plotly.js)
// -------------------------------------------------------------------------

// dataType: history data type (default system_metrics); entity: one user of
// user_metrics.  Storage owners are measured daily, so they default to all data.
function showMetricChart(systemName, metricField, metricLabel, dataType = null, entity = null) {
    const range = dataType === 'storage_breakdown' ? 'all' : '24';   // default: last 24 hours
    openChart('metric', systemName, metricField, metricLabel, range, dataType, entity);
}

function showVolumeChart(systemName, volumePath, volumeLabel) {
    // Volumes are measured daily - not many points, so default to all of them.
    // Leading slashes are stripped before encoding; the Manager restores them.
    openChart('volume', systemName, String(volumePath).replace(/^\/+/, ''), volumeLabel, 'all');
}

function openChart(type, systemName, field, label, defaultRange, dataType = null, entity = null) {
    chart.type     = type;
    chart.dataType = dataType;
    chart.entity   = entity;
    chart.system   = systemName;
    chart.field    = field;
    chart.label    = label;
    document.getElementById('chartCallout').classList.add('active');
    applyRange(defaultRange);
}

function closeChart() {
    document.getElementById('chartCallout').classList.remove('active');
    if (chart.controller) chart.controller.abort();
    chart.controller = null;
    chart.seq++;          // drop anything still in flight
    chart.data = null;
    chart.meta = null;
}

function chartTitleText(count, aggregated = false) {
    const what = chart.type === 'volume' ? `${chart.label} Usage` : chart.label;
    const n    = count === undefined ? '' : ` (${count} ${aggregated ? 'points' : 'measurements'})`;
    return `${chart.system} — ${what}${n}`;
}

// Range keys match the data-range attributes of the buttons in index.html
function applyRange(rangeKey) {
    if (!chart.type) return;
    const now = new Date();
    let fromISO;
    let toISO = now.toISOString();

    if (rangeKey === 'all') {
        fromISO = ALL_TIME_FROM;
    } else if (rangeKey === 'custom') {
        const fromVal = document.getElementById('customFrom').value;
        const toVal   = document.getElementById('customTo').value;
        if (!fromVal || !toVal) {
            showChartNotice('Please select both From and To dates');
            return;
        }
        if (fromVal > toVal) {     // YYYY-MM-DD strings compare chronologically
            showChartNotice('The "From" date must be on or before the "To" date');
            return;
        }
        // Date inputs give YYYY-MM-DD; treat as start/end of day in UTC
        fromISO = new Date(fromVal + 'T00:00:00Z').toISOString();
        toISO   = new Date(toVal   + 'T23:59:59Z').toISOString();
    } else {
        const hours = parseFloat(rangeKey);
        if (!(hours > 0)) return;
        fromISO = new Date(now.getTime() - hours * 60 * 60 * 1000).toISOString();
    }

    chart.range = rangeKey;
    document.querySelectorAll('.range-btn[data-range]').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.range === rangeKey);
    });
    requestChart(fromISO, toISO);
}

function requestChart(fromISO, toISO) {
    if (chart.controller) chart.controller.abort();
    const controller = new AbortController();
    chart.controller = controller;
    const seq = ++chart.seq;
    chart.data = null;    // CSV export waits for this selection's data
    chart.meta = null;
    clearChartError();    // an earlier selection's error must not linger

    document.getElementById('chartTitle').textContent = chartTitleText();
    document.getElementById('chartInfo').textContent  = 'Loading…';

    const dataType = chart.dataType || (chart.type === 'volume' ? 'folder_usage' : 'system_metrics');
    const qs = new URLSearchParams({ from: fromISO, to: toISO, max_points: String(maxChartPoints) });
    if (chart.entity) qs.set('entity', chart.entity);
    const url = `/api/history/${encodeURIComponent(chart.system)}/${encodeURIComponent(dataType)}/${encodeURIComponent(chart.field)}?${qs}`;

    fetch(url, { signal: controller.signal })
        .then(async r => {
            let body = null;
            try { body = await r.json(); } catch (e) { body = null; }
            if (signInRequired(r, body)) {
                goToSignIn();
                throw new Error('Sign-in required');
            }
            if (!r.ok || !body || body.error) {
                throw new Error((body && body.error) || `HTTP ${r.status}`);
            }
            return body;
        })
        .then(body => {
            if (seq !== chart.seq) return;   // superseded by a newer selection
            chart.controller = null;
            const points = Array.isArray(body.data) ? body.data : [];
            const resolution = body.resolution === 'hour' || body.resolution === 'day' ? body.resolution : null;
            chart.data = points;
            chart.meta = { downsampled: !!body.downsampled, truncated: !!body.truncated,
                           band: hasBand(points), resolution };
            document.getElementById('chartTitle').textContent = chartTitleText(points.length, resolution !== null);
            if (chart.type === 'volume') {
                renderVolumeChart(chart.label, points);
            } else {
                renderMetricChart(chart.field, chart.label, resolveMetricDisplay(chart.field, chart.label, body.unit), points);
            }
            updateChartInfo(points, body);
        })
        .catch(err => {
            if (err.name === 'AbortError' || seq !== chart.seq) return;
            console.error('Error fetching chart history:', err);
            showChartError(err.message);
        });
}

function showChartNotice(message) {
    document.getElementById('chartInfo').textContent = message;
}

function showChartError(message) {
    const el = document.getElementById('metricChart');
    if (window.Plotly) Plotly.purge(el);
    const p = document.createElement('p');
    p.className = 'chart-error';
    p.textContent = 'Error: ' + message;
    el.replaceChildren(p);
    document.getElementById('chartInfo').textContent = 'No data';
}

// Plotly.purge/newPlot only manage Plotly's own container, so the <p> left
// by showChartError() would otherwise stay below every later chart.
function clearChartError() {
    document.querySelectorAll('#metricChart > .chart-error').forEach(n => n.remove());
}

function plotChart(traces, layout) {
    clearChartError();
    Plotly.newPlot('metricChart', traces, layout, chartConfig());
}

// Resolve display info: API unit takes priority, then field-name map
function resolveMetricDisplay(metricField, metricLabel, apiUnit) {
    const base = apiUnit && BASE_UNIT_DISPLAY[apiUnit];
    if (base) return { ...base, yLabel: base.unit.trim() ? `${metricLabel} (${base.unit.trim()})` : metricLabel };
    if (METRIC_UNITS[metricField]) return METRIC_UNITS[metricField];

    // If no direct match, try GPU pattern matching
    if (/^gpu_\d+_percent$/.test(metricField)) {
        return { yLabel: metricLabel, unit: '%', decimals: 1, scale: 1, beginAtZero: true };
    } else if (/^gpu_\d+_vram_used$/.test(metricField)) {
        return { yLabel: metricLabel, unit: ' GiB', decimals: 2, scale: 1 / 1073741824, beginAtZero: true };
    } else if (/^gpu_\d+_temp_c$/.test(metricField)) {
        return { yLabel: metricLabel, unit: '°C', decimals: 1, scale: 1, beginAtZero: false };
    } else if (/^gpu_\d+_power_w$/.test(metricField)) {
        return { yLabel: metricLabel, unit: ' W', decimals: 1, scale: 1, beginAtZero: true };
    }
    return { yLabel: metricLabel, unit: '', decimals: 2, scale: 1, beginAtZero: true };
}

function chartLayout(yTitle, zeroline) {
    return {
        title: null,  // We use h3 instead
        xaxis: {
            type: 'date',
            rangeslider: { visible: false },
            rangeselector: { buttons: [] },
            title: 'Time',
        },
        yaxis: {
            title: plotlyText(yTitle),
            zeroline: zeroline,
        },
        hovermode: 'x unified',
        margin: { l: 60, r: 20, t: 10, b: 80 },
        plot_bgcolor: 'rgba(245, 245, 245, 0.5)',
        paper_bgcolor: 'white',
    };
}

function chartConfig() {
    return {
        responsive: true,
        displayModeBar: true,
        displaylogo: false,
        modeBarButtons: [
            ['zoom2d', 'pan2d', 'zoomIn2d', 'zoomOut2d', 'autoScale2d', 'resetScale2d'],
            ['toImage', csvModeBarButton()],
            [hoverClosestButton(), hoverCompareButton()],
        ],
    };
}

function renderMetricChart(metricField, metricLabel, info, data) {
    const timestamps = data.map(d => d.timestamp);
    const values = data.map(d => {
        const v = toNumber(d.value);
        return v === null ? null : v * info.scale;
    });

    // Color by metric type
    let color = '#3498db';
    if (metricField === 'cpu_percent' || metricField === 'ram_percent') {
        color = '#e74c3c';
    } else if (metricField.includes('bandwidth')) {
        color = '#27ae60';
    } else if (/^gpu_\d+_(percent|temp_c|power_w)$/.test(metricField)) {
        color = '#f39c12';  // Orange for GPU metrics
    } else if (/^gpu_\d+_vram/.test(metricField)) {
        color = '#9b59b6';  // Purple for VRAM metrics
    }

    const trace = {
        x: timestamps,
        y: values,
        name: plotlyText(metricLabel),
        mode: 'lines+markers',
        line: { color: color, width: 2 },
        marker: { size: 5 },
        fill: 'tozeroy',
        fillcolor: color + '33',
        hovertemplate: '%{fullData.name}: %{y:.' + info.decimals + 'f}' + info.unit + '<extra></extra>',
    };
    if (!hasBand(data)) {
        plotChart([trace], chartLayout(info.yLabel, info.beginAtZero));
        return;
    }

    // Aggregated points (rollups, downsampled raw data): shade min..max
    // behind the average line - max first, then min filled up to it
    // A point without min/max (e.g. one raw measurement) narrows the band to
    // its average, as the hover does
    const scaled = key => data.map((d, i) => {
        const v = toNumber(d[key]);
        return v === null ? values[i] : v * info.scale;
    });
    const mins = scaled('min');
    const maxs = scaled('max');
    const fmt  = '.' + info.decimals + 'f';
    const bandTrace = (x, y, name, fill) => ({
        x, y, name: plotlyText(`${metricLabel} ${name}`), mode: 'lines',
        line: { color: color, width: 0 }, hoverinfo: 'skip', showlegend: false,
        ...(fill ? { fill: 'tonexty', fillcolor: color + '22' } : {}),
    });
    // tonexty joins the ends of a gap with a shaded wedge, so a band broken
    // by points with no value is drawn as one max/min pair per unbroken run
    const bands = bandRuns(mins, maxs).flatMap(([from, to]) => [
        bandTrace(timestamps.slice(from, to), maxs.slice(from, to), 'max', false),
        bandTrace(timestamps.slice(from, to), mins.slice(from, to), 'min', true),
    ]);
    delete trace.fill;
    delete trace.fillcolor;
    if (data.length > 500) trace.mode = 'lines';
    trace.customdata = data.map((d, i) => [mins[i], maxs[i]]);
    trace.hovertemplate = '%{fullData.name}: %{y:' + fmt + '}' + info.unit
                        + ' (%{customdata[0]:' + fmt + '}–%{customdata[1]:' + fmt + '}' + info.unit + ')<extra></extra>';
    plotChart([...bands, trace], chartLayout(info.yLabel, info.beginAtZero));
}

// [from, to) index ranges where both band edges have a value
function bandRuns(mins, maxs) {
    const runs = [];
    let from = null;
    for (let i = 0; i <= mins.length; i++) {
        const ok = i < mins.length && mins[i] !== null && maxs[i] !== null;
        if (ok && from === null) from = i;
        if (!ok && from !== null) {
            runs.push([from, i]);
            from = null;
        }
    }
    return runs;
}

// True when the points are aggregates carrying min and max (newer Managers)
function hasBand(points) {
    return Array.isArray(points) && points.some(d => d && toNumber(d.min) !== null && toNumber(d.max) !== null);
}

function renderVolumeChart(volumeLabel, data) {
    const timestamps = data.map(d => d.timestamp);
    const rawValues  = data.map(d => toNumber(d.value));   // null = not measured (gap), never 0

    // Auto-scale: use TiB if any value >= 1 TiB, else GiB
    const maxBytes = Math.max(0, ...rawValues.filter(v => v !== null));
    const useTB    = maxBytes >= 1099511627776;  // 1 TiB
    const divisor  = useTB ? 1099511627776 : 1073741824;
    const unitStr  = useTB ? 'TiB' : 'GiB';
    const decimals = 2;

    const scaledValues = rawValues.map(v => v === null ? null : +(v / divisor).toFixed(decimals));

    const trace = {
        x: timestamps,
        y: scaledValues,
        name: plotlyText(`${volumeLabel} Used`),
        mode: 'lines+markers',
        line: { color: '#3498db', width: 2 },
        marker: { size: 5 },
        fill: 'tozeroy',
        fillcolor: '#3498db33',
        hovertemplate: '%{fullData.name}: %{y:.' + decimals + 'f} ' + unitStr + '<extra></extra>',
    };

    plotChart([trace], chartLayout(`Usage (${unitStr})`, false));
}

function updateChartInfo(data, body) {
    const infoEl = document.getElementById('chartInfo');
    if (!data || data.length === 0) {
        infoEl.textContent = 'No data';
        return;
    }
    if (body && (body.resolution === 'hour' || body.resolution === 'day'
                 || (body.downsampled && toNumber(body.bucket_seconds) !== null))) {
        infoEl.textContent = aggregateInfoText(data, body);
        return;
    }
    const n = data.length;
    const firstTime = parseTs(data[0].timestamp);
    const lastTime  = parseTs(data[n - 1].timestamp);
    const spanHours = firstTime !== null && lastTime !== null ? (lastTime - firstTime) / (1000 * 60 * 60) : 0;
    let info = `Showing ${n} points spanning ${spanHours.toFixed(1)} hours`;
    // downsampled: bucket-averaged to max_points; truncated: row limit reached
    const notes = [];
    if (body && body.downsampled) {
        const from = toNumber(body.downsampled_from);
        notes.push(from !== null ? `averaged down from ${from} measurements` : 'averaged into time buckets');
    }
    if (body && body.truncated) notes.push('row limit reached, so not every measurement in the range is included');
    if (notes.length) info += ` (${notes.join('; ')}; pick a shorter range for full detail)`;
    infoEl.textContent = info;
}

// e.g. "Hourly averages in 5-hour buckets · 1,752 points from 105,120
// measurements · shaded = min–max", or "Averages in 15-minute buckets · ..."
// for raw data the Manager downsampled
function aggregateInfoText(data, body) {
    const bucket = toNumber(body.bucket_seconds);
    const level  = body.resolution === 'day' ? 86400 : body.resolution === 'hour' ? 3600 : null;
    let text;
    if (level !== null) {
        text = (level === 86400 ? 'Daily' : 'Hourly') + ' averages';
        if (bucket !== null && bucket > level) text += ` in ${formatBucket(bucket)} buckets`;
    } else {
        text = bucket !== null ? `Averages in ${formatBucket(bucket)} buckets` : 'Averages';
    }
    const counts   = data.map(d => toNumber(d.count)).filter(c => c !== null);
    const measured = counts.length ? counts.reduce((a, b) => a + b, 0) : toNumber(body.downsampled_from);
    text += ` · ${fmtCount(data.length)} point${data.length === 1 ? '' : 's'}`;
    if (measured !== null) text += ` from ${fmtCount(measured)} measurements`;
    if (hasBand(data)) text += ' · shaded = min–max';
    if (body.truncated) text += ' · row limit reached, so not every measurement in the range is included';
    return text;
}

// Bucket width as "15-minute", "5-hour" or "2-day"
function formatBucket(seconds) {
    if (seconds % 86400 === 0) return `${seconds / 86400}-day`;
    if (seconds % 3600 === 0)  return `${seconds / 3600}-hour`;
    return `${Math.max(1, Math.round(seconds / 60))}-minute`;
}

function fmtCount(n) {
    return Math.round(n).toLocaleString();
}

// Hover closest point — press again to turn off.
function hoverClosestButton() {
    return {
        name: 'Hover: data point',
        title: 'Show closest data point · press again to hide',
        icon: Plotly.Icons['tooltip_basic'],
        click: function(gd) {
            const current = gd.layout.hovermode;
            Plotly.relayout(gd, { hovermode: current === 'closest' ? false : 'closest' });
        },
    };
}

// Hover x-unified (data point + time) — press again to turn off.
function hoverCompareButton() {
    return {
        name: 'Hover: data point + time',
        title: 'Show data point and time · press again to hide',
        icon: Plotly.Icons['tooltip_compare'],
        click: function(gd) {
            const current = gd.layout.hovermode;
            Plotly.relayout(gd, { hovermode: current === 'x unified' ? false : 'x unified' });
        },
    };
}


// Returns a Plotly modeBar button definition that downloads current chart data as CSV.
function csvModeBarButton() {
    return {
        name: 'Download CSV',
        title: 'Download data as CSV',
        // Download-arrow icon (1000x1000 SVG, flipped to match Plotly coord system)
        icon: {
            width: 1000,
            height: 1000,
            path: 'M450,150 L450,580 L550,580 L550,150 Z M180,530 L500,820 L820,530 L680,530 L680,580 L550,580 L550,580 L450,580 L450,530 Z M150,860 L850,860 L850,960 L150,960 Z',
            transform: 'matrix(1 0 0 -1 0 1000)',
        },
        click: function() { exportChartCSV(); },
    };
}

function exportChartCSV() {
    if (!chart.data || chart.data.length === 0) {
        alert('No chart data to export');
        return;
    }

    // Download
    const blob = new Blob([chartCsvText()], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = csvFilename();
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// CSV of the plotted points.  Bucket-averaged data (long ranges, see
// max_points) is exported as "value_avg" so it can't pass for raw readings.
function chartCsvText() {
    const meta = chart.meta || {};
    const num  = v => { const n = toNumber(v); return n === null ? '' : n; };
    if (meta.band) {
        let csv = 'timestamp,value_avg,value_min,value_max,count\n';
        for (const row of (chart.data || [])) {
            const ts = String(row.timestamp || '').replace(/"/g, '""');
            csv += `"${ts}",${num(row.value)},${num(row.min)},${num(row.max)},${num(row.count)}\n`;
        }
        return csv;
    }
    let csv = meta.downsampled ? 'timestamp,value_avg\n' : 'timestamp,value\n';
    for (const row of (chart.data || [])) {
        const ts  = String(row.timestamp || '').replace(/"/g, '""');
        const val = toNumber(row.value);
        csv += `"${ts}",${val === null ? '' : val}\n`;
    }
    return csv;
}

// e.g. "Triton_cpu_percent_24h_20260821T143000Z.csv"; hourly or daily
// rollups get "_hourly" / "_daily", averaged data "_avg", data cut at the row
// limit "_truncated" (e.g. "..._all_daily_avg_...csv"); a per-user chart
// carries the user name ("Atlas_alice_cpu_cores_...").
function csvFilename() {
    const clean = s => String(s || '').replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/^_+|_+$/g, '') || 'unknown';
    const meta  = chart.meta || {};
    const field = chart.type === 'volume' ? 'volume_' + chart.field : chart.field;
    const who   = chart.entity ? clean(chart.entity) + '_' : '';
    const range = /^\d+$/.test(chart.range || '') ? chart.range + 'h' : chart.range;
    const level = meta.resolution === 'hour' ? '_hourly' : meta.resolution === 'day' ? '_daily' : '';
    const flags = level + (meta.downsampled ? '_avg' : '') + (meta.truncated ? '_truncated' : '');
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
    return `${clean(chart.system)}_${who}${clean(field)}_${clean(range)}${flags}_${stamp}.csv`;
}

// -------------------------------------------------------------------------
// Utilities
// -------------------------------------------------------------------------

// Tag for HTML templates.  It only concatenates - it does NOT escape - but it
// marks the template so the regression test can check every ${...} in it.
function html(strings, ...values) {
    let out = strings[0];
    for (let i = 0; i < values.length; i++) out += values[i] + strings[i + 1];
    return out;
}

function levelClass(value, thresholds) {
    const n = toNumber(value);
    if (n === null) return '';
    if (n >= thresholds.danger) return 'danger';
    if (n >= thresholds.warn)   return 'warning';
    return '';
}

function updateStatus(status, text) {
    const el = document.getElementById('status');
    const labels = { connected: '🟢 Connected', degraded: '🟠 Degraded', error: '🔴 ' + (text || 'Error') };
    el.textContent = labels[status] || status;
    el.className   = `status ${status}`;
}

// Parse a Manager timestamp to epoch ms (null if missing/invalid).  Manager
// timestamps are UTC; ones without a zone designator would otherwise be read
// as browser-local time.
function parseTs(ts) {
    if (ts === null || ts === undefined || ts === '') return null;
    let s = String(ts).trim();
    if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) s = s.replace(' ', 'T') + 'Z';
    const ms = Date.parse(s);
    return isNaN(ms) ? null : ms;
}

function formatTimestamp(ts) {
    const ms = parseTs(ts);
    if (ms === null) return ts ? String(ts) : 'Unknown';
    return new Date(ms).toLocaleString();
}

function formatClock(ts) {
    const ms = parseTs(ts);
    return new Date(ms === null ? Date.now() : ms).toLocaleTimeString();
}

function formatAge(ms) {
    if (ms === null || ms === undefined || !isFinite(ms)) return 'an unknown time';
    const min = Math.max(0, Math.round(ms / 60000));
    if (min < 1)   return 'less than a minute';
    if (min < 120) return `${min} min`;
    const h = Math.round(min / 60);
    if (h < 48)    return `${h} h`;
    return `${Math.round(h / 24)} days`;
}

// Clock time "14:05" of epoch ms
function formatHm(ms) {
    return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// "14:05" today, otherwise date and time
function formatShortTime(ms) {
    const d = new Date(ms);
    return d.toDateString() === new Date().toDateString()
        ? formatHm(ms) : formatDateTime(ms);
}

function formatDateTime(ms) {
    return new Date(ms).toLocaleDateString() + ' ' + formatHm(ms);
}

// For a narrow cell: "14:05" today, "Nov 20" this year, else "Nov 2025"
function formatSince(ms) {
    const d   = new Date(ms);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return formatHm(ms);
    return d.getFullYear() === now.getFullYear()
        ? d.toLocaleDateString([], { month: 'short', day: 'numeric' })
        : d.toLocaleDateString([], { month: 'short', year: 'numeric' });
}

// Seconds as "45s", "12m", "2h 10m" or "3d 4h"
function formatDuration(seconds) {
    const s = Math.max(0, Math.round(toNumber(seconds) || 0));
    if (s < 60)    return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60)    return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 48)    return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
    const d = Math.floor(h / 24);
    return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}

// Bytes per second in decimal units, as charts show B/s ('—' if unmeasured)
function formatRate(bps) {
    const n = toNumber(bps);
    if (n === null) return '—';
    if (n < 1e3) return `${Math.max(0, n).toFixed(0)} B/s`;
    if (n < 1e6) return `${(n / 1e3).toFixed(1)} KB/s`;
    const mb = n / 1e6;
    return `${mb.toFixed(mb >= 100 ? 0 : 2)} MB/s`;
}

// "1 client", "9 clients"
function plural(n, word) {
    return `${fmtInt(n)} ${word}${Math.round(toNumber(n)) === 1 ? '' : 's'}`;
}

// The name part of DOMAIN\user, as the user table shows it
function shortUser(name) {
    return String(name ?? '').split('\\').pop();
}

// Chart title for card items without a .metric-label: per-user metrics,
// session counts, disk temperatures and storage owners
function chartLabelFor(metric, dataType, entity) {
    let m;
    if (dataType === 'storage_breakdown' && (m = /^owner\/(.+)\/bytes$/.exec(metric))) {
        return `${shortUser(m[1])} storage (scanned folders)`;
    }
    if (dataType === 'storage_health' && (m = /^disk_(.+)_temperature_c$/.exec(metric))) {
        return `Disk ${m[1]} temperature`;
    }
    if (dataType === 'sessions' && (m = /^([a-z][a-z0-9]*)_(clients|users)$/.exec(metric))) {
        return `${m[1].toUpperCase()} ${m[2]}`;
    }
    if (entity) return `${shortUser(entity)} — ${FIELD_LABELS[metric] || metric}`;
    return FIELD_LABELS[metric] || null;
}

// Number, or null for null/undefined/''/NaN/non-numeric (unmeasured, contract 4)
function toNumber(val) {
    if (val === null || val === undefined || val === '' || typeof val === 'boolean') return null;
    const n = typeof val === 'number' ? val : parseFloat(val);
    return isFinite(n) ? n : null;
}

// Numeric formatters: output is digits (or '—'), never caller text
function safeFixed(val, decimals) {
    const n = toNumber(val);
    return n === null ? '—' : n.toFixed(decimals);
}

function fmtInt(val) {
    const n = toNumber(val);
    return n === null ? '—' : String(Math.round(n));
}

// Bar width in percent, clamped to 0..100
function fmtWidth(pct) {
    const n = toNumber(pct);
    return n === null ? '0' : String(Math.round(Math.min(100, Math.max(0, n)) * 10) / 10);
}

// Value plus display unit, or '—' when unmeasured.  The unit comes from the
// collector, so the result must still go through escapeHtml().
function fmtValue(val, decimals, unitStr) {
    const n = toNumber(val);
    return n === null ? '—' : n.toFixed(decimals) + unitSuffix(unitStr);
}

// Binary units (1 KiB = 1024 B) everywhere in the dashboard
function formatBytes(bytes) {
    const n = toNumber(bytes);
    if (n === null) return '—';
    let v = Math.max(0, n);
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    for (const unit of units) {
        if (v < 1024) return `${v.toFixed(unit === 'B' ? 0 : 2)} ${unit}`;
        v /= 1024;
    }
    return `${v.toFixed(2)} PiB`;
}

// Resolve a display suffix from a raw unit string using BASE_UNIT_DISPLAY.
// Falls back to ' {unit}' (with space) for unknown units, '' for null/undefined.
function unitSuffix(unitStr) {
    if (!unitStr) return '';
    const base = BASE_UNIT_DISPLAY[unitStr];
    return base ? base.unit : ` ${unitStr}`;
}

const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

// Escape text for HTML element content AND quoted attribute values
function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    return String(text).replace(/[&<>"']/g, ch => HTML_ESCAPES[ch]);
}

// Plotly renders a small HTML subset in names/titles; pass caller text as entities
function plotlyText(text) {
    return escapeHtml(text);
}
