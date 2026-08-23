/**
 * Lab Monitor Dashboard - Frontend Logic
 * Upgraded with Plotly.js for interactive charts
 */

let refreshInterval = 30000;

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

let metricChart = null;  // Global Plotly.js instance
let currentChartData = null;  // Store current chart data for exports
let currentChartLabel = null;  // Store current chart label
let currentChartInfo = null;   // Store current chart info
let currentSystemName = null;  // Store current system for reload
let currentMetricField = null;  // Store metric field for reload
let currentVolumeInfo = null;   // Store volume info for reload
let currentTimeRangeHours = null;  // Current time range selection
let currentChartType = null;   // 'metric' or 'volume'

// Base-unit → display parameters.  The collector declares what unit it
// measures in; this table says how to present that unit on a chart.
// Falls back to METRIC_UNITS below for legacy/unknown fields.
const BASE_UNIT_DISPLAY = {
    '%':     { unit: '%',     scale: 1,             decimals: 1, beginAtZero: true  },
    's':     { unit: ' h',    scale: 1 / 3600,      decimals: 1, beginAtZero: false },
    'Mbps':  { unit: ' Mbps', scale: 1,             decimals: 2, beginAtZero: true  },
    'bytes': { unit: ' GB',   scale: 1 / 1073741824,decimals: 2, beginAtZero: false },
};

// Field-name fallback for metrics that predate unit fields in the collector
const METRIC_UNITS = {
    cpu_percent:                { yLabel: 'CPU Usage (%)',    unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
    ram_percent:                { yLabel: 'RAM Usage (%)',    unit: '%',     decimals: 1, scale: 1,             beginAtZero: true  },
    uptime_seconds:             { yLabel: 'Uptime (hours)',   unit: ' h',    decimals: 1, scale: 1 / 3600,      beginAtZero: false },
    network_bandwidth_in_mbps:  { yLabel: 'Download (Mbps)', unit: ' Mbps', decimals: 2, scale: 1,             beginAtZero: true  },
    network_bandwidth_out_mbps: { yLabel: 'Upload (Mbps)',   unit: ' Mbps', decimals: 2, scale: 1,             beginAtZero: true  },
    network_bytes_in:           { yLabel: 'Data In (GB)',     unit: ' GB',   decimals: 2, scale: 1 / 1073741824, beginAtZero: false },
    network_bytes_out:          { yLabel: 'Data Out (GB)',    unit: ' GB',   decimals: 2, scale: 1 / 1073741824, beginAtZero: false },
};

function initDashboard(config) {
    refreshInterval = config.refreshInterval || 30000;
    setupChartCallout();
    loadData();
    setInterval(loadData, refreshInterval);
    console.log(`Dashboard initialized. Refresh: ${refreshInterval}ms`);
}

function setupChartCallout() {
    const callout = document.getElementById('chartCallout');
    const closeBtn = document.querySelector('.chart-close');

    closeBtn.onclick = () => { callout.classList.remove('active'); };
    callout.onclick = (e) => {
        if (e.target === callout) callout.classList.remove('active');
    };
    
    // Initialize date pickers to sensible defaults
    const today = new Date();
    const thirtyDaysAgo = new Date(today.getTime() - 30 * 24 * 60 * 60 * 1000);
    document.getElementById('customTo').valueAsDate = today;
    document.getElementById('customFrom').valueAsDate = thirtyDaysAgo;
}

// -------------------------------------------------------------------------
// Data loading
// -------------------------------------------------------------------------

function loadData() {
    fetch('/api/data')
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            updateDashboard(data);
            updateStatus('connected');
            document.getElementById('lastUpdate').textContent =
                'Last updated: ' + new Date().toLocaleTimeString();
        })
        .catch(err => {
            console.error('Error loading data:', err);
            updateStatus('error');
            document.getElementById('nasGrid').innerHTML =
                '<div class="loading">⚠️ Unable to reach Manager. Retrying...</div>';
        });
}

// -------------------------------------------------------------------------
// Dashboard update
// -------------------------------------------------------------------------

