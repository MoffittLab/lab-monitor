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
const ctx = vm.createContext({ console, setTimeout, clearTimeout, AbortController: AbortControllerImpl,
                               URLSearchParams });
vm.runInContext(fs.readFileSync(jsPath, 'utf8'), ctx, { filename: jsPath });

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }

const P   = `"'><img src=x onerror=alert(1)>`;
const CSS = '50;background:url(javascript:alert(1))';
const isoAgo = minutes => new Date(Date.now() - minutes * 60 * 1000).toISOString();

// Every field that renders client-controlled text carries P: usernames
// (per-user table, gpus[].users, sessions, usage, owners), client addresses
// and host names, service and source names, disk ids / models, array and
// volume ids, health reasons, folder paths, disk_busy_max_name.  Numbers
// carry CSS or P and must render as digits or "—".
function hostileSystem(deviceType) {
    const fresh = isoAgo(1);
    return {
        name: P,
        device_type: deviceType,
        users: [
            { username: P + '\\' + P, cpu_percent: CSS, ram_bytes: P, ram_formatted: P,
              cpu_cores: CSS, cpu_seconds: P, interval_s: CSS,
              gpus_held: CSS, gpu_util_percent: P, gpu_vram_bytes: CSS, gpu_processes: P },
            { username: P, cpu_percent: P, ram_bytes: 5, ram_formatted: P, gpus_held: 0 },
            { username: P, gpus_held: null, cpu_cores: null },
        ],
        metrics: {
            timestamp: P, cpu_percent: CSS, ram_percent: P, total_ram_bytes: P,
            uptime_formatted: P, network_bandwidth_in_mbps: CSS, network_bandwidth_out_mbps: P,
            units: { cpu_percent: P, ram_percent: P, network_bandwidth_in_mbps: P, network_bandwidth_out_mbps: P },
            interval_s: CSS, cpu_count: P, cpu_iowait_percent: CSS, cpu_steal_percent: CSS,
            proc_cpu_coverage_percent: P, disk_read_bytes_per_s: CSS, disk_write_bytes_per_s: P,
            disk_read_iops: CSS, disk_write_iops: P, disk_await_ms: CSS, disk_busy_max_percent: CSS,
            disk_busy_max_name: P,
            gpus: [
                { name: P, gpu_percent: CSS, vram_used_bytes: P, vram_total_bytes: P,
                  vram_used_formatted: P, vram_total_formatted: P, temperature_c: P, power_watts: CSS,
                  uuid: P, process_source: P, util_split: P,
                  users: [{ username: P + '\\' + P, vram_bytes: CSS, util_percent: P, processes: P },
                          { username: P, vram_bytes: 5 }, { username: P }, P, null],
                  users_truncated: CSS, unattributed_vram_bytes: CSS, unattributed_processes: P },
                { name: P, gpu_percent: 95, vram_used_bytes: 5, vram_total_bytes: 10,
                  temperature_c: '70"><b>', power_watts: 200, users: P },
                { name: P, users: null, unattributed_vram_bytes: 5, unattributed_processes: 1 },
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
        sessions: {
            timestamp: fresh, list_timestamp: P, list_confirmed_at: fresh,
            summary: { clients_total: CSS, users_total: P, smb_clients: CSS, ssh_users: P, rdp_clients: 1,
                       rdp_disconnected: CSS, [P]: 5, _detail: P, sessions_truncated: true,
                       _sources: { [P]: P, smb: 'not_permitted', tcp: P } },
            sessions: [
                { service: P, username: P + '\\' + P, client: P, client_host: P, since: P,
                  state: 'disconnected', connections: CSS, source: P },
                { service: 'smb', client: P, client_host: P, connections: 2, source: 'tcp' },
                { service: P, username: P, since: '2026-11-21T12:35:00Z' },
                P, null,
            ],
        },
        storage_health: {
            timestamp: fresh, overall_status: 'warning', status_reasons: [P, P],
            sources: { [P]: P, smart: 'not_permitted', md: P },
            arrays: [{ id: P, level: P, state: P, status: 'warning', sync_action: P, sync_percent: CSS,
                       sync_eta_seconds: CSS, members_total: P, members_active: 3, role: P }, P],
            disks: [{ id: P, model: P, status: 'critical', temperature_c: CSS, bus: P, pending_sectors: CSS,
                      io_errors: P, wear_used_percent: CSS },
                    { id: P, model: P, status: P, temperature_c: 40, bus: 'usb' }, null],
            volumes: [{ id: P, status: 'critical', health: P, read_only: true, fs: P }],
            events: { [P]: CSS },
        },
        breakdown: {
            timestamp: P, owner_source: P, reference_time: P, cold_thresholds_days: [P, CSS],
            folders: [{ path: P, bytes: 100, cold_1y_bytes: CSS, cold_3y_bytes: 10, top_owner: P,
                        top_owner_bytes: 40, folder_owner: P }, P],
            device: { bytes: CSS,
                      owners: [{ owner: P + '\\' + P, bytes: CSS, cold_1y_bytes: P },
                               ...[1, 2, 3, 4, 5, 6].map(i => ({ owner: P + i, bytes: i * 10, cold_1y_bytes: CSS })), P],
                      other: { owner_count: CSS, bytes: P } },
        },
        usage: { window: P, complete: false,
                 users: [{ username: P + '\\' + P, gpu_busy_hours: CSS, gpu_held_hours: P },
                         { username: P, cpu_core_hours: CSS }, P] },
    };
}

const ALLOWED_TAGS  = new Set(['div', 'span']);
const ALLOWED_ATTRS = new Set(['class', 'style', 'title', 'data-metric', 'data-volume',
                               'data-type', 'data-entity', 'data-folder', 'data-expand']);
const DATA_TYPES    = new Set(['user_metrics', 'sessions', 'storage_health', 'storage_breakdown']);
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
        const attrs = Object.fromEntries([...m[2].matchAll(/([a-zA-Z-]+)="([^"]*)"/g)].map(a => [a[1], a[2]]));
        for (const [name, value] of Object.entries(attrs)) {
            check(ALLOWED_ATTRS.has(name), `${label}: unexpected attribute ${name}="${value}"`);
            if (name === 'style') check(/^width:\d+(\.\d+)?%$/.test(value), `${label}: unsafe style "${value}"`);
            if (name === 'class') check(/^[a-z0-9 -]*$/.test(value), `${label}: unexpected class "${value}"`);
            // Storage-owner charts name the owner in the field (escaped); every other field is an identifier
            if (name === 'data-metric') {
                check(attrs['data-type'] === 'storage_breakdown' ? /^owner\/.+\/bytes$/.test(value) : /^[a-z0-9_]+$/.test(value),
                      `${label}: unexpected data-metric "${value}"`);
            }
            if (name === 'data-type')   check(DATA_TYPES.has(value), `${label}: unexpected data-type "${value}"`);
            if (name === 'data-expand') check(value === 'owners', `${label}: unexpected data-expand "${value}"`);
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

// --- Sections added with the usage-metrics Manager ---
const setFeatures = f => vm.runInContext(`managerFeatures = ${JSON.stringify(f)};`, ctx);
const card = sys => ctx.buildSystemCardHtml('A', Object.assign({ device_type: 'server' }, sys), STATES[0]);
const okMetrics = extra => Object.assign({ timestamp: isoAgo(1), cpu_percent: 10, ram_percent: 20, units: {}, gpus: [] }, extra);

{
    // A CURRENT/OLD Manager's data: none of the new sections, nothing new on the card
    const plain = card({ metrics: okMetrics({}), users: [{ username: 'alice', cpu_percent: 1, ram_bytes: 5 }],
                         disk: { timestamp: isoAgo(60), volumes: [], folders: [{ path: '/volume1/Lab', usage_bytes: 5 }] } });
    for (const marker of ['Disk I/O', 'Connected', 'Top storage users', 'health-pill', 'I/O wait', 'gpu-users',
                          'usage-line', 'cold-bar', 'metric-note', 'users-gpu', 'session-chip']) {
        check(!plain.includes(marker), `no data -> no "${marker}" on the card`);
    }
    // The user table keeps the fb6498c columns: User | CPU | RAM
    check(!plain.includes('Cores') && !plain.includes('user-cores') && !plain.includes('>GPU<'),
          'no Cores or GPU column without measured data: ' + plain);
    check(/<span>User<\/span><span>CPU<\/span><span>RAM<\/span>\s*<\/div>/.test(plain), 'old 3-column header');
    const cores = card({ metrics: okMetrics({}), users: [{ username: 'a', cpu_percent: 1, cpu_cores: 1.5, cpu_seconds: 450 },
                                                         { username: 'b', cpu_percent: 1, cpu_cores: null }] });
    check(cores.includes('users-cores') && cores.includes('<span>Cores</span>') && cores.includes('>1.50</span>'), 'Cores column when measured');
    check(/<span class="user-cores"[^>]*>—<\/span>/.test(cores), 'unmeasured cores render as —');
    check(!card({ metrics: okMetrics({}), users: [{ username: 'a', cpu_cores: null, cpu_seconds: null }] }).includes('Cores'),
          'all-null cores (first run, or a NEW Manager with an old collector) -> no column');
    check(!ctx.buildSummaryButtonHtml('A', { device_type: 'server', metrics: okMetrics({}) }, STATES[0]).includes('sys-btn-health'),
          'no health glyph without storage health');
    // Empty optional sections render nothing either
    check(!card({ metrics: okMetrics({}), sessions: { summary: {}, sessions: [] } }).includes('Connected'), 'empty sessions hidden');
    check(!card({ breakdown: { device: { owners: [] }, folders: [] } }).includes('Top storage users'), 'no owners -> hidden');
    check(!card({ breakdown: { device: { owners: null } } }).includes('Top storage users'), 'owners not collected -> hidden');
}
{
    // CPU: 1-s sample vs interval average; I/O wait only when measured
    check(card({ metrics: okMetrics({ interval_s: null }) }).includes('title="CPU: 1-s sample"'), 'cpu tooltip without interval');
    const avg = card({ metrics: okMetrics({ interval_s: 300.2, cpu_count: 32, proc_cpu_coverage_percent: 96.5 }) });
    check(avg.includes('CPU: average over 300 s · 32 logical CPUs · 97% of it attributed'), 'cpu tooltip: ' + avg);
    check(!avg.includes('metric-note'), 'no window note for a normal interval');
    check(card({ metrics: okMetrics({ interval_s: 7800 }) }).includes('(avg 2h 10m)'), 'long interval note');
    check(card({ metrics: okMetrics({ cpu_iowait_percent: 1.2 }) }).includes('data-metric="cpu_iowait_percent"'), 'I/O wait shown');
    check(!card({ metrics: okMetrics({ cpu_iowait_percent: null }) }).includes('I/O wait'), 'null I/O wait hidden');
}
{
    // Disk I/O: hidden when every field is null; null items show —; busiest coloured 60/85
    const io = card({ metrics: okMetrics({ disk_read_bytes_per_s: 1250000, disk_write_bytes_per_s: null,
                                           disk_await_ms: 6.3, disk_busy_max_percent: 86, disk_busy_max_name: 'sata2' }) });
    check(io.includes('Disk I/O') && io.includes('1.25 MB/s') && io.includes('6.3 ms'), 'disk io values: ' + io);
    check(/data-metric="disk_write_bytes_per_s"[^>]*>\s*<span class="metric-label">Write<\/span>\s*<span class="metric-value">—<\/span>/.test(io),
          'null write -> —');
    check(io.includes('class="metric-item danger" data-metric="disk_busy_max_percent"') && io.includes('sata2'), 'busiest disk');
    check(/metric-item warning" data-metric="disk_busy_max_percent/.test(card({ metrics: okMetrics({ disk_busy_max_percent: 60 }) })),
          'busiest 60% is a warning');
    check(!card({ metrics: okMetrics({ disk_read_bytes_per_s: null, disk_read_iops: null, disk_await_ms: null }) }).includes('Disk I/O'),
          'all-null disk io hidden');
    check(ctx.formatRate(310000) === '310.0 KB/s' && ctx.formatRate(512) === '512 B/s' && ctx.formatRate(null) === '—',
          'formatRate: ' + ctx.formatRate(310000));
}
{
    // GPU per user (Appendix A.1 / A.2 shapes)
    const gpu = { index: 0, name: 'A6000', gpu_percent: 87, vram_used_bytes: 22548578304, vram_total_bytes: 51527024640,
                  process_source: 'nvml', util_split: 'single_user',
                  users: [{ username: 'LAB\\alice', vram_bytes: 21474836480, util_percent: 87.0, processes: 2 },
                          { username: 'bob', vram_bytes: 1073741824, util_percent: 3, processes: 1 },
                          { username: 'carol', vram_bytes: 1, util_percent: 0, processes: 1 }],
                  users_truncated: 1, unattributed_vram_bytes: 1073741824, unattributed_processes: 1 };
    const users = [
        { username: 'LAB\\alice', cpu_percent: 12.5, ram_bytes: 8589934592, cpu_seconds: 1200.4, cpu_cores: 4.0, interval_s: 300.2,
          gpu_vram_bytes: 21474836480, gpu_util_percent: 87.0, gpu_busy_seconds: 261.2, gpus_held: 1, gpu_held_seconds: 300.2, gpu_processes: 2 },
        { username: 'LAB\\bob', cpu_percent: null, ram_bytes: null, gpu_vram_bytes: 1073741824, gpu_util_percent: 0.0,
          gpus_held: 1, gpu_processes: 1 },
        { username: 'idle', cpu_percent: 1, ram_bytes: 5, gpus_held: 0 },
        { username: 'old-host', cpu_percent: 1, ram_bytes: 5, gpus_held: null },
    ];
    const c = card({ metrics: okMetrics({ gpus: [gpu] }), users });
    check(c.includes('alice 20.00 GiB 87% · bob 1.00 GiB 3% · +2'), 'gpu users line: ' + c);
    check(/title="LAB\\alice 20\.00 GiB 87%\nbob 1\.00 GiB 3%\ncarol 1 B 0%\nand 1 more users\nother 1\.00 GiB \(1 processes/.test(c),
          'gpu users tooltip lists everyone');
    check(c.includes('users-cores users-gpu') && c.includes('>GPU</span>'), 'GPU column shown');
    // 87% busy is a warning (THRESHOLDS.gpu); 20 GiB of 48 GiB total VRAM is not (THRESHOLDS.vram)
    check(/<span class="warning"[^>]*>1× 87%<\/span> · <span class=""[^>]*>20\.00 GiB<\/span>/.test(c), 'gpu cell: ' + c);
    check(c.includes('<span class="user-gpu muted"></span>'), 'measured zero GPUs -> muted empty cell');
    check(c.includes('<span class="user-gpu">—</span>'), 'unattributed GPUs -> —');
    check(c.includes('<span class="user-cores" title="1200.4 CPU-seconds in 300 s"'), 'cores tooltip: ' + c);
    check(c.includes('data-type="user_metrics" data-metric="cpu_cores" data-entity="LAB\\alice"'), 'per-user chart attributes');
    check(!card({ metrics: okMetrics({ gpus: [{ ...gpu, users: null, unattributed_vram_bytes: null }] }) }).includes('gpu-users'),
          'users null -> no line');
    check(!card({ metrics: okMetrics({ gpus: [{ ...gpu, users: 'x', unattributed_vram_bytes: 0 }] }) }).includes('gpu-users'),
          'non-array users -> no line');
    // Per-user history needs the newer Manager: no chart attributes when it lacks usage
    setFeatures({ usage: false });
    check(!card({ metrics: okMetrics({}), users }).includes('data-entity'), 'no per-user charts on an older Manager');
    setFeatures({});
}
{
    // Usage totals: GPU hours when any, else CPU core-hours; hidden on an older Manager
    const usage = { window: '24h', users: [
        { username: 'LAB\\alice', cpu_core_hours: 30, gpu_busy_hours: 18.2, gpu_held_hours: 23.9 },
        { username: 'bob', cpu_core_hours: 50, gpu_busy_hours: null, gpu_held_hours: 2 }] };
    const g = card({ metrics: okMetrics({}), users: [{ username: 'alice', cpu_percent: 1 }], usage });
    check(g.includes('Last 24 h</span> alice 18.2 h busy / 23.9 h held · bob — busy / 2.0 h held</div>'), 'gpu usage line: ' + g);
    check(!g.includes('bob 50'), 'GPU list shows GPU hours, not core-hours');
    // Unknown busy hours (no utilisation reading, or a shared busy GPU) never hide a GPU holder:
    // ranked after every known busy total, then by held hours
    const unknownBusy = { window: '24h', users: [
        { username: 'LAB\\alice', cpu_core_hours: 0.36, gpu_busy_hours: null, gpu_held_hours: 0.333 },
        { username: 'LAB\\bob', cpu_core_hours: 1, gpu_busy_hours: 0.015, gpu_held_hours: 0.167 },
        { username: 'carol', cpu_core_hours: 1, gpu_busy_hours: null, gpu_held_hours: 5 },
        { username: 'dave', cpu_core_hours: 9, gpu_busy_hours: null, gpu_held_hours: null }] };
    const ub = card({ metrics: okMetrics({}), users: [{ username: 'alice', cpu_percent: 1 }], usage: unknownBusy });
    check(ub.includes('Last 24 h</span> bob 0.0 h busy / 0.2 h held · carol — busy / 5.0 h held · alice — busy / 0.3 h held</div>'),
          'unknown busy hours keep the GPU holder: ' + ub);
    check(ub.includes('Busy hours count only the runs whose GPU use could be attributed'), 'partial busy hint: ' + ub);
    // A GPU host's measured zeros are not GPU use: core-hours, and no "0.0 h busy" rows
    const idleGpu = { window: '24h', users: [
        { username: 'alice', cpu_core_hours: 310.5, gpu_busy_hours: 0, gpu_held_hours: 0 },
        { username: 'bob', cpu_core_hours: 2, gpu_busy_hours: 0, gpu_held_hours: 0 }] };
    const idle = card({ metrics: okMetrics({}), users: [{ username: 'alice', cpu_percent: 1 }], usage: idleGpu });
    check(idle.includes('Last 24 h</span> alice 310.5 core-h · bob 2.0 core-h') && !idle.includes('busy'), 'idle GPU host usage line: ' + idle);
    const oneGpu = { window: '24h', users: [...idleGpu.users, { username: 'carol', cpu_core_hours: 1, gpu_busy_hours: 3, gpu_held_hours: 4 }] };
    const one = card({ metrics: okMetrics({}), users: [{ username: 'alice', cpu_percent: 1 }], usage: oneGpu });
    check(one.includes('Last 24 h</span> carol 3.0 h busy / 4.0 h held</div>'), 'only users who used a GPU: ' + one);
    const cpuOnly = { window: '24h', users: [{ username: 'a', cpu_core_hours: 1.25 }, { username: 'b', cpu_core_hours: 7 }] };
    check(card({ metrics: okMetrics({}), users: [], usage: cpuOnly }).includes('b 7.0 core-h · a 1.3 core-h'),
          'cpu usage line (and shown without current users)');
    setFeatures({ usage: false });
    check(!card({ metrics: okMetrics({}), users: [{ username: 'a' }], usage }).includes('usage-line'), 'usage hidden when unsupported');
    setFeatures({});
    check(!ctx.buildSystemCardHtml('A', { device_type: 'nas', usage, users: [{ username: 'a' }] }, STATES[0]).includes('usage-line'),
          'no user activity on NAS cards');
}
{
    // Connected: Appendix A.3, counts every run, list ageing at 15 and 60 minutes
    const summary = { _detail: 'full', _sources: { tcp: 'ok', utmp: 'ok', smb: 'not_permitted' }, _list_state: 'full',
                      clients_total: 9, users_total: 1, smb_clients: 7, nfs_clients: 1, ssh_clients: 1, ssh_users: 1,
                      dsm_clients: 2, rsync_clients: 0, sessions_truncated: false };
    const list = [{ service: 'ssh', username: 'jeff', client: '10.11.2.40', connections: 1, since: '2026-11-21T12:35:00Z', source: 'utmp' },
                  { service: 'smb', client: '10.11.2.17', connections: 2, source: 'tcp' },
                  { service: 'smb', username: 'jeff', client: '10.11.2.40', connections: 1, source: 'smb' }];
    const sess = (listAge, countsAge, extra) => ctx.buildSystemCardHtml('A', { device_type: 'nas',
        sessions: Object.assign({ timestamp: isoAgo(countsAge), list_timestamp: isoAgo(90), list_confirmed_at: isoAgo(listAge),
                                  summary, sessions: list }, extra) }, STATES[0]);
    const fresh = sess(5, 1);
    check(fresh.includes('Connected — 9 clients · 1 user'), 'sessions header: ' + fresh);
    check(fresh.indexOf('>SMB 7<') < fresh.indexOf('>DSM 2<') && fresh.includes('session-chip zero') && fresh.includes('>RSYNC 0<'),
          'chips sorted by count, zero muted');
    check(fresh.includes('data-type="sessions" data-metric="smb_clients"'), 'chip opens the count history');
    check(fresh.indexOf('title="jeff">jeff') < fresh.indexOf('title="SMB user names need root access'), 'named rows first');
    check(fresh.includes('SSH, SMB'), 'services of one user and client folded into one row: ' + fresh);
    check(!fresh.includes('session-list aged') && !fresh.includes('as of'), 'fresh list not aged');
    const aged = sess(20, 1);
    check(aged.includes('session-list aged') && aged.includes('as of '), 'list older than 15 min greyed out');
    // Narrow cells: a short date for older sessions, the full values in the tooltips
    const v6 = '2001:db8:1234:5678:9abc:def0:1234:5678';
    const older = new Date(Date.now() - 2 * 86400 * 1000);
    const wide = sess(5, 1, { sessions: [{ service: 'ssh', username: 'jeff', client: v6, client_host: 'ws1.lab',
                                           connections: 1, since: older.toISOString() }] });
    const sinceCell = (wide.match(/<span class="session-since" title="([^"]*)">([^<]*)<\/span>/) || []).slice(1);
    const sameYear = older.getFullYear() === new Date().getFullYear();
    check(sinceCell.length === 2 && sinceCell[1] === ctx.formatSince(older.getTime()) && !sinceCell[1].includes(':')
          && (!sameYear || !/\d{4}/.test(sinceCell[1])), 'older session: short date in the cell: ' + sinceCell);
    check(sinceCell[0] === older.toLocaleDateString() + ' ' + ctx.formatHm(older.getTime()), 'full start time in the tooltip: ' + sinceCell);
    check(wide.includes(`<span class="session-client" title="${v6} · ws1.lab">${v6}</span>`), 'client tooltip carries the address: ' + wide);
    const lastYear = new Date(new Date().getFullYear() - 1, 10, 20, 3, 0).getTime();
    check(/\d{4}/.test(ctx.formatSince(lastYear)) && !ctx.formatSince(lastYear).includes(':'), 'other year: month and year');
    const gone = sess(70, 1);
    check(gone.includes('List not reported since') && !gone.includes('session-row'), 'list older than 60 min hidden');
    check(gone.includes('Connected — 9 clients'), 'counts still shown when the list is hidden');
    check(sess(5, 20).includes('section-label aged'), 'counts age on the snapshot timestamp');
    const counts = sess(5, 1, { summary: Object.assign({}, summary, { _detail: 'counts' }) });
    check(counts.includes('session-chip') && !counts.includes('session-row'), 'counts detail: chips only');
    setFeatures({ sessions: false });
    check(!sess(5, 1).includes('Connected'), 'sessions hidden when unsupported');
    setFeatures({});
}
{
    // Storage health: pill states, problems, temperatures, stale
    const disks = [{ id: 'sata1', status: 'ok', temperature_c: 34, bus: 'sata' },
                   { id: 'sata2', status: 'ok', temperature_c: 41, bus: 'sata' },
                   { id: 'usb1', status: 'ok', temperature_c: 30, bus: 'usb' }, { id: 'sata4', status: 'ok', temperature_c: null }];
    const arrays = [{ id: 'md2', status: 'ok', role: 'data', sync_action: 'idle' }, { id: 'md0', status: 'ok', role: 'system' }];
    const health = (status, extra) => ({ device_type: 'nas', storage_health: Object.assign(
        { timestamp: isoAgo(10), overall_status: status, status_reasons: [], sources: { md: 'ok', smart: 'not_permitted' },
          arrays, disks, volumes: [] }, extra) });
    const ok = ctx.buildSystemCardHtml('A', health('ok'), STATES[0]);
    check(ok.includes('health-pill ok">Healthy') && ok.includes(' · 4 disks · md2 clean · 30–41 °C'), 'healthy line: ' + ok);
    check(ok.includes('SMART: not permitted for the collector account'), 'source coverage tooltip');
    check(ok.includes('data-type="storage_health" data-metric="disk_sata1_temperature_c"') && !ok.includes('disk_usb1_'),
          'temperatures open history (not USB disks)');
    check(!ok.includes('health-row'), 'no problem rows when healthy');
    const warn = ctx.buildSystemCardHtml('A', health('warning', { status_reasons: ['md2 rebuilding 37.5% (ETA 5h 12m)'],
        arrays: [{ id: 'md2', kind: 'md', level: 'raid5', state: 'active', status: 'warning', sync_action: 'recover',
                   sync_percent: 37.5, sync_eta_seconds: 18720, members_total: 4, members_active: 3 }],
        disks: [{ id: 'sata3', model: 'WD80EFZX', status: 'critical', temperature_c: 61, pending_sectors: 8 }],
        volumes: [{ id: '/volume1', status: 'ok' }] }), STATES[0]);
    check(warn.includes('health-pill warning">Warning') && warn.includes(' · md2 rebuilding 37.5% (ETA 5h 12m)'), 'warning line');
    check(warn.indexOf('health-row critical') < warn.indexOf('health-row warning'), 'critical rows first');
    check(warn.includes('Disk sata3 (WD80EFZX): critical, 61 °C, 8 pending'), 'disk problem row: ' + warn);
    check(warn.includes('md2 raid5: warning, active, recover 37.5% ETA 5h 12m, 3 of 4 members'), 'array problem row: ' + warn);
    check(warn.includes('health-temp critical'), 'hot disk coloured');
    const many = ctx.buildSystemCardHtml('A', health('critical', { disks: Array.from({ length: 8 }, (_, i) => ({ id: 'd' + i, status: 'critical' })) }), STATES[0]);
    check((many.match(/health-row/g) || []).length === 5 && many.includes('health-pill critical">Critical'), 'at most 5 problem rows');
    check(ctx.buildSystemCardHtml('A', health('bogus'), STATES[0]).includes('health-pill unknown">Unknown'), 'unknown status');
    const stale = ctx.buildSystemCardHtml('A', health('ok', { timestamp: isoAgo(4 * 60) }), STATES[0]);
    check(/class="storage-health[^"]*\baged\b/.test(stale) && stale.includes('(as of '), 'health older than 3 h is stale');
    check(!/class="storage-health[^"]*\baged\b/.test(ok), 'fresh health not stale');
    // Summary strip glyph
    const strip = st => ctx.buildSummaryButtonHtml('A', health(st), STATES[0]);
    check(strip('warning').includes('sys-btn-health warning') && strip('warning').includes('⚠'), 'strip warning glyph');
    check(strip('critical').includes('✖'), 'strip critical glyph');
    check(!strip('ok').includes('sys-btn-health'), 'no glyph when healthy');
    // Health without a disk scan still gets a Storage section
    check(ctx.buildSystemCardHtml('A', health('ok'), STATES[0]).includes('<div class="section-label">Storage</div>'), 'health-only storage section');
    setFeatures({ storage_health: false });
    check(!ctx.buildSystemCardHtml('A', health('warning'), STATES[0]).includes('health-pill'), 'health hidden when unsupported');
    setFeatures({});
}
{
    // Storage owners: cold bars (Appendix A.5 numbers), null cold -> no bar, top 5 then all
    const disk = { timestamp: isoAgo(60), volumes: [],
                   folders: [{ path: '/volume1/Lab', usage_bytes: 5000 }, { path: '/volume1/Other', usage_bytes: 10 },
                             { path: '/volume1/Win', usage_bytes: 7 }] };
    const owners = ['jeff', 'a', 'b', 'c', 'd', 'e', 'f'].map((o, i) => ({ owner: o, bytes: 4000 - i * 100, cold_1y_bytes: 2500 }));
    const breakdown = { timestamp: isoAgo(60), owner_source: 'posix_uid', cold_thresholds_days: [365, 1095],
        folders: [{ path: '/volume1/Lab', folder_owner: 'jeff', bytes: 5000, files: 120, cold_1y_bytes: 3000, cold_3y_bytes: 1000,
                    top_owner: 'jeff', top_owner_bytes: 4000 },
                  { path: '/volume1/Other', bytes: 10, cold_1y_bytes: null, cold_3y_bytes: null }],
        device: { bytes: 5000, files: 120, cold_1y_bytes: 3000, cold_3y_bytes: 1000, owners,
                  other: { owner_count: 3, bytes: 1000 } } };
    const c = ctx.buildSystemCardHtml('Tri', { device_type: 'nas', disk, breakdown }, STATES[0]);
    check((c.match(/class="cold-bar"/g) || []).length === 1, 'cold bar only where cold data was measured: ' + c);
    check(c.includes('<div class="cold-3y" style="width:20%"></div><div class="cold-1y" style="width:40%"></div>'), 'cold bar widths');
    check(c.includes('Not modified ≥1y: 2.93 KiB (60%) · ≥3y 1000 B · top owner jeff (80%) · folder owner jeff · click for owners'),
          'cold tooltip: ' + c);
    check(c.includes('data-folder="/volume1/Lab"'), 'cold bar opens the folder owner list');
    check(c.includes('Top storage users (scanned folders)') && (c.match(/class="owner-row"/g) || []).length === 5
          && c.includes('>+2 more<') && !c.includes('3 other owners'), 'top 5 owners: ' + c);
    check(c.includes('data-type="storage_breakdown" data-metric="owner/jeff/bytes"'), 'owner opens its history');
    check(c.includes('3.91 KiB (80%)') && c.includes('63% ≥1y'), 'owner size and cold share');
    vm.runInContext("expandedOwners.add('Tri');", ctx);
    const open = ctx.buildSystemCardHtml('Tri', { device_type: 'nas', disk, breakdown }, STATES[0]);
    check((open.match(/class="owner-row"/g) || []).length === 7 && open.includes('Show fewer') && open.includes('3 other owners'),
          'expanded owner list');
    vm.runInContext("expandedOwners.clear(); folderOwners.set('Tri\\0/volume1/Lab', { state: 'ok', total: 5000, " +
                    "owners: [{ owner: 'LAB\\\\jeff', bytes: 4000, cold_1y_bytes: 2500 }], other: { owner_count: 3, bytes: 1000 } });", ctx);
    const fo = ctx.buildSystemCardHtml('Tri', { device_type: 'nas', disk, breakdown }, STATES[0]);
    check(fo.includes('class="folder-owners"') && fo.includes('title="LAB\\jeff">jeff') && fo.includes('3 other owners'), 'folder owner list: ' + fo);
    vm.runInContext("folderOwners.clear();", ctx);
    // Windows in v1: cold data without owners - a bar, but nothing to click and no owner list
    const win = Object.assign({}, breakdown, { owner_source: 'none', device: Object.assign({}, breakdown.device, { owners: null }) });
    const wc = ctx.buildSystemCardHtml('W', { device_type: 'server', disk, breakdown: win }, STATES[0]);
    check(wc.includes('cold-bar') && !wc.includes('data-folder') && !wc.includes('Top storage users'), 'no owners on Windows');
    check(ctx.folderOwnersFrom({ folders: [{ path: '/x', bytes: 5, owners: [{ owner: 'a', bytes: 1 }, { owner: 'b', bytes: 3 }, 'junk'] }] }, '/x')
             .owners.map(o => o.owner).join() === 'b,a', 'folder owners from the proxy answer, largest first');
}
{
    // Chart labels for items without a .metric-label
    check(ctx.chartLabelFor('owner/LAB\\jeff/bytes', 'storage_breakdown', null) === 'jeff storage (scanned folders)', 'owner label');
    check(ctx.chartLabelFor('disk_sata1_temperature_c', 'storage_health', null) === 'Disk sata1 temperature', 'disk temp label');
    check(ctx.chartLabelFor('smb_clients', 'sessions', null) === 'SMB clients', 'sessions label');
    check(ctx.chartLabelFor('cpu_cores', 'user_metrics', 'LAB\\alice') === 'alice — CPU cores', 'user label');
    check(ctx.chartLabelFor('gpu_0_percent', null, null) === null, 'GPU metrics keep their card label');
}

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
    // Optional sections carry over like the others; a failed call keeps the last feature answer
    const good = goodPoll();
    good.features = { sessions: true, storage_health: true, breakdown: false, usage: true };
    good.systems.Triton.sessions = { timestamp: freshTs, list_confirmed_at: freshTs, summary: { clients_total: 3 }, sessions: [] };
    good.systems.Triton.storage_health = { timestamp: freshTs, overall_status: 'ok', disks: [], arrays: [], volumes: [] };
    const bad = goodPoll();
    bad.degraded = true;
    bad.errors = [{ endpoint: '/api/sessions/all', error: 'timeout' }, { endpoint: '/api/storage/health/all', error: 'HTTP 500' }];
    bad.features = { sessions: null, storage_health: null, breakdown: false, usage: true };
    const merged = ctx.mergeLastGood(bad, good);
    const tri = merged.systems.Triton;
    check(tri.sessions && tri.sessions.summary.clients_total === 3 && tri.storage_health, 'sessions and health carried over');
    check(JSON.stringify(tri.staleSections) === JSON.stringify(['sessions', 'storage_health']), 'staleSections: ' + JSON.stringify(tri.staleSections));
    check(merged.features.sessions === true && merged.features.breakdown === false, 'features carried over: ' + JSON.stringify(merged.features));
    const triCard = ctx.buildSystemCardHtml('Triton', tri, ctx.systemState(tri));
    check(triCard.includes('Showing connections, storage health from an earlier refresh'), 'stale notice: ' + triCard);
    // Usage totals are server-only: never listed on a NAS card
    const nas = { device_type: 'nas', unavailable: ['usage', 'sessions'], metrics: { timestamp: freshTs, units: {}, gpus: [] } };
    const nasCard = ctx.buildSystemCardHtml('N', nas, ctx.systemState(nas));
    check(nasCard.includes('Unavailable right now: connections') && !nasCard.includes('usage totals'), 'NAS notice: ' + nasCard);
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
// Rollup points: min/max/count columns, and the level in the file name
vm.runInContext(`chart.data = [{ timestamp: '2026-01-01T00:00:00Z', value: 1.5, min: 1, max: 2, count: 12 },
                               { timestamp: '2026-01-01T01:00:00Z', value: null, min: null, max: null, count: 0 }];
                 chart.meta = { downsampled: true, truncated: false, band: true, resolution: 'hour' };
                 chart.range = '2160'; chart.entity = 'LAB/alice'; chart.field = 'cpu_cores';`, ctx);
check(ctx.chartCsvText() === 'timestamp,value_avg,value_min,value_max,count\n"2026-01-01T00:00:00Z",1.5,1,2,12\n"2026-01-01T01:00:00Z",,,,0\n',
      'band CSV: ' + ctx.chartCsvText());
check(/^Triton_LAB_alice_cpu_cores_2160h_hourly_avg_\d{8}T\d{6}Z\.csv$/.test(ctx.csvFilename()), 'hourly CSV name: ' + ctx.csvFilename());
vm.runInContext(`chart.meta.resolution = 'day'; chart.entity = null; chart.field = 'cpu_percent';`, ctx);
check(/^Triton_cpu_percent_2160h_daily_avg_/.test(ctx.csvFilename()), 'daily CSV name: ' + ctx.csvFilename());
vm.runInContext(`chart.meta = null; chart.range = '24';`, ctx);

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
{
    // Min-max band behind the average when the points are aggregates
    let traces = null;
    ctx.Plotly = { Icons: {}, newPlot(id, t) { traces = t; }, purge() {} };
    const info = ctx.resolveMetricDisplay('cpu_percent', 'CPU', '%');
    const agg  = n => Array.from({ length: n }, (_, i) => ({ timestamp: `t${i}`, value: 5, min: 1, max: 9, count: 12 }));
    ctx.renderMetricChart('cpu_percent', 'CPU', info, agg(3));
    check(traces.length === 3 && traces[0].line.width === 0 && traces[1].line.width === 0, 'band: two zero-width traces first');
    check(traces[1].fill === 'tonexty' && traces[1].fillcolor === '#e74c3c22' && traces[0].fill === undefined, 'min trace fills up to max');
    check(JSON.stringify(traces[0].y) === '[9,9,9]' && JSON.stringify(traces[1].y) === '[1,1,1]', 'max then min');
    check(traces[2].fill === undefined && traces[2].mode === 'lines+markers', 'average line: no tozeroy fill');
    check(traces[2].hovertemplate.includes('(%{customdata[0]:.1f}–%{customdata[1]:.1f}%)'), 'hover avg (min–max): ' + traces[2].hovertemplate);
    ctx.renderMetricChart('cpu_percent', 'CPU', info, agg(501));
    check(traces[2].mode === 'lines', 'no markers above 500 points');
    ctx.renderMetricChart('cpu_percent', 'CPU', info, [{ timestamp: 't', value: 5 }]);
    check(traces.length === 1 && traces[0].fill === 'tozeroy' && traces[0].mode === 'lines+markers', 'plain points chart as before');
    // A point with no value splits the band (tonexty would shade a wedge across
    // the gap); a point with a value but no min/max narrows it to the average
    const gap = agg(6);
    gap[3] = { timestamp: 't3', value: null, min: null, max: null, count: 0 };
    ctx.renderMetricChart('cpu_percent', 'CPU', info, gap);
    const bands = traces.slice(0, -1);
    check(bands.length === 4 && bands.every(t => t.y.every(v => v !== null) && t.y.length === t.x.length),
          'one band pair per unbroken run: ' + JSON.stringify(bands.map(t => t.x)));
    check(JSON.stringify(bands.map(t => t.x)) === JSON.stringify([['t0', 't1', 't2'], ['t0', 't1', 't2'], ['t4', 't5'], ['t4', 't5']])
          && bands[1].fill === 'tonexty' && bands[3].fill === 'tonexty' && bands[2].fill === undefined, 'max then min in each run');
    const avgLine = traces[traces.length - 1];
    check(avgLine.y[3] === null && avgLine.y.length === 6, 'average line keeps its gap');
    const mixed = agg(4);
    mixed[2] = { timestamp: 't2', value: 7 };
    ctx.renderMetricChart('cpu_percent', 'CPU', info, mixed);
    check(traces.length === 3 && JSON.stringify(traces[0].y) === '[9,9,7,9]' && JSON.stringify(traces[1].y) === '[1,1,7,1]',
          'point without min/max narrows the band to its value');
    ctx.renderMetricChart('cpu_percent', '<b>x</b>', info, agg(2));
    check(traces.every(t => !t.name.includes('<b>')), 'trace names go through plotlyText');
    // Units added for the new fields
    check(ctx.resolveMetricDisplay('disk_read_bytes_per_s', 'Disk read', 'B/s').unit === ' MB/s'
          && ctx.resolveMetricDisplay('disk_read_bytes_per_s', 'Disk read', 'B/s').scale === 1e-6, 'B/s -> MB/s');
    check(ctx.resolveMetricDisplay('smb_clients', 'SMB clients', 'count').yLabel === 'SMB clients', 'count has no unit label');
    check(ctx.resolveMetricDisplay('disk_await_ms', 'Disk latency', null).unit === ' ms', 'fallback units for new fields');
    // Info line
    const el = ctx.document.getElementById('chartInfo');
    ctx.updateChartInfo(agg(1752).map(p => Object.assign(p, { count: 60 })),
                        { resolution: 'hour', bucket_seconds: 18000, downsampled: true });
    check(/^Hourly averages in 5-hour buckets · 1.752 points from 105.120 measurements · shaded = min–max$/.test(el.textContent),
          'hourly info: ' + el.textContent);
    ctx.updateChartInfo(agg(3), { resolution: 'day', bucket_seconds: 86400, downsampled: true });
    check(el.textContent.startsWith('Daily averages · 3 points from 36 measurements'), 'daily info: ' + el.textContent);
    ctx.updateChartInfo([{ timestamp: 't', value: 1 }], { resolution: 'raw', bucket_seconds: 900, downsampled: true, downsampled_from: 5000 });
    check(/^Averages in 15-minute buckets · 1 point from 5.000 measurements$/.test(el.textContent), 'raw downsampled info: ' + el.textContent);
    ctx.updateChartInfo([{ timestamp: '2026-01-01T00:00:00Z', value: 1 }, { timestamp: '2026-01-01T02:00:00Z', value: 1 }],
                        { downsampled: false, truncated: false });
    check(el.textContent === 'Showing 2 points spanning 2.0 hours', 'older Manager info unchanged: ' + el.textContent);
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

    // Clicking a cold-data bar loads that folder's owners through the dashboard proxy
    const requested = [];
    ctx.fetch = async url => {
        requested.push(url);
        return { ok: true, status: 200, json: async () => ({ folders: [
            { path: '/volume1/Lab', bytes: 5000, owners: [{ owner: 'jeff', bytes: 4000 }, { owner: 'ann', bytes: 900 }],
              other: { owner_count: 3, bytes: 100 } }] }) };
    };
    ctx.toggleFolderOwners('Tri', '/volume1/Lab');
    check(vm.runInContext("folderOwners.get('Tri\\0/volume1/Lab').state", ctx) === 'loading', 'owners loading');
    await new Promise(r => setTimeout(r, 10));
    check(requested[0] === '/api/storage/breakdown/Tri?folder=%2Fvolume1%2FLab', 'owner request: ' + requested[0]);
    check(vm.runInContext("folderOwners.get('Tri\\0/volume1/Lab').owners.map(o => o.owner).join()", ctx) === 'jeff,ann',
          'owners loaded');
    ctx.toggleFolderOwners('Tri', '/volume1/Lab');
    check(vm.runInContext("folderOwners.size", ctx) === 0, 'second click closes the list');
    ctx.fetch = async () => ({ ok: false, status: 404, json: async () => ({ success: false, error: 'Not found' }) });
    ctx.toggleFolderOwners('Tri', '/x');
    await new Promise(r => setTimeout(r, 10));
    check(ctx.buildFolderOwnersHtml('Tri', '/x') === '<div class="folder-owners">Owners unavailable: Not found</div>',
          'owner error: ' + ctx.buildFolderOwnersHtml('Tri', '/x'));
}

// --- Chips + one detail: the lab-wide tiles and which card is rendered ---
{
    const now = new Date().toISOString();
    const systems = {
        A: { device_type: 'server', metrics: { timestamp: now }, sessions: { summary: { clients_total: 3 } },
             disk: { volumes: [{ usage_bytes: 60, total_bytes: 100, free_bytes: 40 }] } },
        B: { device_type: 'nas', metrics: null, sessions: { summary: { clients_total: 2 } },
             disk: { volumes: [{ usage_bytes: 10, total_bytes: 50 }, { usage_bytes: 5 }] } },   // free from total - used; no capacity: skipped
        C: { device_type: 'nas', metrics: { timestamp: now }, disk: null },                       // no sessions, no disk
    };
    ctx.updateSnapshotTiles(systems);
    check(els.systemsReporting.textContent === '2 of 3', 'systems reporting: ' + els.systemsReporting.textContent);
    check(els.connectedNow.textContent === '5 clients', 'connected now: ' + els.connectedNow.textContent);
    check(els.freeSpace.textContent === '80 B', 'free space: ' + els.freeSpace.textContent);
    ctx.updateSnapshotTiles({ A: { device_type: 'nas', metrics: { timestamp: now }, sessions: { summary: { clients_total: 1 } } } });
    check(els.connectedNow.textContent === '1 client' && els.freeSpace.textContent === '—', 'one client, no volumes');
    ctx.updateSnapshotTiles({});
    check(els.systemsReporting.textContent === '—' && els.connectedNow.textContent === '—', 'no systems: dashes');

    // Only the selected system's card is rendered; the hint shows when none is
    ctx.document.createTextNode = text => text;
    const rendered = [];
    vm.runInContext('var __realRenderCards = renderCards; var __realStrip = renderSummaryStrip;', ctx);
    ctx.__capture = entries => rendered.push(entries.map(e => e[0]));
    vm.runInContext('renderCards = e => __capture(e); renderSummaryStrip = () => {};', ctx);
    const data = { systems, global_totals: {}, errors: [] };
    vm.runInContext('selectedSystem = null;', ctx);
    ctx.updateDashboard(data);
    check(JSON.stringify(rendered.pop()) === '[]' && els.detailHint.hidden === false, 'no selection: no card, hint shown');
    vm.runInContext('selectedSystem = "B";', ctx);
    ctx.updateDashboard(data);
    check(JSON.stringify(rendered.pop()) === '["B"]' && els.detailHint.hidden === true, 'selected system only, hint hidden');
    vm.runInContext('selectedSystem = "Gone";', ctx);
    ctx.updateDashboard(data);
    check(JSON.stringify(rendered.pop()) === '[]' && vm.runInContext('selectedSystem', ctx) === null,
          'a system that no longer exists is deselected');
    vm.runInContext('renderCards = __realRenderCards; renderSummaryStrip = __realStrip; selectedSystem = null;', ctx);
    check(ctx.selectionFromAddress() === null, 'no window: no selection from the address');
}

asyncChecks().catch(err => failures.push('async checks threw: ' + (err && err.stack || err))).then(() => {
    if (failures.length) {
        for (const f of failures) console.log('FAIL: ' + f);
        process.exit(1);
    }
    console.log('OK');
});
