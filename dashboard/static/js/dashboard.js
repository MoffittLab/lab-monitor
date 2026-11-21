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
    system:     null,
    field:      null,   // metric field, or volume path without leading slash
    label:      null,
    range:      null,   // data-range key of the active range button
    data:       null,   // points currently plotted (source for CSV export)
    meta:       null,   // { downsampled, truncated } of the plotted data
};

// Which part of each system a Manager endpoint supplies (app.py
// get_all_systems_data).  /api/devices is not listed: when it fails the whole
// poll fails (HTTP 502) and handleLoadFailure keeps the last good data.
const ENDPOINT_SECTIONS = {
    '/api/metrics/all':      'metrics',
    '/api/usage/all':        'disk',
    '/api/user_metrics/all': 'users',
    '/api/totals':           'totals',
};
const SECTION_LABELS = { metrics: 'metrics', disk: 'storage', users: 'user activity' };

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

        const metricEl = e.target.closest('[data-metric]');
        if (metricEl) {
            const metric = metricEl.dataset.metric;
            let label = metricEl.querySelector('.metric-label')?.textContent || metric;
            // For GPU metrics, prefix the GPU index for clarity
            const gpu = metric.match(/^gpu_(\d+)_/);
            if (gpu) label = `GPU ${gpu[1]} - ${label}`;
            showMetricChart(system, metric, label);
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

    return html`<div class="sys-btn-header">${badgeHtml}<div class="sys-btn-name">${escapeHtml(systemName)}</div></div>${bodyHtml}`;
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
    const stale     = sys.staleSections || [];
    const metricCls = stale.includes('metrics') ? 'stale-data' : '';
    const usersCls  = stale.includes('users')   ? 'stale-data' : '';
    const diskCls   = stale.includes('disk')    ? 'stale-data' : '';

    const stateBannerHtml = buildStateBannerHtml(st);
    const noticeHtml      = buildSectionNoticeHtml(sys, isServer);
    const metricsHtml     = buildMetricsHtml(sys.metrics, metricCls);
    const gpuHtml         = isServer ? buildGpuHtml((sys.metrics || {}).gpus || [], metricCls) : '';
    const usersHtml       = isServer ? buildUsersHtml(sys.users || [], sys.metrics, usersCls) : '';
    const diskHtml        = buildDiskHtml(sys.disk, diskCls);
    const emptyHtml       = !sys.metrics && !sys.disk && !(sys.unavailable || []).length
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
        ${gpuHtml}
        ${usersHtml}
        ${diskHtml}
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
    const shown = key => Object.prototype.hasOwnProperty.call(SECTION_LABELS, key) && (isServer || key !== 'users');
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
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">System</div>
            <div class="metrics-stats">
                <div class="metric-item ${cpuCls}" data-metric="cpu_percent">
                    <span class="metric-label">CPU</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.cpu_percent, 1, u.cpu_percent || '%'))}</span>
                </div>
                <div class="metric-item ${ramCls}" data-metric="ram_percent">
                    <span class="metric-label">RAM</span>
                    <span class="metric-value">${escapeHtml(fmtValue(m.ram_percent, 1, u.ram_percent || '%'))}</span>
                </div>
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
            </div>`;
    }).join('');
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">GPU</div>
            ${rowsHtml}
        </div>`;
}

// --- User activity section (server devices only) ---
function buildUsersHtml(users, metrics, sectionCls = '') {
    if (!users.length) return '';
    // Sort by CPU usage descending, show all users
    const sorted   = [...users].sort((a, b) => (toNumber(b.cpu_percent) || 0) - (toNumber(a.cpu_percent) || 0));
    const totalRam = toNumber(metrics && metrics.total_ram_bytes);

    const rowsHtml = sorted.map(u => {
        const cpuCls   = levelClass(u.cpu_percent, THRESHOLDS.cpu);
        // Color RAM by its share of total system RAM
        const ramBytes = toNumber(u.ram_bytes);
        const ramCls   = totalRam > 0 && ramBytes !== null ? levelClass((ramBytes / totalRam) * 100, THRESHOLDS.ram) : '';
        const fullName = String(u.username ?? '');
        const ramText  = ramBytes !== null ? formatBytes(ramBytes) : (u.ram_formatted || '—');
        return html`
            <div class="user-row">
                <span class="user-name" title="${escapeHtml(fullName)}">${escapeHtml(fullName.split('\\').pop())}</span>
                <span class="user-cpu ${cpuCls}">${escapeHtml(fmtValue(u.cpu_percent, 1, '%'))}</span>
                <span class="user-ram ${ramCls}">${escapeHtml(ramText)}</span>
            </div>`;
    }).join('');

    const scrollCls = sorted.length > 8 ? 'user-list-scrollable' : '';
    return html`
        <div class="card-section ${sectionCls}">
            <div class="section-label">User Activity (${fmtInt(sorted.length)})</div>
            <div class="user-header">
                <span>User</span><span>CPU</span><span>RAM</span>
            </div>
            <div class="user-list ${scrollCls}">${rowsHtml}</div>
        </div>`;
}