function updateDashboard(data) {
    const nasGrid      = document.getElementById('nasGrid');
    const systems      = data.systems       || {};
    const globalTotals = data.global_totals || {};
    const names        = Object.keys(systems);

    if (names.length === 0) {
        nasGrid.innerHTML = '<div class="loading">No systems reporting yet</div>';
        updateSummary(globalTotals);
        return;
    }

    // Total storage: sum used and capacity across all volumes on all systems
    let totalStorageUsed     = 0;
    let totalStorageCapacity = 0;
    for (const sys of Object.values(systems)) {
        for (const vol of ((sys.disk || {}).volumes || [])) {
            totalStorageUsed     += (vol.usage_bytes  || 0);
            totalStorageCapacity += (vol.total_bytes  || 0);
        }
    }
    globalTotals.total_storage_used     = totalStorageUsed;
    globalTotals.total_storage_capacity = totalStorageCapacity;

    updateSummary(globalTotals);

    // Render summary strip — grouped by device type in fixed order
    const GROUP_ORDER  = ['server', 'nas', 'nas-instrument', 'nas-backup'];
    const GROUP_LABELS = {
        'server':         'Servers',
        'nas':            'NAS',
        'nas-instrument': 'NAS Instrument',
        'nas-backup':     'NAS Backup',
    };

    // Bucket systems into groups; unknown types collect under 'other'
    const groupedButtons = {};
    for (const [name, sys] of Object.entries(systems)) {
        const key = (sys.device_type || '').toLowerCase();
        const bucket = GROUP_ORDER.includes(key) ? key : 'other';
        (groupedButtons[bucket] = groupedButtons[bucket] || []).push([name, sys]);
    }

    const strip = document.getElementById('systemSummaryStrip');
    strip.innerHTML = '';

    const renderGroup = (key, label) => {
        const items = groupedButtons[key];
        if (!items || items.length === 0) return;
        const row = document.createElement('div');
        row.className = 'sys-btn-group';
        const lbl = document.createElement('div');
        lbl.className = 'sys-btn-group-label';
        lbl.textContent = label;
        row.appendChild(lbl);
        for (const [name, sys] of items) row.appendChild(createSummaryButton(name, sys));
        strip.appendChild(row);
    };

    for (const key of GROUP_ORDER) renderGroup(key, GROUP_LABELS[key]);
    if (groupedButtons['other']) renderGroup('other', 'Other');

    // Render full cards — sorted by device type, then alphabetically within each type
    nasGrid.innerHTML = '';
    const groupedCards = {};
    for (const [name, sys] of Object.entries(systems)) {
        const key = (sys.device_type || '').toLowerCase();
        const bucket = GROUP_ORDER.includes(key) ? key : 'other';
        (groupedCards[bucket] = groupedCards[bucket] || []).push([name, sys]);
    }
    for (const key of GROUP_ORDER) {
        if (groupedCards[key]) {
            groupedCards[key].sort((a, b) => a[0].localeCompare(b[0]));
            for (const [name, sys] of groupedCards[key]) {
                nasGrid.appendChild(createSystemCard(name, sys));
            }
        }
    }
    if (groupedCards['other']) {
        groupedCards['other'].sort((a, b) => a[0].localeCompare(b[0]));
        for (const [name, sys] of groupedCards['other']) {
            nasGrid.appendChild(createSystemCard(name, sys));
        }
    }
}

function updateSummary(globalTotals) {
    // Global totals come from server-side accumulation (survives reboots)
    document.getElementById('lifetimeTransferIn').innerHTML  = formatBytes(globalTotals.total_bytes_in  || 0) + ' <span class="transfer-label">in</span>';
    document.getElementById('lifetimeTransferOut').innerHTML = formatBytes(globalTotals.total_bytes_out || 0) + ' <span class="transfer-label">out</span>';

    const storageUsed = globalTotals.total_storage_used     || 0;
    const storageCap  = globalTotals.total_storage_capacity || 0;
    document.getElementById('totalStorage').textContent =
        storageCap > 0 ? `${formatBytes(storageUsed)} of ${formatBytes(storageCap)}` : '-';
}

// -------------------------------------------------------------------------
// System summary button (compact strip above the full cards)
// -------------------------------------------------------------------------

const NAS_TYPES = new Set(['nas', 'nas-instrument', 'nas-backup']);

function metricClass1(val, warnThresh, dangerThresh) {
    if (val === null || isNaN(val)) return '';
    return val >= dangerThresh ? 'danger' : val >= warnThresh ? 'warning' : '';
}

