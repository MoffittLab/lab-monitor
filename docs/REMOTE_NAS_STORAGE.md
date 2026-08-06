# Remote NAS Storage Configuration

## Overview

You can configure lab-monitor to store its data on a remote NAS (e.g., `\\triton1.hms.harvard.edu\lab-monitor-data`) instead of the local Atlantis drive. This enables centralized backup and snapshot management.

---

## Key Insight: Independent Data Directories

**Manager and Collector have independent `data_dir` paths.**

- **Collector's `data_dir`**: Used only for local archiving (`YYYY-MM.jsonl`) and staging (`queue.json`)
- **Manager's `data_dir`**: Used for central databases (`metrics.db`, per-system `data.db`)

Data flow:
```
Collector → Writes archive + queue to its data_dir
         ↓
         → POSTs queue to Manager (HTTP)
         ↓
Manager → Receives POST → Stores to its own data_dir
```

They're **independent**. The Collector doesn't read from the Manager's storage, and the Manager doesn't read from the Collector's local queue.

---

## Configuration Options

### Option A: Both Point to Remote NAS (Everything Backed Up)

**Pros:**
- ✅ All data (archives + databases) on NAS, snapshotted daily
- ✅ Complete centralized backup
- ✅ Simplest mental model (one remote path)

**Cons:**
- ⚠️ Network I/O for everything (queue writes, database operations)
- ⚠️ SQLite databases over SMB can be slower (though WAL mode mitigates this)
- ⚠️ Any network hiccup affects both collector and manager

**Configuration:**

Manager (`E:\Users\lab-monitor\scripts\lab-monitor\manager\local\config.json`):
```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data",
  "auth_tokens": ["YOUR-SECURE-TOKEN"],
  ...
}
```

Collector on atlantis (`E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json`):
```json
{
  "name": "atlantis",
  "id": "windows-atlantis",
  "manager_url": "http://localhost:5000",
  "manager_token": "YOUR-SECURE-TOKEN",
  "data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data",
  ...
}
```

Remote path structure:
```
\\triton1.hms.harvard.edu\lab-monitor-data\
├── metrics.db                   ← Manager: central summary
├── atlantis\
│   ├── data.db                  ← Manager: full history
│   ├── 2026-08.jsonl            ← Collector: archive
│   └── queue.json               ← Collector: staging
├── triton\
│   ├── data.db
│   ├── 2026-08.jsonl
│   └── queue.json
└── other-system\
    └── ...
```

---

### Option B: Collector Local, Manager Remote (RECOMMENDED)

