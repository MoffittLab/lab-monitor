# Lab Monitor - Development Notebook

## Current Project State

**Lab Monitor** is a distributed system for tracking disk usage across NAS systems:
- **Collector** runs daily on each Synology NAS, measures folder sizes
- **Manager** (Windows Server) receives reports via HTTP POST, stores in append-only JSONL files
- **Dashboard** web UI polls Manager every 30s, displays real-time usage trends

### Deployment Status
- Three-service architecture (Collector, Manager, Dashboard)
- Synology and Windows support documented
- Virtual environment setup recommended for both platforms
- Task Scheduler automation in place

---

## Active Work: Local Record Retention

### Goal
Modify Collector to **retain local records** instead of deleting them after successful upload to Manager.

### Current Behavior
- Collector measures folders → enqueues to `queue.jsonl` (local)
- Posts to Manager API
- **On success: deletes queue.jsonl**
- On failure: queue persists, retried next run

### New Behavior
- Collector measures folders → enqueues locally
- Posts to Manager API
- **Regardless of success: keep local copy**
- Additional archive/retention policy TBD

### Files to Modify

**Collector Component:**
- `collector/collector.py` - Main script that currently deletes queue
- `collector/config.*.example.json` - May need new config options

**Configuration Changes (Optional):**
- New config flag: `keep_queue_after_sync` (default: true after change)
- Or: New config flag: `local_archive_path` for separate archival location
- Or: Hybrid: Keep queue, also archive to separate dir for long-term storage

## Implementation Plan (Selected: Option 2 with Monthly Rollup)

### Behavior
1. Collector measures folders daily → writes to `queue.jsonl`
2. POSTs to Manager
3. **On success:**
   - Append queue contents to `archive/YYYY-MM.jsonl` (monthly file)
   - Clear `queue.jsonl` for next run
4. **On failure:**
   - Queue persists, retried next run (existing behavior)
5. Monthly archives grow indefinitely (or with optional retention policy TBD)

### Directory Structure
```
/volume1/lab-monitor/data/
├── queue.jsonl              # Daily working queue (cleared after success)
├── archive/
│   ├── 2026-07.jsonl       # July 2026 accumulation
│   ├── 2026-08.jsonl       # August 2026 accumulation
│   └── 2026-09.jsonl       # September 2026 accumulation
└── lab-monitor-collector.log
```

### Code Changes

**`collector/collector.py`**
- After successful POST to Manager:
  - Instead of `os.remove(queue_path)`
  - Call new function: `archive_queue(queue_path, archive_dir)`
  - Function reads queue.jsonl, appends to `archive/YYYY-MM.jsonl`, then clears queue
- Archive function handles:
  - Creating `archive/` directory if not exists
  - Determining current month (YYYY-MM format)
  - Appending queue lines to monthly file
  - Preserving queue.jsonl structure (JSONL = one JSON per line)

**`collector/config.*.example.json`**
- New optional field: `archive_dir` (default: relative to queue_path parent)
- Example: `"archive_dir": "./archive"`

**Documentation**
- Update `collector/README.md` with new archive behavior
- Note: Monthly files are append-only, records never deleted (or with optional retention)

### Next Steps
1. Implement `archive_queue()` function in collector.py
2. Update POST success handler to call archive instead of delete
3. Add `archive_dir` config option
4. Test on dev machine (create fake data, run through cycle)
5. Update README with new behavior and monthly archive structure
6. Test on actual NAS/Windows to verify permissions and folder creation