function createSummaryButton(systemName, sys) {
    const btn = document.createElement('div');

    const deviceType = (sys.device_type || '').toLowerCase();
    const metricsTs  = sys.metrics ? sys.metrics.timestamp : null;
    const OFFLINE_MS = 30 * 60 * 1000;  // 30 minutes
    const isOffline  = !metricsTs || (Date.now() - new Date(metricsTs).getTime()) > OFFLINE_MS;

    let outerClass = '';
    let innerHtml  = '';
    const offlineBadge = isOffline ? '<span class="sys-btn-offline-badge">Offline</span>' : '';

    if (NAS_TYPES.has(deviceType)) {
        // --- NAS: aggregate bar + used/total label ---
        let usedBytes  = 0;
        let totalBytes = 0;
        for (const vol of ((sys.disk || {}).volumes || [])) {
            usedBytes  += (vol.usage_bytes || 0);
            totalBytes += (vol.total_bytes || 0);
        }
        const pct      = totalBytes > 0 ? Math.round((usedBytes / totalBytes) * 100) : null;
        const barClass = metricClass1(pct, 75, 90);
        outerClass     = barClass;
        // Always render same structure for consistent sizing
        const usedTB  = pct !== null ? Math.round(usedBytes  / 1e12) : 0;
        const totalTB = pct !== null ? Math.round(totalBytes / 1e12) : 0;
        const displayPct = pct !== null ? pct : 0;
        innerHtml = `
            <div class="sys-btn-bar-row">
                <div class="usage-bar ${barClass}"><div class="usage-fill" style="width:${displayPct}%"></div></div>
                <span class="sys-btn-pct">${displayPct}%</span>
            </div>
            <div class="sys-btn-stat">${usedTB} TB / ${totalTB} TB</div>`;
    } else {
        // --- Server: CPU + RAM sub-tiles (show last readings even if offline) ---
        const m      = sys.metrics || {};
        const cpu    = parseFloat(m.cpu_percent);
        const ram    = parseFloat(m.ram_percent);
        const cpuCls = metricClass1(isNaN(cpu) ? null : cpu, 50, 90);
        const ramCls = metricClass1(isNaN(ram) ? null : ram, 50, 90);
        const max    = Math.max(isNaN(cpu) ? 0 : cpu, isNaN(ram) ? 0 : ram);
        outerClass   = metricClass1(max, 50, 90);
        const cpuW   = isNaN(cpu) ? 0 : Math.min(100, cpu);
        const ramW   = isNaN(ram) ? 0 : Math.min(100, ram);
        const cpuTxt = isNaN(cpu) ? '—' : safeFixed(cpu, 1) + '%';
        const ramTxt = isNaN(ram) ? '—' : safeFixed(ram, 1) + '%';
        innerHtml = `
            <div class="sys-btn-metrics">
                <div class="sys-btn-metric ${cpuCls}">
                    <div class="sys-btn-metric-label">CPU</div>
                    <div class="sys-btn-metric-val">${cpuTxt}</div>
                    <div class="usage-bar ${cpuCls}"><div class="usage-fill" style="width:${cpuW}%"></div></div>
                </div>
                <div class="sys-btn-metric ${ramCls}">
                    <div class="sys-btn-metric-label">RAM</div>
                    <div class="sys-btn-metric-val">${ramTxt}</div>
                    <div class="usage-bar ${ramCls}"><div class="usage-fill" style="width:${ramW}%"></div></div>
                </div>
            </div>`;
    }

    btn.className = ['sys-btn', outerClass, isOffline ? 'offline' : ''].filter(Boolean).join(' ');
    btn.innerHTML = `<div class="sys-btn-header">${offlineBadge}<div class="sys-btn-name">${escapeHtml(systemName)}</div></div>${innerHtml}`;

    // Click scrolls to the full card
    btn.onclick = () => {
        const card = document.querySelector(`[data-system-name="${CSS.escape(systemName)}"]`);
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };

    return btn;
}

// System card
// -------------------------------------------------------------------------