**Pros:**
- ✅ Manager history (the most important data) backed up on NAS
- ✅ Collector queue stays local and fast (resilient to network hiccups)
- ✅ Best performance — only critical path is remote
- ✅ Collector archives stay local (they're small, append-only)
- ✅ If NAS goes down, Collector can still queue locally

**Cons:**
- ⚠️ Collector archives not automatically backed up (but they're just append-only logs; rarely needed)

**Configuration:**

Manager (`E:\Users\lab-monitor\scripts\lab-monitor\manager\local\config.json`):
```json
{
  "host": "0.0.0.0",
  "port": 5000,
  "data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data",
  "auth_tokens": ["YOUR-SECURE-TOKEN"],
  ...
}
```

Collector on atlantis (`E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json`):
```json
{
  "name": "atlantis",
  "id": "windows-atlantis",
  "manager_url": "http://localhost:5000",
  "manager_token": "YOUR-SECURE-TOKEN",
  "data_dir": "E:\\Users\\lab-monitor\\data",
  ...
}
```

Local and remote structure:

**Local (E:\Users\lab-monitor\data):**
```
E:\Users\lab-monitor\data\
└── atlantis\
    ├── 2026-08.jsonl            ← Collector: archive (local)
    └── queue.json               ← Collector: staging (local)
```

**Remote (\\triton1.hms.harvard.edu\lab-monitor-data):**
```
\\triton1.hms.harvard.edu\lab-monitor-data\
├── metrics.db                   ← Manager: central summary (backed up)
├── atlantis\
│   └── data.db                  ← Manager: full history (backed up)
├── triton\
│   └── data.db                  ← Manager: full history (backed up)
└── other-system\
    └── data.db
```

---

## Which Option?

| Scenario | Recommendation |
|----------|---|
| **You want everything backed up + don't mind network I/O** | Option A |
| **You want performance + Manager data backed up + collector archives are "nice to have"** | Option B (Recommended) |
| **You have very fast network (iSCSI, Fiber, etc.)** | Option A is acceptable |
| **Network can be flaky** | Option B (local queue survives brief outages) |

**My recommendation: Option B** — The Manager databases are what matter (they're the authoritative history). The Collector archives are just a local safety net; in practice, you'll rely on the Manager's databases for any data recovery.

---

## Network Path Format

Windows supports two formats for network paths:

### UNC Path (Recommended)
```json
"data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data"
```
- `\\\\` at the start (escaped backslashes in JSON)
- Hostname or IP after the double backslash
- Share name
- Optional subfolder

### Mapped Drive Letter
```json
"data_dir": "Z:\\lab-monitor-data"
```
Requires the network share to be pre-mapped:
```powershell
net use Z: \\triton1.hms.harvard.edu\lab-monitor-data /persistent:yes
```

**I recommend UNC paths** — they don't depend on a mapped drive and work immediately without setup.

---

## SMB/Network Considerations

### SQLite WAL Mode (Already Enabled)

Both components use SQLite WAL mode for concurrent access:
```python
db.execute('PRAGMA journal_mode=WAL')
```

This works reliably over Windows SMB shares, even with concurrent readers/writers. However:

- **First access**: Slightly slower than local (network latency)
- **Sustained load**: Similar performance to local with WAL mode enabled
- **Network hiccups**: May briefly stall operations (depends on timeout settings)

### Performance Expectations

**Option A (Both Remote):**
- Collector queue writes: ~10-50 ms (vs 1 ms local)
- Manager database inserts: ~5-20 ms (vs 1-2 ms local)
- Overall impact: Negligible for 5-minute collection intervals

**Option B (Collector Local, Manager Remote):**
- Collector: No change (local)
- Manager: Same as Option A's Manager timing
- Overall impact: Minimal (Manager isn't time-critical)

---

## Backup & Snapshot Strategy

If you're using Triton's snapshot feature (e.g., hourly snapshots):

**With Option A (Both Remote):**
- Snapshots capture everything — archives, queues, databases
- Full point-in-time recovery available
- **Disadvantage:** Queues are temporary; snapshots include incomplete data

**With Option B (Manager Remote):**
- Snapshots capture just `metrics.db` and per-system `data.db`
- This is the data that matters
- Collectors have local backups anyway (archives)
- **Advantage:** Smaller snapshots, cleaner recovery

---

## Setup Steps

### 1. Create NAS Share (if not already done)

On Triton NAS:
```
Create share: lab-monitor-data
Permissions: Read/Write for atlantis user
```

### 2. Update Manager Config

Edit `E:\Users\lab-monitor\scripts\lab-monitor\manager\local\config.json`:
```json
{
  "data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data",
  ...
}
```

### 3. Update Collector Config (if Option A)

Edit `E:\Users\lab-monitor\scripts\lab-monitor\collector\local\config.json`:
```json
{
  "data_dir": "\\\\triton1.hms.harvard.edu\\lab-monitor-data",
  ...
}
```

(Or skip this step if using Option B — keep Collector local.)

### 4. Restart Services

```powershell
Get-ScheduledTask -TaskName "Lab Monitor*" | Stop-ScheduledTask
Start-Sleep -Seconds 2
Get-ScheduledTask -TaskName "Lab Monitor*" | Start-ScheduledTask
```

### 5. Verify

Check Manager logs:
```powershell
Get-Content E:\Users\lab-monitor\logs\manager.log -Tail 20
# Should show: "Central metrics DB: \\triton1.hms.harvard.edu\lab-monitor-data\metrics.db"
```

Run a collection manually:
```powershell
cd E:\Users\lab-monitor\scripts\lab-monitor\collector
python collector.py --config local\config.json --mode metrics --verbose
# Should complete without errors
```

---

## Troubleshooting

### "Permission denied" or "Access is denied"

**Cause:** Atlantis user doesn't have write permission on the share  
**Fix:** Add write permission for the user running the services on Triton

### "Cannot create file" or "Network path not found"

**Cause:** UNC path typo or share doesn't exist  
**Fix:** Test from PowerShell:
```powershell
Test-Path "\\triton1.hms.harvard.edu\lab-monitor-data"
# Should return True
```

### Slow collection or "timeout" errors

**Cause:** Network latency or SMB congestion  
**Fix:** 
- Increase `request_timeout_seconds` in Collector config (default: 30)
- Check network connectivity: `ping triton1.hms.harvard.edu`
- Monitor SMB share performance: `Performance Monitor` → Network Interface

### "Database is locked"

**Cause:** Multiple Manager instances trying to access same database  
**Fix:** Ensure only one Manager process is running:
```powershell
tasklist | findstr python
# Should show only one manager.py process
```

---

## Migration from Local to Remote

If you've been running with local storage and want to switch to remote:

### Option 1: Clean Start
1. Stop Manager and Collector
2. Update configs to point to remote NAS
3. Start services
4. Data will accumulate fresh on remote (old local archives remain local)

### Option 2: Migrate History
1. Stop services
2. Copy `E:\Users\lab-monitor\data\*` to `\\triton1.hms.harvard.edu\lab-monitor-data\*`
3. Update configs to point to remote NAS
4. Start services
5. Verify in Manager logs and Dashboard

---

## See Also

- [ATLANTIS_DUAL_ROLE.md](ATLANTIS_DUAL_ROLE.md) — Dual Manager + Collector setup
- [ARCHITECTURE.md](ARCHITECTURE.md) — System design
- [CONFIG.md](../CONFIG.md) — Configuration security
