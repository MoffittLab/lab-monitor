/**
 * Lab Monitor Dashboard - Frontend Logic
 */

let refreshInterval = 30000; // Default 30 seconds
let lastUpdateTime = null;

/**
 * Initialize dashboard
 */
function initDashboard(config) {
    refreshInterval = config.refreshInterval || 30000;
    
    // Setup modal close handler
    setupModal();
    
    // Initial load
    loadData();
    
    // Set up refresh interval
    setInterval(loadData, refreshInterval);
    
    console.log(`Dashboard initialized. Refresh interval: ${refreshInterval}ms`);
}

/**
 * Load data from API
 */
function loadData() {
    fetch('/api/data')
        .then(response => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then(data => {
            updateDashboard(data);
            updateStatus('connected');
        })
        .catch(error => {
            console.error('Error loading data:', error);
            updateStatus('error');
            document.getElementById('nasGrid').innerHTML = 
                `<div class="loading">⚠️ Error loading data: ${error.message}</div>`;
        });
}

/**
 * Update dashboard with data
 */
function updateDashboard(data) {
    const nasGrid = document.getElementById('nasGrid');
    const nasSystems = data.nas_systems || {};
    
    // Update summary
    const nasCount = Object.keys(nasSystems).length;
    document.getElementById('nasCount').textContent = nasCount;
    
    let totalUsage = 0;
    for (const nas of Object.values(nasSystems)) {
        totalUsage += nas.total_usage_bytes || 0;
    }
    document.getElementById('totalUsage').textContent = formatBytes(totalUsage);
    
    // Update timestamp
    if (data.timestamp) {
        const now = new Date(data.timestamp);
        document.getElementById('lastUpdate').textContent = 
            `Last updated: ${now.toLocaleTimeString()}`;
        document.getElementById('lastUpdateTime').textContent = now.toLocaleTimeString();
    }
    
    // Clear grid
    nasGrid.innerHTML = '';
    
    // Add NAS cards
    if (nasCount === 0) {
        nasGrid.innerHTML = '<div class="loading">No NAS systems reporting yet</div>';
        return;
    }
    
    for (const [nasName, nas] of Object.entries(nasSystems)) {
        const card = createNasCard(nasName, nas);
        nasGrid.appendChild(card);
    }
}

/**
 * Create NAS card element
 */
function createNasCard(nasName, nas) {
    const card = document.createElement('div');
    card.className = 'nas-card';
    
    const timestamp = nas.timestamp ? new Date(nas.timestamp).toLocaleString() : 'Unknown';
    
    // Estimate percentage (simplified - would need capacity info for real %)
    const totalBytes = nas.total_usage_bytes || 0;
    
    // Create folder list
    let folderHtml = '';
    if (nas.folders && nas.folders.length > 0) {
        for (const folder of nas.folders.slice(0, 5)) { // Show top 5
            folderHtml += `
                <div class="folder-item">
                    <span class="folder-path">${folder.path}</span>
                    <span class="folder-size">${folder.usage_formatted}</span>
                </div>
            `;
        }
        if (nas.folders.length > 5) {
            folderHtml += `
                <div class="folder-item">
                    <span class="folder-path">... and ${nas.folders.length - 5} more</span>
                </div>
            `;
        }
    }
    
    card.innerHTML = `
        <div class="nas-card-header">
            <div>
                <div class="nas-name">${nasName}</div>
                <div class="nas-id">${nas.nas_id || 'N/A'}</div>
            </div>
            <div class="timestamp">${timestamp}</div>
        </div>
        
        <div class="usage-stats">
            <span>Total Usage</span>
            <span class="usage-value">${nas.total_usage_formatted}</span>
        </div>
        
        <div class="usage-bar">
            <div class="usage-fill" style="width: 45%;"></div>
        </div>
        
        <div class="folder-list">
            ${folderHtml || '<div class="folder-item">No folders</div>'}
        </div>
    `;
    
    // Click handler for modal
    card.addEventListener('click', () => showNasDetail(nasName, nas));
    
    return card;
}

/**
 * Show NAS detail modal
 */
function showNasDetail(nasName, nas) {
    const modal = document.getElementById('detailModal');
    document.getElementById('modalNasName').textContent = nasName;
    
    let folderHtml = '<h3>Folders</h3>';
    if (nas.folders && nas.folders.length > 0) {
        folderHtml += '<table style="width:100%;border-collapse:collapse;">';
        for (const folder of nas.folders) {
            folderHtml += `
                <tr style="border-bottom:1px solid #ddd;">
                    <td style="padding:8px;">${folder.path}</td>
                    <td style="padding:8px;text-align:right;font-weight:bold;">${folder.usage_formatted}</td>
                </tr>
            `;
        }
        folderHtml += '</table>';
    }
    
    const timestamp = nas.timestamp ? new Date(nas.timestamp).toLocaleString() : 'Unknown';
    document.getElementById('modalContent').innerHTML = `
        <p><strong>NAS ID:</strong> ${nas.nas_id || 'N/A'}</p>
        <p><strong>Last Update:</strong> ${timestamp}</p>
        <p><strong>Total Usage:</strong> ${nas.total_usage_formatted}</p>
        ${folderHtml}
    `;
    
    modal.style.display = 'block';
}

/**
 * Setup modal close handler
 */
function setupModal() {
    const modal = document.getElementById('detailModal');
    const closeBtn = document.querySelector('.close');
    
    closeBtn.onclick = () => {
        modal.style.display = 'none';
    };
    
    window.onclick = (event) => {
        if (event.target === modal) {
            modal.style.display = 'none';
        }
    };
}

/**
 * Update connection status indicator
 */
function updateStatus(status) {
    const statusEl = document.getElementById('status');
    statusEl.textContent = status === 'connected' ? '🟢 Connected' : '🔴 Error';
    statusEl.className = `status ${status}`;
}

/**
 * Format bytes to human-readable
 */
function formatBytes(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let size = bytes;
    
    for (let unit of units) {
        if (size < 1024) {
            return `${size.toFixed(2)} ${unit}`;
        }
        size /= 1024;
    }
    
    return `${size.toFixed(2)} PB`;
}
