# Security & Code Quality Fixes (v0.0.14+)

## Summary

Comprehensive review and fixes applied to Manager and Dashboard before production deployment to Windows Server.

## Critical Issues Fixed

### 1. API Format Mismatch ✅
**Issue:** Manager expected `{"reports": [...]}` array, Collector sends single report object.

**Fix:** Manager now accepts BOTH formats:
- Single report: `{"nas_name": "triton-01", ...}`
- Array of reports: `{"reports": [{...}, {...}]}`

**File:** `manager/manager.py` - `ingest_reports()` endpoint

---

### 2. Unauthenticated Query Endpoints ✅
**Issue:** GET endpoints (`/api/usage/all`, `/api/usage/history/<nas_name>`, `/api/usage/nas/<nas_name>`) had NO authentication.

**Fix:** All GET endpoints now require Bearer token authentication.
- Added `@require_auth` decorator
- Authentication check happens before data access
- Same token mechanism as POST endpoint

**File:** `manager/manager.py` - All GET route handlers

---

### 3. CORS Misconfiguration ✅
**Issue:** `CORS(app)` without parameters allowed requests from ANY origin.

**Fix:** CORS now whitelists specific origins from config:
```json
"cors_origins": ["http://localhost:5001", "http://dashboard.internal:5001"]
```

**File:** `manager/manager.py` - `init_app()` function

---

### 4. Data Validation Missing ✅
**Issue:** No validation of report structure, NAS names, folder paths, or usage values.

**Risks:**
- Path traversal attacks (`../../etc/passwd`)
- Invalid usage values (negative numbers)
- Malformed timestamps
- Excessive data sizes

**Fix:** Added comprehensive validation functions:
- `validate_nas_name()` - Alphanumeric + hyphen/underscore, max 100 chars
- `validate_path()` - No `..` traversal, reasonable length
- `validate_report()` - Full structure validation with detailed error messages

**File:** `manager/manager.py` - New validation functions

---

### 5. Retention Days Not Implemented ✅
**Issue:** Config had `retention_days` but code didn't use it. Data accumulated forever.

**Fix:** Implemented `cleanup_old_reports()` function that:
- Reads all reports from JSONL file
- Filters out records older than `retention_days`
- Rewrites file with kept records
- Logs cleanup statistics

Called automatically after each successful POST if `retention_days` is configured.

**File:** `manager/manager.py` - New `cleanup_old_reports()` function

---

### 6. Timestamp Parsing Crashes ✅
**Issue:** `parse_iso_timestamp()` crashed on malformed timestamps instead of handling gracefully.

**Fix:** Added try/except with fallback to current timestamp.

**File:** `manager/manager.py` - `parse_iso_timestamp()` function

---

### 7. Dashboard Hardcoded Usage Bar ✅
**Issue:** Usage bar always showed `width: 45%` regardless of actual data.

**Fix:** 
- Calculate percentage based on estimated capacity (100TB default)
- Can be customized in `static/js/dashboard.js`
- Proper fallback if calculation fails

**File:** `dashboard/static/js/dashboard.js` - `createNasCard()` function

---

### 8. Missing XSS Protection ✅
**Issue:** Dashboard displayed user data directly in HTML without escaping.

**Risk:** If NAS name or folder path contained `<script>` tags, could execute arbitrary JS.

**Fix:** Added `escapeHtml()` function to sanitize all user-displayed data.

**File:** `dashboard/static/js/dashboard.js` - New `escapeHtml()` function

---

## Important Improvements

### Error Handling
- Dashboard now shows "Unable to reach Manager" instead of raw error messages
- All API calls wrapped in try/catch with logging
- Invalid timestamps/data logged with context instead of crashing

### Input Validation
- Dashboard validates `days` parameter (1-365 range)
- Folder size validation (no negative numbers)
- NAS name format validation (no special characters)

### Logging Improvements
- Per-report logging changed from INFO to DEBUG (reduces log spam)
- Validation failures logged with descriptive messages
- Cleanup operations logged for audit trail

### Windows Compatibility
- Manager README includes Windows Server setup (Python install, Services, Firewall)
- Dashboard README includes Windows Server setup
- NSSM (service manager) instructions included
- Path examples use Windows conventions (`C:\lab-monitor`)

---

## Configuration Changes

### Manager Config
**New fields:**
```json
{
  "cors_origins": ["http://localhost:5001"],
  "retention_days": 90,
  "debug": false
}
```

### Dashboard Config
**New fields:**
```json
{
  "manager_token": "change-me-token",
  "manager_timeout_seconds": 5,
  "debug": false
}
```

---

## Testing Recommendations

### Before Deployment
- [ ] Test Manager with single report format (Collector sends)
- [ ] Test Manager with array format (backward compatibility)
- [ ] Test authentication: Valid token should succeed, invalid token should fail
- [ ] Test CORS: Requests from whitelisted origins should work
- [ ] Test Dashboard authentication: Check Manager logs for token validation
- [ ] Test with multiple NAS systems (>1)
- [ ] Test retention cleanup: Verify old reports are deleted after N days

### After Deployment
- [ ] Monitor logs for validation errors
- [ ] Check CORS access logs (should see from Dashboard only)
- [ ] Verify retention cleanup runs daily
- [ ] Test firewall rules (ports 5000, 5001 accessible)
- [ ] Backup strategy for `data_dir` (important data!)

---

## Security Checklist

- [ ] Change all `auth_tokens` in Manager config (randomly generated)
- [ ] Change `manager_token` in Dashboard config (must match one of Manager's tokens)
- [ ] Update `cors_origins` to match actual Dashboard URLs
- [ ] Set `debug: false` in both Manager and Dashboard configs
- [ ] Review and adjust `retention_days` policy (default 90 days)
- [ ] Plan log rotation (logs grow over time)
- [ ] Consider HTTPS with nginx reverse proxy (production)
- [ ] Restrict access to `data_dir` (contains all historical data)
- [ ] Test authentication: Verify tokens work correctly
- [ ] Monitor error logs for attack patterns

---

## Backward Compatibility

- ✅ Manager still accepts collector's single-report format
- ✅ Existing JSONL files (no migration needed)
- ✅ Config structure extended (optional fields have defaults)
- ✅ API endpoints unchanged (just added auth requirements)

---

## Files Changed

- `manager/manager.py` - Major: Added validation, auth decorators, cleanup, error handling
- `manager/README.md` - Complete rewrite: Added Windows setup, security notes
- `manager/config.example.json` - Added CORS, retention_days, debug fields
- `dashboard/app.py` - Added token auth, input validation, error handling
- `dashboard/README.md` - Complete rewrite: Added Windows setup, troubleshooting
- `dashboard/config.example.json` - Added manager_token, timeout, debug
- `dashboard/static/js/dashboard.js` - Added XSS protection, usage bar calc, error handling
- `SECURITY_FIXES.md` - NEW: This document

---

## Deployment Instructions

See `manager/README.md` and `dashboard/README.md` for complete Windows Server setup.

Quick start:
1. Install Python 3.8+
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `config.example.json` → `config.json` (update tokens!)
4. Create data directories
5. Run as Windows Service (or manually for testing)
6. Configure firewall
7. Test connectivity

---

**Version:** v0.0.15 (pending release)
**Status:** Ready for production deployment