function createSystemCard(systemName, sys) {
    const card = document.createElement('div');

    const deviceType   = sys.device_type || 'unknown';
    const metricsTs    = sys.metrics ? sys.metrics.timestamp : null;
    const OFFLINE_MS   = 30 * 60 * 1000;  // 30 minutes
    const isOffline    = !metricsTs || (Date.now() - new Date(metricsTs).getTime()) > OFFLINE_MS;
    const timestamp    = metricsTs
                            ? ('Last report: ' + formatTimestamp(metricsTs))
                            : 'No reports yet';

    card.className = 'nas-card' + (isOffline ? ' offline' : '');

    // --- Metrics section ---
    let metricsHtml = '';
    if (sys.metrics) {
        const m = sys.metrics;
        const u = m.units || {};
        metricsHtml = `
            <div class="card-section">
                <div class="section-label">System</div>
                <div class="metrics-stats">
                    <div class="metric-item ${metricClass(m.cpu_percent, 50, 75)}" data-metric="cpu_percent" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">CPU</span>
                        <span class="metric-value">${safeFixed(m.cpu_percent, 1)}${unitSuffix(u.cpu_percent || '%')}</span>
                    </div>
                    <div class="metric-item ${metricClass(m.ram_percent, 50, 75)}" data-metric="ram_percent" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">RAM</span>
                        <span class="metric-value">${safeFixed(m.ram_percent, 1)}${unitSuffix(u.ram_percent || '%')}</span>
                    </div>
                    <div class="metric-item" data-metric="uptime_seconds" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">Uptime</span>
                        <span class="metric-value">${escapeHtml(m.uptime_formatted || '0s')}</span>
                    </div>
                    <div class="metric-item" data-metric="network_bandwidth_in_mbps" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">↓</span>
                        <span class="metric-value">${safeFixed(m.network_bandwidth_in_mbps, 2)}${unitSuffix(u.network_bandwidth_in_mbps || 'Mbps')}</span>
                    </div>
                    <div class="metric-item" data-metric="network_bandwidth_out_mbps" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">↑</span>
                        <span class="metric-value">${safeFixed(m.network_bandwidth_out_mbps, 2)}${unitSuffix(u.network_bandwidth_out_mbps || 'Mbps')}</span>
                    </div>
                </div>
            </div>`;
    }

    // --- Storage Overview section (volume buttons) ---
    let storageOverviewHtml = '';
    // --- Storage section (shared folders only, sorted largest to smallest) ---
    let diskHtml = '';

    if (sys.disk) {
        const d = sys.disk;

        // Volume buttons - one per volume, "volume1: XX of YY" + progress bar
        let volHtml = '';
        for (const vol of (d.volumes || [])) {
            const volLabel = vol.path.replace(/^\//, '');  // strip leading slash
            if (vol.total_bytes) {
                const pct      = Math.min(100, Math.round((vol.usage_bytes / vol.total_bytes) * 100));
                const barClass = pct >= 90 ? 'danger' : pct >= 70 ? 'warning' : '';
                volHtml += `
                    <div class="volume-btn" data-volume="${escapeHtml(vol.path)}" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <div class="volume-btn-label">
                            <span class="volume-btn-name">${escapeHtml(volLabel)}</span>
                            <span class="volume-btn-usage">${escapeHtml(vol.usage_formatted)} of ${escapeHtml(vol.total_formatted)}</span>
                        </div>
                        <div class="usage-bar ${barClass}">
                            <div class="usage-fill" style="width:${pct}%"></div>
                        </div>
                    </div>`;
            } else {
                // Fallback: no capacity data yet (older collector)
                volHtml += `
                    <div class="folder-item">
                        <span class="folder-path">${escapeHtml(vol.path)}</span>
                        <span class="folder-size">${escapeHtml(vol.usage_formatted)}</span>
                    </div>`;
            }
        }

        if (volHtml) {
            storageOverviewHtml = `
                <div class="card-section">
                    <div class="section-label">Storage Overview</div>
                    ${volHtml}
                </div>`;
        }

        // Shared folders only - sorted largest to smallest, all shown
        const folders = [...(d.folders || [])].sort((a, b) => b.usage_bytes - a.usage_bytes);
        let folderHtml = '';
        for (const f of folders) {
            folderHtml += `
                <div class="folder-item">
                    <span class="folder-path">${escapeHtml(f.path)}</span>
                    <span class="folder-size">${escapeHtml(f.usage_formatted)}</span>
                </div>`;
        }

        if (folderHtml) {
            diskHtml = `
                <div class="card-section">
                    <div class="section-label">Storage</div>
                    <div class="folder-list">
                        ${folderHtml}
                    </div>
                </div>`;
        }
    }

    // --- User activity section (server devices only) ---
    const isServer = !NAS_TYPES.has((sys.device_type || '').toLowerCase());

    // --- GPU section (server devices with NVIDIA GPUs only) ---
    let gpuHtml = '';
    const gpus = (sys.metrics || {}).gpus || [];
    if (isServer && gpus.length > 0) {
        const gpuRows = gpus.map((g, idx) => {
            const vramPct   = g.vram_total_bytes > 0
                ? Math.round((g.vram_used_bytes / g.vram_total_bytes) * 100) : 0;
            const gpuCls    = g.gpu_percent >= 90 ? 'danger'  : g.gpu_percent >= 70 ? 'warning' : '';
            const vramCls   = vramPct       >= 90 ? 'danger'  : vramPct       >= 75 ? 'warning' : '';
            const tempStr   = g.temperature_c !== null && g.temperature_c !== undefined
                ? ` &bull; ${g.temperature_c}\u00b0C` : '';
            const powerStr  = g.power_watts  !== null && g.power_watts  !== undefined
                ? ` &bull; ${g.power_watts}W` : '';
            // Use GPU index for metric field names (e.g., "gpu_0_percent", "gpu_0_vram_used")
            const gpuMetricPrefix = `gpu_${idx}`;
            return `
                <div class="gpu-row">
                    <div class="gpu-name">${escapeHtml(g.name)}${tempStr}${powerStr}</div>
                    <div class="metrics-stats">
                        <div class="metric-item ${gpuCls}" data-metric="${gpuMetricPrefix}_percent" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                            <span class="metric-label">GPU</span>
                            <span class="metric-value">${safeFixed(g.gpu_percent, 1)}%</span>
                        </div>
                        <div class="metric-item ${vramCls}" data-metric="${gpuMetricPrefix}_vram_used" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                            <span class="metric-label">VRAM</span>
                            <span class="metric-value">${escapeHtml(g.vram_used_formatted)} / ${escapeHtml(g.vram_total_formatted)}</span>
                        </div>
                    </div>
                </div>`;
        }).join('');
        gpuHtml = `
            <div class="card-section">
                <div class="section-label">GPU</div>
                ${gpuRows}
            </div>`;
    }

    let usersHtml = '';
    if (isServer && sys.users && sys.users.length > 0) {
        // Sort by CPU usage descending, show all users
        const sortedUsers = [...sys.users].sort((a, b) => (b.cpu_percent || 0) - (a.cpu_percent || 0));
        
        // Get total system RAM from metrics
        const totalRamBytes = (sys.metrics && sys.metrics.total_ram_bytes) || 0;
        
        const rows = sortedUsers.map(u => {
            // Color CPU based on thresholds: >75% red, >50% orange
            const cpuClass = u.cpu_percent >= 75 ? 'danger' : u.cpu_percent >= 50 ? 'warning' : '';
            
            // Color RAM based on percentage of total system RAM
            let ramClass = '';
            const ramBytes = u.ram_bytes || 0;
            if (totalRamBytes > 0) {
                const ramPercent = (ramBytes / totalRamBytes) * 100;
                if (ramPercent >= 75) {
                    ramClass = 'danger';
                } else if (ramPercent >= 50) {
                    ramClass = 'warning';
                }
            }
            
            return `
            <div class="user-row">
                <span class="user-name" title="${escapeHtml(u.username)}">${escapeHtml(u.username.split('\\').pop())}</span>
                <span class="user-cpu ${cpuClass}">${safeFixed(u.cpu_percent, 1)}%</span>
                <span class="user-ram ${ramClass}">${escapeHtml(u.ram_formatted)}</span>
            </div>`;
        }).join('');
        
        const scrollClass = sortedUsers.length > 8 ? 'user-list-scrollable' : '';
        usersHtml = `
            <div class="card-section">
                <div class="section-label">User Activity (${sortedUsers.length})</div>
                <div class="user-header">
                    <span>User</span><span>CPU</span><span>RAM</span>
                </div>
                <div class="user-list ${scrollClass}">${rows}</div>
            </div>`;
    }

    card.dataset.systemName = systemName;

    // Offline banner (top of card if offline)
    const offlineBanner = isOffline ? `<div class="offline-banner">⚠️ Offline (no report for 30+ minutes)</div>` : '';

    card.innerHTML = offlineBanner + `
        <div class="nas-card-header">
            <div>
                <div class="nas-name">${escapeHtml(systemName)}</div>
                <div class="device-type">${escapeHtml(deviceType)}</div>
            </div>
            <div class="timestamp${isOffline ? ' offline-timestamp' : ''}">${timestamp}</div>
        </div>
        ${metricsHtml}
        ${gpuHtml}
        ${usersHtml}
        ${storageOverviewHtml}
        ${diskHtml}
        ${!sys.metrics && !sys.disk ? '<div class="folder-item">No data yet</div>' : ''}
    `;

    // Attach click handlers only if not offline
    if (!isOffline) {
        const metricItems = card.querySelectorAll('[data-metric]');
        metricItems.forEach(item => {
            item.addEventListener('click', () => {
                const system = item.getAttribute('data-system');
                const metric = item.getAttribute('data-metric');
                let label = item.querySelector('.metric-label')?.textContent || metric;
                // For GPU metrics, append GPU index for clarity
                if (metric.startsWith('gpu_')) {
                    const parts = metric.split('_');
                    const gpuIdx = parts[1];
                    const metricType = parts.slice(2).join('_');
                    label = `GPU ${gpuIdx} - ${label}`;
                }
                showMetricChart(system, metric, label);
            });
        });

        // Attach click handlers to volume buttons
        const volumeButtons = card.querySelectorAll('[data-volume]');
        volumeButtons.forEach(btn => {
            btn.addEventListener('click', () => {
                const system = btn.getAttribute('data-system');
                const volume = btn.getAttribute('data-volume');
                const label = btn.querySelector('.volume-btn-name')?.textContent || volume;
                showVolumeChart(system, volume, label);
            });
        });
    }

    return card;
}

// -------------------------------------------------------------------------
// Metric Chart (Plotly.js)
// -------------------------------------------------------------------------

function showMetricChart(systemName, metricField, metricLabel) {
    const callout = document.getElementById('chartCallout');
    const title = document.getElementById('chartTitle');
    title.textContent = `${systemName} — ${metricLabel}`;

    callout.classList.add('active');
    currentChartType = 'metric';
    currentSystemName = systemName;
    currentMetricField = metricField;
    currentTimeRangeHours = 24;  // Default: last 24 hours
    currentChartLabel = metricLabel;

    // Default: last 24 hours
    const to   = new Date();
    const from = new Date(to.getTime() - 24 * 60 * 60 * 1000);
    fetchAndRenderMetric(systemName, metricField, metricLabel, from.toISOString(), to.toISOString());
}

function fetchAndRenderMetric(systemName, metricField, metricLabel, fromISO, toISO) {
    const title = document.getElementById('chartTitle');

    // Build query string: use time-range when available, else tail limit
    let qs;
    if (fromISO) {
        qs = `from=${encodeURIComponent(fromISO)}&to=${encodeURIComponent(toISO)}`;
    } else {
        qs = `limit=500`;
    }

    // Fetch historical data
    fetch(`/api/history/${encodeURIComponent(systemName)}/system_metrics/${encodeURIComponent(metricField)}?${qs}`)
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            if (data.error) throw new Error(data.error);

            // Resolve display info: API unit takes priority, then field-name map
            const base = data.unit && BASE_UNIT_DISPLAY[data.unit];
            let info = base
                ? { ...base, yLabel: `${metricLabel} (${base.unit.trim()})` }
                : (METRIC_UNITS[metricField] || null);
            
            // If no direct match, try GPU pattern matching
            if (!info) {
                if (metricField.match(/^gpu_\d+_percent$/)) {
                    info = { yLabel: metricLabel, unit: '%', decimals: 1, scale: 1, beginAtZero: true };
                } else if (metricField.match(/^gpu_\d+_vram_used$/)) {
                    info = { yLabel: metricLabel, unit: ' GB', decimals: 2, scale: 1 / 1073741824, beginAtZero: true };
                } else if (metricField.match(/^gpu_\d+_temp_c$/)) {
                    info = { yLabel: metricLabel, unit: '°C', decimals: 1, scale: 1, beginAtZero: false };
                } else if (metricField.match(/^gpu_\d+_power_w$/)) {
                    info = { yLabel: metricLabel, unit: ' W', decimals: 1, scale: 1, beginAtZero: true };
                } else {
                    info = { yLabel: metricLabel, unit: '', decimals: 2, scale: 1, beginAtZero: true };
                }
            }

            const n = (data.data || []).length;
            title.textContent = `${systemName} — ${metricLabel} (${n} measurements)`;
            renderMetricChart(metricField, metricLabel, info, data.data);
            currentChartInfo = info;
        })
        .catch(err => {
            console.error('Error fetching metric history:', err);
            document.getElementById('metricChart').innerHTML = `<p style="color:#e74c3c;">Error: ${escapeHtml(err.message)}</p>`;
        });
}

