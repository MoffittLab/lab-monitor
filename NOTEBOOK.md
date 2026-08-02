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

## Implementation Complete ✓ (Latest: v0.0.14)

### Key Decisions Made (Final)

**Permissions Approach:** Synology-native ACL (not sudo modifications)
- GUI-based folder permissions survive Synology updates
- No risky `/etc/sudoers` modifications
- Follows Synology official recommendations
- Admin user (not root) runs Task Scheduler
- Least privilege security model

**Monitoring:** Non-Root User Friendly
- SSH + tail logs for post-run verification
- Optional email notifications for failures
- Archive files accumulate monthly for audit trail
- Queue status shows sync success/failure
- Daily routine takes ~2 minutes

### What Was Implemented

**Behavior:**
1. Collector measures folders daily → writes to `queue.jsonl`
2. POSTs to Manager
3. **On success:**
   - Append queue contents to `archive/YYYY-MM.jsonl` (monthly file)
   - Clear `queue.jsonl` for next run
4. **On failure:**
   - Queue persists, retried next run (existing behavior)
5. Monthly archives accumulate indefinitely

### Code Changes Made

**`collector/lib/queue.py`**
- Added `archive_queue(queue_path, archive_dir)` function
  - Reads queue entries
  - Appends to monthly archive file `{archive_dir}/YYYY-MM.jsonl`
  - Truncates queue for next run
  - Creates archive directory automatically

**`collector/collector.py`**
- Updated POST success handler:
  - Changed from `clear_queue()` to `archive_queue(queue_path, archive_dir)`
  - Added `archive_dir` config loading

**Config Files**
- `config.example.json`: Added `archive_dir: null`
- `config.synology.example.json`: Added `archive_dir: "/volume1/lab-monitor/data/archive"`
- `config.windows.example.json`: Added `archive_dir: "C:\\lab-monitor\\data\\archive"`

**Documentation**
- Updated `collector/README.md`:
  - Overview now mentions "Archives queue to monthly files"
  - Added `archive_dir` to Configuration Reference table
  - New "Archive Behavior" section with directory structure, file format, and customization
  - Updated Notes section with data retention info

### Testing
- ✓ Manual test: Created queue with 2 reports → archived to YYYY-MM.jsonl → queue cleared
- ✓ Archive file verified: Correct monthly naming, valid JSONL format

### Commit & Tag
- **Commit**: `9a29ef2` - "feat: implement local record archival with monthly rollup"
- **Tag**: `v0.0.2` - Local record archival with monthly rollup

