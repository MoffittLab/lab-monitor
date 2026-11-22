'use strict';
/*
 * Behavioral XSS / formatting checks for dashboard/static/js/dashboard.js.
 * Loads the script in a bare vm context (no DOM needed for the pure HTML
 * builders), renders cards and summary buttons from hostile Manager data, and
 * checks that the markup only contains the dashboard's own tags/attributes.
 *
 * Usage: node dashboard_xss_harness.js <path/to/dashboard.js>
 * Exit code 0 = pass; failures are printed one per line.
 */
const fs = require('fs');
const vm = require('vm');

const jsPath = process.argv[2];

// Node < 15 (e.g. 14.x on the Windows server) has no global AbortController:
// a minimal stand-in with what dashboard.js and the fake fetch below use.
class MiniAbortController {
    constructor() {
        const listeners = [];
        this.signal = {
            aborted: false,
            addEventListener(type, fn) { if (type === 'abort') listeners.push(fn); },
            removeEventListener(type, fn) { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
        };
        this._listeners = listeners;
    }
    abort() {
        if (this.signal.aborted) return;
        this.signal.aborted = true;
        for (const fn of this._listeners.slice()) fn({ type: 'abort' });
    }
}
const AbortControllerImpl = typeof AbortController === 'undefined' ? MiniAbortController : AbortController;
const ctx = vm.createContext({ console, setTimeout, clearTimeout, AbortController: AbortControllerImpl });
vm.runInContext(fs.readFileSync(jsPath, 'utf8'), ctx, { filename: jsPath });

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

const P   = `"'><img src=x onerror=alert(1)>`;
const CSS = '50;background:url(javascript:alert(1))';

function hostileSystem(deviceType) {
    return {
        name: P,
        device_type: deviceType,
        users: [
            { username: P + '\\' + P, cpu_percent: CSS, ram_bytes: P, ram_formatted: P },
            { username: P, cpu_percent: P, ram_bytes: 5, ram_formatted: P },
        ],
        metrics: {
            timestamp: P, cpu_percent: CSS, ram_percent: P, total_ram_bytes: P,
            uptime_formatted: P, network_bandwidth_in_mbps: CSS, network_bandwidth_out_mbps: P,
            units: { cpu_percent: P, ram_percent: P, network_bandwidth_in_mbps: P, network_bandwidth_out_mbps: P },
            gpus: [
                { name: P, gpu_percent: CSS, vram_used_bytes: P, vram_total_bytes: P,
                  vram_used_formatted: P, vram_total_formatted: P, temperature_c: P, power_watts: CSS },
                { name: P, gpu_percent: 95, vram_used_bytes: 5, vram_total_bytes: 10,
                  temperature_c: '70"><b>', power_watts: 200 },
            ],
        },
        disk: {
            timestamp: P,
            volumes: [
                { path: P, usage_bytes: CSS, total_bytes: '100', usage_formatted: P, total_formatted: P },
                { path: P, usage_bytes: P, usage_formatted: P },
            ],
            folders: [{ path: P, usage_bytes: P, usage_formatted: P }, { path: P, usage_bytes: CSS }],
            timed_out: [P, P, P, P],
            errors: [P],
        },
    };
}

const ALLOWED_TAGS  = new Set(['div', 'span']);
const ALLOWED_ATTRS = new Set(['class', 'style', 'title', 'data-metric', 'data-volume']);
const TAG_RE = /<\/?([a-zA-Z0-9]+)((?:\s+[a-zA-Z-]+="[^"]*")*)\s*>/y;

function checkMarkup(out, label) {
    let text = '';
    let i = 0;
    while (i < out.length) {
        const lt = out.indexOf('<', i);
        text += out.slice(i, lt === -1 ? out.length : lt);
        if (lt === -1) break;
        TAG_RE.lastIndex = lt;
        const m = TAG_RE.exec(out);
        if (!m) {
            failures.push(`${label}: malformed or injected markup at: ${out.slice(lt, lt + 80)}`);
            break;
        }
        check(ALLOWED_TAGS.has(m[1]), `${label}: unexpected tag <${m[1]}>`);
        for (const a of m[2].matchAll(/([a-zA-Z-]+)="([^"]*)"/g)) {
            const [, name, value] = a;
            check(ALLOWED_ATTRS.has(name), `${label}: unexpected attribute ${name}="${value}"`);
            if (name === 'style') check(/^width:\d+(\.\d+)?%$/.test(value), `${label}: unsafe style "${value}"`);
            if (name === 'class') check(/^[a-z0-9 -]*$/.test(value), `${label}: unexpected class "${value}"`);
            if (name === 'data-metric') check(/^[a-z0-9_]+$/.test(value), `${label}: unexpected data-metric "${value}"`);
            check(!/[<>'`]/.test(value), `${label}: unescaped character in ${name}="${value}"`);
        }
        i = lt + m[0].length;
    }
    check(!/[<>"']/.test(text), `${label}: unescaped < > " ' in text content`);
    check(!out.includes('javascript:'), `${label}: numeric field leaked as text (javascript: payload)`);
    check(out.includes('&lt;img src=x onerror=alert(1)&gt;'), `${label}: hostile text not rendered escaped`);
}

const STATES = [
    { state: 'ok', ageMs: 60 * 1000 },
    { state: 'stale', ageMs: 30 * 60 * 1000 },
    { state: 'offline', ageMs: null },
    { state: 'offline', ageMs: 5 * 60 * 60 * 1000 },
];

for (const type of ['server', 'nas', P]) {
    for (const st of STATES) {
        checkMarkup(ctx.buildSystemCardHtml(P, hostileSystem(type), st), `card[${type.slice(0, 6)}/${st.state}]`);
        checkMarkup(ctx.buildSummaryButtonHtml(P, hostileSystem(type), st), `strip[${type.slice(0, 6)}/${st.state}]`);
    }
}

// --- unit checks of the helpers ---
check(ctx.escapeHtml(`&<>"'`) === '&amp;&lt;&gt;&quot;&#39;', 'escapeHtml must escape & < > " \'');
check(ctx.escapeHtml(null) === '' && ctx.escapeHtml(0) === '0', 'escapeHtml null/0');
check(ctx.formatBytes(null) === '—' && ctx.formatBytes(undefined) === '—', 'formatBytes(null) must be —');
check(ctx.formatBytes(0) === '0 B', 'formatBytes(0) must be 0 B, got ' + ctx.formatBytes(0));
check(ctx.formatBytes(1536) === '1.50 KiB', 'formatBytes uses binary units, got ' + ctx.formatBytes(1536));
check(ctx.formatBytes(500 * 1024 ** 3) === '500.00 GiB', 'small volume must not round to 0 TB');
check(ctx.formatBytes(3 * 1024 ** 4) === '3.00 TiB', 'formatBytes TiB');
check(ctx.fmtWidth('100;x') === '100' && ctx.fmtWidth(150) === '100' && ctx.fmtWidth(null) === '0', 'fmtWidth clamps');
check(ctx.safeFixed(null, 1) === '—' && ctx.safeFixed('12.345', 1) === '12.3', 'safeFixed');
check(ctx.fmtValue(null, 1, '%') === '—', 'fmtValue null must be —');
check(ctx.parseTs('2026-08-04T18:30:00') === Date.UTC(2026, 7, 4, 18, 30, 0), 'naive Manager timestamps are UTC');
check(ctx.parseTs('2026-08-04T18:30:00Z') === Date.UTC(2026, 7, 4, 18, 30, 0), 'Z timestamps');
check(ctx.parseTs('garbage') === null && ctx.parseTs(null) === null, 'parseTs invalid');

// Unmeasured (null) metrics render as "—", never 0 (contract 4)
const nullSys = { device_type: 'server', metrics: { timestamp: new Date().toISOString(),
    cpu_percent: null, ram_percent: null, network_bandwidth_in_mbps: null, network_bandwidth_out_mbps: null,
    units: {}, gpus: [] } };
const nullCard = ctx.buildSystemCardHtml('A', nullSys, STATES[0]);
check((nullCard.match(/<span class="metric-value">—<\/span>/g) || []).length === 5,
      'null metrics must render as — (got: ' + nullCard + ')');
check(!/0\.0%/.test(nullCard), 'null metrics must not render as 0');

// Card and summary strip use the same thresholds
for (const [cpu, level] of [[49, ''], [60, 'warning'], [80, 'danger']]) {
    const sys = { device_type: 'server', metrics: { timestamp: new Date().toISOString(), cpu_percent: cpu,
                  ram_percent: null, units: {}, gpus: [] } };
    const card  = ctx.buildSystemCardHtml('A', sys, STATES[0]);
    const strip = ctx.buildSummaryButtonHtml('A', sys, STATES[0]);
    const cardLevel  = (card.match(/class="metric-item ([a-z]*)" data-metric="cpu_percent"/) || [])[1];
    const stripLevel = (strip.match(/class="sys-btn-metric ([a-z]*)"/) || [])[1];
    check(cardLevel === level && stripLevel === level,
          `cpu ${cpu}%: card "${cardLevel}" / strip "${stripLevel}", expected "${level}"`);
}
for (const [usedPct, level] of [[50, ''], [80, 'warning'], [95, 'danger']]) {
    const sys = { device_type: 'nas', disk: { timestamp: new Date().toISOString(),
                  volumes: [{ path: '/volume1', usage_bytes: usedPct, total_bytes: 100 }], folders: [] } };
    const card  = ctx.buildSystemCardHtml('A', sys, STATES[0]);
    const strip = ctx.buildSummaryButtonHtml('A', sys, STATES[0]);
    const cardLevel  = (card.match(/<div class="usage-bar ([a-z]*)">/) || [])[1];
    const stripLevel = (strip.match(/<div class="usage-bar ([a-z]*)">/) || [])[1];
    check(cardLevel === level && stripLevel === level,
          `volume ${usedPct}%: card "${cardLevel}" / strip "${stripLevel}", expected "${level}"`);
}

// Small volumes are shown in GiB, not "0 TB"
const smallNas = { device_type: 'nas', disk: { timestamp: new Date().toISOString(),
                   volumes: [{ path: '/volume1', usage_bytes: 200 * 1024 ** 3, total_bytes: 500 * 1024 ** 3 }] } };
const smallStrip = ctx.buildSummaryButtonHtml('A', smallNas, STATES[0]);
check(smallStrip.includes('200.00 GiB / 500.00 GiB'), 'summary strip byte units: ' + smallStrip);

// Disk snapshot age is flagged after diskStaleHours; scan warnings are shown
const oldDisk = { device_type: 'nas', disk: { timestamp: new Date(Date.now() - 48 * 3600 * 1000).toISOString(),
                  volumes: [], folders: [], timed_out: ['/volume1/Big'], errors: [] } };
const oldCard = ctx.buildSystemCardHtml('A', oldDisk, STATES[0]);
check(oldCard.includes('class="disk-scan old"'), 'disk snapshot older than 36 h must be flagged');
check(oldCard.includes('timed out') && oldCard.includes('/volume1/Big'), 'timed-out folders must be listed');

// --- Degraded polls keep the last good sections (no false "Offline") ---
const freshTs = new Date(Date.now() - 60 * 1000).toISOString();
function goodPoll() {
    return {
        success: true, degraded: false, errors: [], timestamp: freshTs,
        global_totals: { total_bytes_in: 10, total_bytes_out: 20 },
        systems: {
            Atlas:  { name: 'Atlas', device_type: 'server', totals: { total_bytes_in: 1 },
                      metrics: { timestamp: freshTs, cpu_percent: 10, ram_percent: 20, units: {}, gpus: [] },
                      users: [{ username: 'alice', cpu_percent: 1, ram_bytes: 5 }], disk: null },
            Triton: { name: 'Triton', device_type: 'nas', metrics: { timestamp: freshTs, units: {}, gpus: [] },
                      disk: { timestamp: freshTs, folders: [],
                              volumes: [{ path: '/volume1', usage_bytes: 50, total_bytes: 100 }] } },
        },
    };
}
// What app.py returns when only these endpoints failed: their sections are empty
function degradedPoll(failedPaths, extraSystem) {
    const d = goodPoll();
    d.degraded = true;
    d.errors = failedPaths.map(endpoint => ({ endpoint, error: 'timeout' }));
    for (const sys of Object.values(d.systems)) {
        if (failedPaths.includes('/api/metrics/all'))      sys.metrics = null;
        if (failedPaths.includes('/api/usage/all'))        sys.disk = null;
        if (failedPaths.includes('/api/user_metrics/all')) delete sys.users;
        if (failedPaths.includes('/api/totals'))           sys.totals = null;
    }
    if (failedPaths.includes('/api/totals')) d.global_totals = {};
    if (extraSystem) d.systems[extraSystem] = { name: extraSystem, device_type: 'server', metrics: null, disk: null };
    return d;
}
const ALL_FAILING = ['/api/metrics/all', '/api/usage/all', '/api/user_metrics/all', '/api/totals'];

{
    const merged = ctx.mergeLastGood(degradedPoll(ALL_FAILING, 'NewBox'), goodPoll());
    const atlas = merged.systems.Atlas;
    check(atlas.metrics && atlas.metrics.cpu_percent === 10, 'metrics carried over from the last good poll');
    check(atlas.users && atlas.users.length === 1, 'users carried over from the last good poll');
    check(merged.systems.Triton.disk && merged.systems.Triton.disk.volumes.length === 1, 'disk carried over');
    check(merged.global_totals.total_bytes_in === 10, 'global totals carried over');
    check(JSON.stringify(atlas.staleSections) === JSON.stringify(['metrics', 'users', 'totals']),
          'staleSections: ' + JSON.stringify(atlas.staleSections));
    check(ctx.systemState(atlas).state === 'ok', 'carried-over fresh metrics keep the system ok');
    const card = ctx.buildSystemCardHtml('Atlas', atlas, ctx.systemState(atlas));
    check(!card.includes('Offline'), 'degraded card must not say Offline: ' + card);
    check(card.includes('card-section stale-data'), 'carried-over sections must be greyed out');
    check(card.includes('Showing metrics, user activity from an earlier refresh'), 'stale notice: ' + card);
    // Stale/unavailable markup stays safe with hostile data
    const hs = hostileSystem('server');
    hs.staleSections = ['metrics', 'users', 'disk', 'totals'];
    hs.unavailable = ['disk', 'users'];
    checkMarkup(ctx.buildSystemCardHtml(P, hs, STATES[0]), 'degraded hostile card');

    // A system first seen in this degraded poll has nothing to carry over
    const nb = merged.systems.NewBox;
    const nbState = ctx.systemState(nb);
    check(nbState.state === 'unknown', 'metrics call failed, no earlier copy -> unknown, got ' + nbState.state);
    const nbCard  = ctx.buildSystemCardHtml('NewBox', nb, nbState);
    const nbStrip = ctx.buildSummaryButtonHtml('NewBox', nb, nbState);
    check(!nbCard.includes('Offline') && !nbStrip.includes('Offline'), 'unknown must not render as Offline');
    check(nbCard.includes('Metrics unavailable') && nbCard.includes('Unavailable right now: storage, user activity'),
          'unknown card: ' + nbCard);
    check(!nbCard.includes('No data yet'), 'unknown card must not claim there is no data');
    check(nbStrip.includes('sys-btn-badge unknown'), 'strip badge for unknown: ' + nbStrip);
}
{
    // First load (no last good data) with only /api/metrics/all failing
    const merged = ctx.mergeLastGood(degradedPoll(['/api/metrics/all']), null);
    for (const [name, sys] of Object.entries(merged.systems)) {
        const st = ctx.systemState(sys);
        check(st.state === 'unknown', `${name}: first-load metrics failure -> unknown, got ${st.state}`);
        check(!ctx.buildSystemCardHtml(name, sys, st).includes('Offline'), `${name}: no false Offline banner`);
    }
    check(merged.systems.Triton.disk !== null, 'sections that did not fail are kept');
    // A system that genuinely never reported is still offline on a healthy poll
    const never = { device_type: 'server', metrics: null, disk: null };
    check(ctx.systemState(never).state === 'offline', 'no reports on a healthy poll -> offline');
}

// --- HTTP failure labels: only the dashboard's own JSON blames the Manager ---
check(ctx.describeHttpFailure(502, { success: false, error: 'Manager unavailable', errors: [] }) === 'Manager unreachable',
      '502 from the dashboard server -> Manager unreachable');
check(ctx.describeHttpFailure(502, null) === 'Dashboard server unreachable (HTTP 502)',
      'proxy 502 (HTML body) -> dashboard server: ' + ctx.describeHttpFailure(502, null));
check(ctx.describeHttpFailure(504, null) === 'Dashboard server unreachable (HTTP 504)', 'proxy 504');
check(ctx.describeHttpFailure(500, { success: false, error: 'Internal error' }) === 'Dashboard error (HTTP 500)',
      'dashboard 500');

// --- CSV export marks averaged / truncated data ---
vm.runInContext(`chart.type = 'metric'; chart.system = 'Triton'; chart.field = 'cpu_percent'; chart.range = 'all';
                 chart.data = [{ timestamp: '2026-01-01T00:00:00Z', value: 1.5 }, { timestamp: 't"2', value: null }];
                 chart.meta = { downsampled: true, truncated: false };`, ctx);
check(ctx.chartCsvText() === 'timestamp,value_avg\n"2026-01-01T00:00:00Z",1.5\n"t""2",\n', 'averaged CSV: ' + ctx.chartCsvText());
check(/^Triton_cpu_percent_all_avg_\d{8}T\d{6}Z\.csv$/.test(ctx.csvFilename()), 'averaged CSV name: ' + ctx.csvFilename());
vm.runInContext(`chart.meta = { downsampled: false, truncated: true }; chart.range = '24';`, ctx);
check(ctx.chartCsvText().startsWith('timestamp,value\n'), 'raw CSV header');
check(/^Triton_cpu_percent_24h_truncated_\d{8}T\d{6}Z\.csv$/.test(ctx.csvFilename()), 'truncated CSV name: ' + ctx.csvFilename());
vm.runInContext(`chart.meta = null;`, ctx);
check(/^Triton_cpu_percent_24h_\d{8}T\d{6}Z\.csv$/.test(ctx.csvFilename()), 'plain CSV name: ' + ctx.csvFilename());

// --- Minimal DOM stubs for the loader and chart helpers ---
function stubEl() {
    return { textContent: '', className: '', hidden: false, dataset: {}, removed: false,
             classList: { toggle() {}, add() {}, remove() {} },
             querySelector: () => null, querySelectorAll: () => [],
             prepend() {}, replaceChildren() {}, remove() { this.removed = true; } };
}
const els = {};
const chartErrors = [];
ctx.document = {
    hidden: false,
    getElementById: id => (els[id] = els[id] || stubEl()),
    createElement: () => stubEl(),
    querySelectorAll: sel => (sel === '#metricChart > .chart-error' ? chartErrors.splice(0) : []),
    addEventListener() {},
};

// The chart error paragraph is removed before the next plot
{
    const errP = stubEl();
    chartErrors.push(errP);
    let plotted = 0;
    ctx.Plotly = { Icons: {}, newPlot() { plotted++; }, purge() {} };
    ctx.plotChart([], {});
    check(errP.removed && plotted === 1, 'plotChart must remove a leftover .chart-error');
}

async function asyncChecks() {
    // A request that never settles is aborted and reported; polling resumes
    vm.runInContext('dataTimeoutMs = 30; lastGoodData = null;', ctx);
    ctx.fetch = (url, opts) => new Promise((resolve, reject) => {
        opts.signal.addEventListener('abort', () => {
            const e = new Error('The operation was aborted'); e.name = 'AbortError'; reject(e);
        });
    });
    await ctx.loadData();
    check(els.status.textContent.includes('Dashboard server not responding'),
          'hung /api/data must be reported, status: ' + els.status.textContent);
    check(vm.runInContext('loadInFlight', ctx) === false, 'loadInFlight must be cleared after a timeout');

    // A reverse proxy's 502 (HTML body) is not blamed on the Manager
    ctx.fetch = async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError('html'); } });
    await ctx.loadData();
    check(els.status.textContent.includes('Dashboard server unreachable (HTTP 502)'),
          'proxy 502 status: ' + els.status.textContent);

    // The dashboard server's own 502 is
    ctx.fetch = async () => ({ ok: false, status: 502,
        json: async () => ({ success: false, error: 'Manager unavailable', errors: [{ endpoint: '/api/devices', error: 'timeout' }] }) });
    await ctx.loadData();
    check(els.status.textContent.includes('Manager unreachable'), 'manager 502 status: ' + els.status.textContent);
    check(els.connectionBanner.textContent.includes('/api/devices: timeout'), 'banner lists the failing call');
}

asyncChecks().catch(err => failures.push('async checks threw: ' + (err && err.stack || err))).then(() => {
    if (failures.length) {
        for (const f of failures) console.log('FAIL: ' + f);
        process.exit(1);
    }
    console.log('OK');
});