function renderMetricChart(metricField, metricLabel, info, data) {
    // Store data for exports
    currentChartData = data;
    
    const timestamps = data.map(d => d.timestamp);
    const values = data.map(d => (d.value != null) ? +(d.value * info.scale) : null);

    // Color by metric type
    let color = '#3498db';
    if (metricField === 'cpu_percent' || metricField === 'ram_percent') {
        color = '#e74c3c';
    } else if (metricField.includes('bandwidth')) {
        color = '#27ae60';
    } else if (metricField.match(/^gpu_\d+_(percent|temp_c|power_w)$/)) {
        color = '#f39c12';  // Orange for GPU metrics
    } else if (metricField.match(/^gpu_\d+_vram/)) {
        color = '#9b59b6';  // Purple for VRAM metrics
    }

    const trace = {
        x: timestamps,
        y: values,
        name: metricLabel,
        mode: 'lines+markers',
        line: { color: color, width: 2 },
        marker: { size: 5 },
        fill: 'tozeroy',
        fillcolor: color + '33',
        hovertemplate: metricLabel + ': %{y:.' + info.decimals + 'f}' + info.unit + '<extra></extra>'
    };

    const layout = {
        title: null,  // We use h3 instead
        xaxis: {
            type: 'date',
            rangeslider: { visible: false },
            rangeselector: { buttons: [] },
            title: 'Time',
        },
        yaxis: {
            title: info.yLabel,
            zeroline: info.beginAtZero,
        },
        hovermode: 'x unified',
        margin: { l: 60, r: 20, t: 10, b: 80 },
        plot_bgcolor: 'rgba(245, 245, 245, 0.5)',
        paper_bgcolor: 'white',
    };

    const config = {
        responsive: true,
        displayModeBar: true,
        displaylogo: false,
        modeBarButtonsToRemove: ['lasso2d', 'select2d'],
        modeBarButtonsToAdd: [csvModeBarButton()],
    };

    Plotly.newPlot('metricChart', [trace], layout, config);
    updateChartInfo(data);
}

