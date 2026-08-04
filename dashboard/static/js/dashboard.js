/**
 * Lab Monitor Dashboard — Frontend Logic
 */

let refreshInterval = 30000;

// -------------------------------------------------------------------------
// Init
// -------------------------------------------------------------------------

function initDashboard(config) {
    refreshInterval = config.refreshInterval || 30000;
    setupModal();
    loadData();
    setInterval(loadData, refreshInterval);
    console.log(`Dashboard initialized. Refresh: ${refreshInterval}ms`);
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
        updateSummary({}, globalTotals);
        return;
    }

    // Device type counts (still local — not in global_totals)
    const deviceTypeCounts = {};
    for (const sys of Object.values(systems)) {
        const dt = sys.device_type || 'unknown';
        deviceTypeCounts[dt] = (deviceTypeCounts[dt] || 0) + 1;
    }

    updateSummary(deviceTypeCounts, globalTotals);

    // Render cards
    nasGrid.innerHTML = '';
    for (const [name, sys] of Object.entries(systems)) {
        nasGrid.appendChild(createSystemCard(name, sys));
    }
}

function updateSummary(deviceTypeCounts, globalTotals) {
    // Device type table
    const tableDiv = document.getElementById('deviceTypeTable');
    let html = '<table style="width:100%;font-size:14px;border-collapse:collapse;">';
    for (const [type, count] of Object.entries(deviceTypeCounts).sort()) {
        html += `<tr style="border-bottom:1px solid #ddd;">
                   <td style="padding:6px;">${escapeHtml(type)}</td>
                   <td style="padding:6px;text-align:right;">${count}</td>
                 </tr>`;
    }
    html += '</table>';
    tableDiv.innerHTML = html;

    // Global totals come from server-side accumulation (survives reboots)
    document.getElementById('totalDataIn').textContent  = formatBytes(globalTotals.total_bytes_in   || 0);
    document.getElementById('totalDataOut').textContent = formatBytes(globalTotals.total_bytes_out  || 0);
    document.getElementById('totalUsage').textContent   = formatBytes(globalTotals.total_disk_bytes || 0);
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
                    <div class="metric-item">
                        <span class="metric-label">CPU</span>
                        <span class="metric-value">${safeFixed(m.cpu_percent, 1)}%</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">RAM</span>
                        <span class="metric-value">${safeFixed(m.ram_percent, 1)}%</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Uptime</span>
                        <span class="metric-value">${escapeHtml(m.uptime_formatted || '0s')}</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">↓</span>
                        <span class="metric-value">${safeFixed(m.network_bandwidth_in_mbps, 2)} Mbps</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">↑</span>
                        <span class="metric-value">${safeFixed(m.network_bandwidth_out_mbps, 2)} Mbps</span>
                    </div>
                </div>
            </div>`;
    }

    // --- Disk section ---
    let diskHtml = '';
    if (sys.disk) {
        const d = sys.disk;

        // Volume summary rows
        let volHtml = '';
        for (const vol of (d.volumes || [])) {
            volHtml += `
                <div class="folder-item">
                    <span class="folder-path">${escapeHtml(vol.path)}</span>
                    <span class="folder-size">${escapeHtml(vol.usage_formatted)}</span>
                </div>`;
        }

        // Folder rows (top 5)
        let folderHtml = '';
        const folders = d.folders || [];
        for (const f of folders.slice(0, 5)) {
            folderHtml += `
                <div class="folder-item subfolder">
                    <span class="folder-path">${escapeHtml(f.path)}</span>
                    <span class="folder-size">${escapeHtml(f.usage_formatted)}</span>
                </div>`;
        }
        if (folders.length > 5) {
            folderHtml += `<div class="folder-item subfolder">
                <span class="folder-path">… and ${folders.length - 5} more</span>
            </div>`;
        }

        diskHtml = `
            <div class="card-section">
                <div class="section-label">Storage</div>
                <div class="usage-stats">
                    <span>Total</span>
                    <span class="usage-value">${escapeHtml(d.total_usage_formatted)}</span>
                </div>
                <div class="folder-list">
                    ${volHtml}
                    ${folderHtml}
                </div>
            </div>`;
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
        ${diskHtml}
        ${!sys.metrics && !sys.disk ? '<div class="folder-item">No data yet</div>' : ''}
    `;

    card.addEventListener('click', () => showSystemDetail(systemName, sys));
    return card;
}

