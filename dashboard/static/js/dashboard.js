/**
 * Lab Monitor Dashboard - Frontend Logic
 */

let refreshInterval = 30000;

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

let metricChart = null;  // Global Chart.js instance

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
    const grouped = {};
    for (const [name, sys] of Object.entries(systems)) {
        const key = (sys.device_type || '').toLowerCase();
        const bucket = GROUP_ORDER.includes(key) ? key : 'other';
        (grouped[bucket] = grouped[bucket] || []).push([name, sys]);
    }

    const strip = document.getElementById('systemSummaryStrip');
    strip.innerHTML = '';

    const renderGroup = (key, label) => {
        const items = grouped[key];
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
    if (grouped['other']) renderGroup('other', 'Other');

    // Render full cards
    nasGrid.innerHTML = '';
    for (const [name, sys] of Object.entries(systems)) {
        nasGrid.appendChild(createSystemCard(name, sys));
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
    const OFFLINE_MS = 7 * 60 * 1000;
    const isOffline  = !metricsTs || (Date.now() - new Date(metricsTs).getTime()) > OFFLINE_MS;

    let outerClass = '';
    let innerHtml  = '';

    if (isOffline) {
        innerHtml = '<div class="sys-btn-stat" style="color:#c0392b">Offline</div>';
    } else if (NAS_TYPES.has(deviceType)) {
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
        if (pct !== null) {
            const usedTB  = Math.round(usedBytes  / 1e12);
            const totalTB = Math.round(totalBytes / 1e12);
            innerHtml = `
                <div class="sys-btn-bar-row">
                    <div class="usage-bar ${barClass}"><div class="usage-fill" style="width:${pct}%"></div></div>
                    <span class="sys-btn-pct">${pct}%</span>
                </div>
                <div class="sys-btn-stat">${usedTB} TB / ${totalTB} TB</div>`;
        } else {
            innerHtml = '<div class="sys-btn-stat" style="opacity:.6">No storage data</div>';
        }
    } else {
        // --- Server: CPU + RAM sub-tiles ---
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
    btn.innerHTML = `<div class="sys-btn-name">${escapeHtml(systemName)}</div>${innerHtml}`;

    // Click scrolls to the full card
    btn.addEventListener('click', () => {
        const card = document.querySelector(`[data-system-name="${CSS.escape(systemName)}"]`);
        if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });

    return btn;
}

// System card
// -------------------------------------------------------------------------

function createSystemCard(systemName, sys) {
    const card = document.createElement('div');

    const deviceType   = sys.device_type || 'unknown';
    const metricsTs    = sys.metrics ? sys.metrics.timestamp : null;
    const OFFLINE_MS   = 7 * 60 * 1000;
    const isOffline    = !metricsTs || (Date.now() - new Date(metricsTs).getTime()) > OFFLINE_MS;
    const timestamp    = isOffline
                            ? 'System offline'
                            : ('Last report: ' + formatTimestamp(metricsTs));

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
        const gpuRows = gpus.map(g => {
            const vramPct   = g.vram_total_bytes > 0
                ? Math.round((g.vram_used_bytes / g.vram_total_bytes) * 100) : 0;
            const gpuCls    = g.gpu_percent >= 90 ? 'danger'  : g.gpu_percent >= 70 ? 'warning' : '';
            const vramCls   = vramPct       >= 90 ? 'danger'  : vramPct       >= 75 ? 'warning' : '';
            const tempStr   = g.temperature_c !== null && g.temperature_c !== undefined
                ? ` &bull; ${g.temperature_c}\u00b0C` : '';
            const powerStr  = g.power_watts  !== null && g.power_watts  !== undefined
                ? ` &bull; ${g.power_watts}W` : '';
            return `
                <div class="gpu-row">
                    <div class="gpu-name">${escapeHtml(g.name)}${tempStr}${powerStr}</div>
                    <div class="metrics-stats">
                        <div class="metric-item ${gpuCls}">
                            <span class="metric-label">GPU</span>
                            <span class="metric-value">${safeFixed(g.gpu_percent, 1)}%</span>
                        </div>
                        <div class="metric-item ${vramCls}">
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
        const topUsers = sys.users.slice(0, 8);  // cap at 8 rows
        const rows = topUsers.map(u => `
            <div class="user-row">
                <span class="user-name" title="${escapeHtml(u.username)}">${escapeHtml(u.username.split('\\').pop())}</span>
                <span class="user-cpu ${u.cpu_percent >= 90 ? 'danger' : u.cpu_percent >= 50 ? 'warning' : ''}">${safeFixed(u.cpu_percent, 1)}%</span>
                <span class="user-ram">${escapeHtml(u.ram_formatted)}</span>
            </div>`).join('');
        usersHtml = `
            <div class="card-section">
                <div class="section-label">User Activity</div>
                <div class="user-header">
                    <span>User</span><span>CPU</span><span>RAM</span>
                </div>
                <div class="user-list">${rows}</div>
            </div>`;
    }

    card.dataset.systemName = systemName;

    card.innerHTML = `
        <div class="nas-card-header">
            <div>
                <div class="nas-name">${escapeHtml(systemName)}</div>
                <div class="device-type">${escapeHtml(deviceType)}</div>
            </div>
            <div class="timestamp">${timestamp}</div>
        </div>
        ${metricsHtml}
        ${gpuHtml}
        ${usersHtml}
        ${storageOverviewHtml}
        ${diskHtml}
        ${!sys.metrics && !sys.disk ? '<div class="folder-item">No data yet</div>' : ''}
    `;

    // Attach click handlers to metric items
    const metricItems = card.querySelectorAll('[data-metric]');
    metricItems.forEach(item => {
        item.addEventListener('click', () => {
            const system = item.getAttribute('data-system');
            const metric = item.getAttribute('data-metric');
            const label = item.querySelector('.metric-label')?.textContent || metric;
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

    return card;
}

// -------------------------------------------------------------------------
// Metric Chart
// -------------------------------------------------------------------------

function showMetricChart(systemName, metricField, metricLabel) {
    const callout = document.getElementById('chartCallout');
    const title = document.getElementById('chartTitle');
    title.textContent = `${systemName} - ${metricLabel}`;

    callout.classList.add('active');

    // Fetch historical data
    fetch(`/api/history/${encodeURIComponent(systemName)}/system_metrics/${encodeURIComponent(metricField)}?limit=200`)
        .then(r => {
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            if (data.error) throw new Error(data.error);

            // Resolve display info: API unit takes priority, then field-name map
            const base = data.unit && BASE_UNIT_DISPLAY[data.unit];
            const info = base
                ? { ...base, yLabel: `${metricLabel} (${base.unit.trim()})` }
                : (METRIC_UNITS[metricField] || { yLabel: metricLabel, unit: '', decimals: 2, scale: 1, beginAtZero: true });

            const n = (data.data || []).length;
            title.textContent = `${systemName} — ${metricLabel} (${n} measurements)`;
            renderMetricChart(metricField, metricLabel, info, data.data);
        })
        .catch(err => {
            console.error('Error fetching metric history:', err);
            const chartContainer = document.querySelector('.chart-container');
            if (chartContainer) {
                chartContainer.innerHTML = `<p style="color:#e74c3c;">Error: ${escapeHtml(err.message)}</p>`;
            }
        });
}

function renderMetricChart(metricField, metricLabel, info, data) {
    // info is pre-resolved by the caller: BASE_UNIT_DISPLAY (from API unit)
    // or METRIC_UNITS (field-name fallback for older data).
    const canvas = document.getElementById('metricChart');
    const ctx = canvas.getContext('2d');

    if (metricChart) metricChart.destroy();

    const labels = formatChartTimestamps(data);
    const values = data.map(d => (d.value != null) ? +(d.value * info.scale) : null);

    // Color by metric type
    let color = '#3498db';
    if (metricField === 'cpu_percent' || metricField === 'ram_percent') {
        color = '#e74c3c';
    } else if (metricField.includes('bandwidth')) {
        color = '#27ae60';
    }

    metricChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: metricLabel,
                data: values,
                borderColor: color,
                backgroundColor: color + '22',
                borderWidth: 2,
                fill: true,
                tension: 0.4,
                pointRadius: 2,
                pointBackgroundColor: color,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: true, position: 'top' },
                tooltip: {
                    callbacks: {
                        label: ctx => `${ctx.dataset.label}: ${(+ctx.parsed.y).toFixed(info.decimals)}${info.unit}`
                    }
                }
            },
            scales: {
                y: {
                    beginAtZero: info.beginAtZero,
                    title: { display: true, text: info.yLabel },
                    ticks: {
                        callback: v => `${(+v).toFixed(info.decimals)}${info.unit}`
                    }
                },
                x: {
                    display: true,
                    ticks: { maxRotation: 45, minRotation: 0 }
                }
            }
        }
    });
}

function showVolumeChart(systemName, volumePath, volumeLabel) {
    const callout = document.getElementById('chartCallout');
    const title = document.getElementById('chartTitle');
    title.textContent = `${systemName} — ${volumeLabel} Usage`;
    
    callout.classList.add('active');
    
    // Fetch volume usage history from folder_usage table
    fetch(`/api/history/${encodeURIComponent(systemName)}/folder_usage/${encodeURIComponent(volumePath)}?limit=200`)
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
            const chartContainer = document.querySelector('.chart-container');
            if (chartContainer) {
                chartContainer.innerHTML = `<p style="color:#e74c3c;">Error: ${escapeHtml(err.message)}</p>`;
            }
        });
}

function renderVolumeChart(volumeLabel, data) {
    const canvas = document.getElementById('metricChart');
    const ctx = canvas.getContext('2d');

    if (metricChart) metricChart.destroy();

    const labels = formatChartTimestamps(data);

    // Auto-scale: use TB if any value >= 1 TB, else GB
    const rawValues = data.map(d => d.value || 0);
    const maxBytes  = Math.max(...rawValues, 0);
    const useTB     = maxBytes >= 1099511627776;  // 1 TiB
    const divisor   = useTB ? 1099511627776 : 1073741824;
    const unitStr   = useTB ? 'TB' : 'GB';
    const decimals  = 2;

    const scaledValues = rawValues.map(v => +(v / divisor).toFixed(decimals));

    metricChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: `${volumeLabel} Used`,
                data: scaledValues,
                borderColor: '#3498db',
                backgroundColor: '#3498db22',
                borderWidth: 2,
                fill: true,
                tension: 0.4,
                pointRadius: 2,
                pointBackgroundColor: '#3498db',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: true, position: 'top' },
                tooltip: {
                    callbacks: {
                        label: ctx => `${ctx.dataset.label}: ${(+ctx.parsed.y).toFixed(decimals)} ${unitStr}`
                    }
                }
            },
            scales: {
                y: {
                    beginAtZero: false,
                    title: { display: true, text: `Usage (${unitStr})` },
                    ticks: {
                        callback: v => `${(+v).toFixed(decimals)} ${unitStr}`
                    }
                },
                x: {
                    display: true,
                    ticks: { maxRotation: 45, minRotation: 0 }
                }
            }
        }
    });
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

// Smart timestamp labels: date-only for multi-day spans (e.g. daily disk), time-only for intraday
function formatChartTimestamps(data) {
    if (!data.length) return [];
    const times  = data.map(d => new Date(d.timestamp));
    const spanMs = times[times.length - 1] - times[0];
    if (spanMs > 18 * 3600 * 1000) {
        // Multi-day: show "Aug 4" style
        return times.map(d => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }));
    }
    // Intraday: show HH:MM
    return times.map(d => d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }));
}

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}