function showVolumeChart(systemName, volumePath, volumeLabel) {
    const callout = document.getElementById('chartCallout');
    const title = document.getElementById('chartTitle');
    title.textContent = `${systemName} — ${volumeLabel} Usage`;
    
    callout.classList.add('active');
    currentChartType = 'volume';
    currentSystemName = systemName;
    currentVolumeInfo = { path: volumePath, label: volumeLabel };
    currentTimeRangeHours = null;  // Default: all available (volumes are daily — not many points)

    // Strip leading slashes before encoding
    const safeVolumePath = volumePath.replace(/^\/+/, '');
    fetchAndRenderVolume(systemName, safeVolumePath, volumeLabel, null, null);
}

function fetchAndRenderVolume(systemName, safeVolumePath, volumeLabel, fromISO, toISO) {
    const title = document.getElementById('chartTitle');

    let qs;
    if (fromISO) {
        qs = `from=${encodeURIComponent(fromISO)}&to=${encodeURIComponent(toISO)}`;
    } else {
        qs = `limit=500`;
    }

    // Fetch volume usage history from folder_usage table
    fetch(`/api/history/${encodeURIComponent(systemName)}/folder_usage/${encodeURIComponent(safeVolumePath)}?${qs}`)
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            if (data.error) {
                throw new Error(data.error);
            }
            const n = (data.data || []).length;
            title.textContent = `${systemName} — ${volumeLabel} Usage (${n} measurements)`;
            renderVolumeChart(volumeLabel, data.data);
        })
        .catch(err => {
            console.error('Error fetching volume history:', err);
            document.getElementById('metricChart').innerHTML = `<p style="color:#e74c3c;">Error: ${escapeHtml(err.message)}</p>`;
        });
}

