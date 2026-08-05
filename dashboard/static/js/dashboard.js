/**
 * Lab Monitor Dashboard - Frontend Logic
 */

let refreshInterval = 30000;

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

let metricChart = null;  // Global Chart.js instance

// Unit metadata for system_metrics fields — inferred from field names
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

    // Render cards
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
// System card
// -------------------------------------------------------------------------

function createSystemCard(systemName, sys) {
    const card = document.createElement('div');
    card.className = 'nas-card';

    const deviceType = sys.device_type || 'unknown';
    const timestamp  = formatTimestamp(sys.timestamp);

    // --- Metrics section ---
    let metricsHtml = '';
    if (sys.metrics) {
        const m = sys.metrics;
        metricsHtml = `
            <div class="card-section">
                <div class="section-label">System</div>
                <div class="metrics-stats">
                    <div class="metric-item ${metricClass(m.cpu_percent, 50, 75)}" data-metric="cpu_percent" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">CPU</span>
                        <span class="metric-value">${safeFixed(m.cpu_percent, 1)}%</span>
                    </div>
                    <div class="metric-item ${metricClass(m.ram_percent, 50, 75)}" data-metric="ram_percent" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">RAM</span>
                        <span class="metric-value">${safeFixed(m.ram_percent, 1)}%</span>
                    </div>
                    <div class="metric-item" data-metric="uptime_seconds" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">Uptime</span>
                        <span class="metric-value">${escapeHtml(m.uptime_formatted || '0s')}</span>
                    </div>
                    <div class="metric-item" data-metric="network_bandwidth_in_mbps" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">↓</span>
                        <span class="metric-value">${safeFixed(m.network_bandwidth_in_mbps, 2)} Mbps</span>
                    </div>
                    <div class="metric-item" data-metric="network_bandwidth_out_mbps" data-system="${escapeHtml(systemName)}" style="cursor:pointer;">
                        <span class="metric-label">↑</span>
                        <span class="metric-value">${safeFixed(m.network_bandwidth_out_mbps, 2)} Mbps</span>
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

    card.innerHTML = `
        <div class="nas-card-header">
            <div>
                <div class="nas-name">${escapeHtml(systemName)}</div>
                <div class="device-type">${escapeHtml(deviceType)}</div>
            </div>
            <div class="timestamp">${escapeHtml(timestamp)}</div>
        </div>
        ${metricsHtml}
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
            if (data.error) {
                throw new Error(data.error);
            }
            const n = (data.data || []).length;
            title.textContent = `${systemName} — ${metricLabel} (${n} measurements)`;
            renderMetricChart(metricField, metricLabel, data.data);
        })
        .catch(err => {
            console.error('Error fetching metric history:', err);
            const chartContainer = document.querySelector('.chart-container');
            if (chartContainer) {
                chartContainer.innerHTML = `<p style="color:#e74c3c;">Error: ${escapeHtml(err.message)}</p>`;
            }
        });
}

function renderMetricChart(metricField, metricLabel, data) {
    const canvas = document.getElementById('metricChart');
    const ctx = canvas.getContext('2d');

    if (metricChart) metricChart.destroy();

    const info = METRIC_UNITS[metricField] || { yLabel: metricLabel, unit: '', decimals: 2, scale: 1, beginAtZero: true };

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