// -------------------------------------------------------------------------
// Detail modal
// -------------------------------------------------------------------------

function showSystemDetail(systemName, sys) {
    const modal      = document.getElementById('detailModal');
    const deviceType = sys.device_type || 'unknown';
    document.getElementById('modalNasName').textContent =
        `${systemName}  (${deviceType})`;

    let html = `
        <p><strong>Last update:</strong> ${escapeHtml(formatTimestamp(sys.timestamp))}</p>
        ${sys.first_seen ? `<p><strong>First seen:</strong> ${escapeHtml(formatTimestamp(sys.first_seen))}</p>` : ''}
        ${sys.system_id  ? `<p><strong>System ID:</strong>  ${escapeHtml(sys.system_id)}</p>` : ''}
    `;

    // --- Metrics detail ---
    if (sys.metrics) {
        const m = sys.metrics;
        html += `
            <h3>System Metrics</h3>
            <table style="width:100%;border-collapse:collapse;">
                ${metricRow('CPU Usage',       safeFixed(m.cpu_percent, 2) + '%')}
                ${metricRow('RAM Usage',       safeFixed(m.ram_percent, 2) + '%')}
                ${metricRow('Uptime',          escapeHtml(m.uptime_formatted || '0s'))}
                ${metricRow('Download (avg)',  safeFixed(m.network_bandwidth_in_mbps, 2) + ' Mbps')}
                ${metricRow('Upload (avg)',    safeFixed(m.network_bandwidth_out_mbps, 2) + ' Mbps')}
                ${metricRow('Total received',  formatBytes(m.network_bytes_in  || 0))}
                ${metricRow('Total sent',      formatBytes(m.network_bytes_out || 0))}
            </table>`;
    }

    // --- Disk detail ---
    if (sys.disk) {
        const d = sys.disk;
        html += `
            <h3 style="margin-top:20px;">Storage</h3>
            <table style="width:100%;border-collapse:collapse;">
                ${metricRow('Total usage', escapeHtml(d.total_usage_formatted))}
            </table>`;

        if ((d.volumes || []).length > 0) {
            html += `<h4 style="margin:16px 0 6px;">By Volume</h4>
                     <table style="width:100%;border-collapse:collapse;">`;
            for (const vol of d.volumes) {
                html += metricRow(escapeHtml(vol.path), escapeHtml(vol.usage_formatted));
            }
            html += '</table>';
        }

        if ((d.folders || []).length > 0) {
            html += `<h4 style="margin:16px 0 6px;">Folders</h4>
                     <table style="width:100%;border-collapse:collapse;">`;
            for (const f of d.folders) {
                html += metricRow(escapeHtml(f.path), escapeHtml(f.usage_formatted));
            }
            html += '</table>';
        }
    }

    // --- Running totals section ---
    if (sys.totals) {
        const t = sys.totals;
        html += `
            <h3 style="margin-top:20px;">Running Totals</h3>
            <table style="width:100%;border-collapse:collapse;">
                ${metricRow('Lifetime received', formatBytes(t.total_bytes_in  || 0))}
                ${metricRow('Lifetime sent',     formatBytes(t.total_bytes_out || 0))}
                ${metricRow('Current storage',   formatBytes(t.total_disk_bytes || 0))}
            </table>`;
    }

    document.getElementById('modalContent').innerHTML = html;
    modal.style.display = 'block';
}

function metricRow(label, value) {
    return `<tr style="border-bottom:1px solid #ddd;">
                <td style="padding:8px;">${label}</td>
                <td style="padding:8px;text-align:right;font-weight:bold;">${value}</td>
            </tr>`;
}

// -------------------------------------------------------------------------
// Modal setup
// -------------------------------------------------------------------------

function setupModal() {
    const modal    = document.getElementById('detailModal');
    const closeBtn = document.querySelector('.close');
    closeBtn.onclick = () => { modal.style.display = 'none'; };
    window.onclick  = e  => { if (e.target === modal) modal.style.display = 'none'; };
}

// -------------------------------------------------------------------------
// Utilities
// -------------------------------------------------------------------------

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
    return isNaN(n) ? '—' : n.toFixed(decimals);
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

function escapeHtml(text) {
    if (text === null || text === undefined) return '';
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}