function renderVolumeChart(volumeLabel, data) {
    // Store data for exports
    currentChartData = data;
    
    const timestamps = data.map(d => d.timestamp);
    const rawValues = data.map(d => d.value || 0);

    // Auto-scale: use TB if any value >= 1 TB, else GB
    const maxBytes  = Math.max(...rawValues, 0);
    const useTB     = maxBytes >= 1099511627776;  // 1 TiB
    const divisor   = useTB ? 1099511627776 : 1073741824;
    const unitStr   = useTB ? 'TB' : 'GB';
    const decimals  = 2;

    const scaledValues = rawValues.map(v => +(v / divisor).toFixed(decimals));

    const trace = {
        x: timestamps,
        y: scaledValues,
        name: `${volumeLabel} Used`,
        mode: 'lines+markers',
        line: { color: '#3498db', width: 2 },
        marker: { size: 5 },
        fill: 'tozeroy',
        fillcolor: '#3498db33',
        hovertemplate: volumeLabel + ' Usage: %{y:.' + decimals + 'f} ' + unitStr + '<extra></extra>'
    };

    const layout = {
        title: null,
        xaxis: {
            type: 'date',
            rangeslider: { visible: false },
            rangeselector: { buttons: [] },
            title: 'Time',
        },
        yaxis: {
            title: `Usage (${unitStr})`,
            zeroline: false,
        },
        hovermode: 'x unified',
        margin: { l: 60, r: 20, t: 10, b: 80 },
        plot_bgcolor: 'rgba(245, 245, 245, 0.5)',
        paper_bgcolor: 'white',
    };

    const config = {
        responsive: true,
        displayModeBar: true,
        displaylogo: false,
        modeBarButtonsToRemove: ['lasso2d', 'select2d'],
        modeBarButtonsToAdd: [csvModeBarButton()],
    };

    Plotly.newPlot('metricChart', [trace], layout, config);
    updateChartInfo(data);
}

