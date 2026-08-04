# Configuration Management

**Never commit your actual config files to git.** They contain auth tokens and sensitive data.

---

## Pattern

### 1. Example Files (Tracked in Git)
Each component has a `config.example.json` file:
- `manager/config.example.json`
- `dashboard/config.example.json`
- `collector/config.example.json`

These show the structure and acceptable values. They're safe to commit.

### 2. Real Config Files (NOT Tracked)
When deploying, copy the example:

**Windows (Manager/Dashboard):**
```powershell
cd E:\Users\lab-monitor\scripts\lab-monitor\manager
copy config.example.json config.json
# Edit config.json with real values
notepad config.json
```

**Synology (Collector):**
```bash
cd /volume1/lab-monitor/scripts/lab-monitor/collector
cp config.example.json config.json
# Edit config.json with real values
nano config.json
```

Then edit `config.json` with your actual auth tokens and URLs.

### 3. Git Protection (.gitignore)
All `config.json` files are in `.gitignore`:

```
manager/config.json      # NOT tracked
dashboard/config.json    # NOT tracked
collector/config.json    # NOT tracked
```

**Verify before committing:**
```bash
git status
# Should show: nothing config-related
# Or if you see config.json → DON'T COMMIT IT
```

---

## Why This Matters

**❌ Bad:**
```json
{
  "auth_tokens": ["super-secret-token-abc123", "another-secret"]
}
```
If this is in git, anyone with repo access sees your secrets.

**✅ Good:**
```json
{
  "auth_tokens": ["GENERATE-RANDOM-TOKEN-1", "GENERATE-RANDOM-TOKEN-2"]
}
```
This is in git as an example. Real tokens go in `config.json` (not tracked).

---

## Workflow Summary

| File | In Git? | Purpose |
|------|---------|---------|
| `config.example.json` | ✅ Yes | Shows structure, template |
| `config.json` | ❌ No | Actual deployment values |
| `config.windows.json` | ✅ Yes | Alternative template (reference) |

---

## Team Sharing

**To share deployment with team without exposing secrets:**

1. Share `config.example.json` - Safe template
2. Share **setup instructions** pointing to WINDOWS-INSTALL.md
3. Each person generates their own tokens
4. Each person maintains their own `config.json` (not in git)

If you need to change config structure, update `config.example.json` and push it. Others can see the new template without exposing their real values.

---

## Accidental Commits

If you accidentally committed `config.json` with secrets:

```bash
# Remove it from git history (CRITICAL)
git rm --cached manager/config.json
git commit -m "Remove config.json from tracking"

# Rotate your tokens immediately
# (Assume they're compromised)
```

This removes it going forward, but doesn't erase old history. Treat compromised tokens as exposed.

---

## See Also

- **WINDOWS-INSTALL.md** - Setup instructions
- **.gitignore** - Protection rules