// --- Storage sections: volume buttons, then shared folders ---
function buildDiskHtml(d, sectionCls = '') {
    if (!d) return '';
    const scanHtml = buildDiskScanHtml(d);

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
    let folderHtml = '';
    for (const f of folders) {
        folderHtml += html`
            <div class="folder-item">
                <span class="folder-path">${escapeHtml(f.path)}</span>
                <span class="folder-size">${formatBytes(f.usage_bytes)}</span>
            </div>`;
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

// -------------------------------------------------------------------------
// History charts (Plotly.js)
// -------------------------------------------------------------------------

function showMetricChart(systemName, metricField, metricLabel) {
    openChart('metric', systemName, metricField, metricLabel, '24');   // default: last 24 hours
}

function showVolumeChart(systemName, volumePath, volumeLabel) {
    // Volumes are measured daily - not many points, so default to all of them.
    // Leading slashes are stripped before encoding; the Manager restores them.
    openChart('volume', systemName, String(volumePath).replace(/^\/+/, ''), volumeLabel, 'all');
}

function openChart(type, systemName, field, label, defaultRange) {
    chart.type   = type;
    chart.system = systemName;
    chart.field  = field;
    chart.label  = label;
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

function chartTitleText(count) {
    const what = chart.type === 'volume' ? `${chart.label} Usage` : chart.label;
    const n    = count === undefined ? '' : ` (${count} measurements)`;
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

    const dataType = chart.type === 'volume' ? 'folder_usage' : 'system_metrics';
    const qs = new URLSearchParams({ from: fromISO, to: toISO, max_points: String(maxChartPoints) });
    const url = `/api/history/${encodeURIComponent(chart.system)}/${dataType}/${encodeURIComponent(chart.field)}?${qs}`;

    fetch(url, { signal: controller.signal })
        .then(async r => {
            let body = null;
            try { body = await r.json(); } catch (e) { body = null; }
            if (!r.ok || !body || body.error) {
                throw new Error((body && body.error) || `HTTP ${r.status}`);
            }
            return body;
        })
        .then(body => {
            if (seq !== chart.seq) return;   // superseded by a newer selection
            chart.controller = null;
            const points = Array.isArray(body.data) ? body.data : [];
            chart.data = points;
            chart.meta = { downsampled: !!body.downsampled, truncated: !!body.truncated };
            document.getElementById('chartTitle').textContent = chartTitleText(points.length);
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
    if (base) return { ...base, yLabel: `${metricLabel} (${base.unit.trim()})` };
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

    plotChart([trace], chartLayout(info.yLabel, info.beginAtZero));
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
    let csv = meta.downsampled ? 'timestamp,value_avg\n' : 'timestamp,value\n';
    for (const row of (chart.data || [])) {
        const ts  = String(row.timestamp || '').replace(/"/g, '""');
        const val = toNumber(row.value);
        csv += `"${ts}",${val === null ? '' : val}\n`;
    }
    return csv;
}

// e.g. "Triton_cpu_percent_24h_20260821T143000Z.csv"; averaged data gets
// "_avg", data cut at the row limit "_truncated" (e.g. "..._all_avg_...csv")
function csvFilename() {
    const clean = s => String(s || '').replace(/[^A-Za-z0-9_.-]+/g, '_').replace(/^_+|_+$/g, '') || 'unknown';
    const meta  = chart.meta || {};
    const field = chart.type === 'volume' ? 'volume_' + chart.field : chart.field;
    const range = /^\d+$/.test(chart.range || '') ? chart.range + 'h' : chart.range;
    const flags = (meta.downsampled ? '_avg' : '') + (meta.truncated ? '_truncated' : '');
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
    return `${clean(chart.system)}_${clean(field)}_${clean(range)}${flags}_${stamp}.csv`;
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