function updateChartInfo(data) {
    if (!data || data.length === 0) {
        document.getElementById('chartInfo').textContent = 'No data';
        return;
    }
    const n = data.length;
    const firstTime = new Date(data[0].timestamp);
    const lastTime = new Date(data[n - 1].timestamp);
    const spanHours = (lastTime - firstTime) / (1000 * 60 * 60);
    const info = `Showing ${n} measurements spanning ${spanHours.toFixed(1)} hours`;
    document.getElementById('chartInfo').textContent = info;
}

// Time range control functions
function setTimeRange(hours) {
    currentTimeRangeHours = hours;

    let fromISO = null;
    let toISO   = null;

    const toDate = new Date();
    toISO = toDate.toISOString();

    if (hours !== null) {
        // Specific window: compute ISO timestamps
        fromISO = new Date(toDate.getTime() - hours * 60 * 60 * 1000).toISOString();
    } else {
        // All Available: anchor far enough back to capture everything
        fromISO = '2020-01-01T00:00:00.000Z';
    }

    if (currentChartType === 'metric') {
        fetchAndRenderMetric(currentSystemName, currentMetricField, currentChartLabel, fromISO, toISO);
    } else if (currentChartType === 'volume') {
        const safeVolumePath = currentVolumeInfo.path.replace(/^\/+/, '');
        fetchAndRenderVolume(currentSystemName, safeVolumePath, currentVolumeInfo.label, fromISO, toISO);
    }
}

function applyCustomRange() {
    const fromVal = document.getElementById('customFrom').value;
    const toVal   = document.getElementById('customTo').value;
    if (!fromVal || !toVal) {
        alert('Please select both From and To dates');
        return;
    }
    // Date inputs give YYYY-MM-DD; treat as start/end of day in UTC
    const fromISO = new Date(fromVal + 'T00:00:00Z').toISOString();
    const toISO   = new Date(toVal   + 'T23:59:59Z').toISOString();
    currentTimeRangeHours = null;  // Custom range, not a preset

    if (currentChartType === 'metric') {
        fetchAndRenderMetric(currentSystemName, currentMetricField, currentChartLabel, fromISO, toISO);
    } else if (currentChartType === 'volume') {
        const safeVolumePath = currentVolumeInfo.path.replace(/^\/+/, '');
        fetchAndRenderVolume(currentSystemName, safeVolumePath, currentVolumeInfo.label, fromISO, toISO);
    }
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
    if (!currentChartData || currentChartData.length === 0) {
        alert('No chart data to export');
        return;
    }
    
    // Build CSV
    let csv = 'timestamp,value\n';
    for (const row of currentChartData) {
        const ts = row.timestamp || '';
        const val = row.value !== null && row.value !== undefined ? row.value : '';
        csv += `"${ts}",${val}\n`;
    }
    
    // Download
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `chart-data-${Date.now()}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// -------------------------------------------------------------------------
// Utilities
// -------------------------------------------------------------------------

function metricClass(value, warnAt, dangerAt) {
    if (value > dangerAt) return 'danger';
    if (value > warnAt)   return 'warning';
    return '';
}

function updateStatus(status) {
    const el = document.getElementById('status');
    el.textContent = status === 'connected' ? '🟢 Connected' : '🔴 Error';
    el.className   = `status ${status}`;
}

function formatTimestamp(ts) {
    if (!ts) return 'Unknown';
    try { return new Date(ts).toLocaleString(); } catch (e) { return ts; }
}

function safeFixed(val, decimals) {
    const n = parseFloat(val);
    return isNaN(n) ? '-' : n.toFixed(decimals);
}

function formatBytes(bytes) {
    let v = parseFloat(bytes);
    if (isNaN(v) || !isFinite(v)) return '0 B';
    v = Math.max(0, v);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    for (const unit of units) {
        if (v < 1024) return `${v.toFixed(2)} ${unit}`;
        v /= 1024;
    }
    return `${v.toFixed(2)} PB`;
}

// Resolve a display suffix from a raw unit string using BASE_UNIT_DISPLAY.
// Falls back to ' {unit}' (with space) for unknown units, '' for null/undefined.
function unitSuffix(unitStr) {
    if (!unitStr) return '';
    const base = BASE_UNIT_DISPLAY[unitStr];
    return base ? base.unit : ` ${unitStr}`;
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}
