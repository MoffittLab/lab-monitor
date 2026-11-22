# Configuration and Security

**Never commit your real config files to git.** They contain tokens and password hashes.

---

## Where the configs live

| Component | Real config (not tracked) | Template (tracked) | Created by |
|-----------|---------------------------|--------------------|------------|
| Manager | `manager/config.json` | `manager/config.example.json`, `manager/config.windows.json` | `install-manager-dashboard-taskscheduler.ps1` |
| Dashboard | `dashboard/config.json` | `dashboard/config.example.json` | same installer |
| Collector | `collector/local/config.json` | `collector/config.example.json`, `config.synology.example.json`, `config.windows.example.json` | `install-collector-synology.sh`, `install-collector-windows.ps1` |

The Manager installer writes its two configs with random tokens; the collector installers ask for
the device's token. To create a config by hand, copy the template and replace **every** placeholder. The Manager refuses to start with the template's tokens
(`GENERATE-RANDOM-…`), the Dashboard refuses its template `auth` block until it holds a real hash
(and warns about a placeholder `manager_token`), and a collector's placeholder token simply matches
no Manager token (HTTP 401).

`.gitignore` covers `config.json` everywhere, the `local/` folders (such as `collector/local/`), `*.local.json`, `data/`,
`logs/`, `tools/collectors.csv` (SSH passwords) and `tools/.known_hosts`. Check before committing:

```bash
git status        # no config.json, collectors.csv or .known_hosts should appear
```

Full key references: [manager/README.md](manager/README.md#configuration),
[dashboard/README.md](dashboard/README.md#configuration),
[collector/README.md](collector/README.md#configuration).

---

## Manager tokens

Every Manager endpoint except `GET /health` needs `Authorization: Bearer <token>`. Tokens are
scoped:

| Key | Who uses it | Allowed |
|-----|-------------|---------|
| `admin_tokens` | You, and tools that delete systems (`tools/reset_system.py`) | Everything, including `DELETE /api/system/<name>` and `GET /api/dead_letter` |
| `read_tokens` | The Dashboard (`manager_token` in `dashboard/config.json`) | GET endpoints only |
| `collector_tokens` | Each collector (`manager_token` in its config) | Only `POST /api/data/queue`, and only for its own device |
| `auth_tokens` | Legacy (configs from before the upgrade) | Everything, plus writing for any device. Still accepted; a deprecation warning is logged at startup |

```json
{
  "admin_tokens": ["<random admin token>"],
  "read_tokens":  ["<random read token for the Dashboard>"],
  "collector_tokens": {
    "Triton": "<random token for Triton>",
    "Atlas":  "<random token for Atlas>"
  }
}
```

- **Generate a token:** `python -c "import secrets; print(secrets.token_hex(32))"`
- **The Manager refuses to start** (exit code 2) when a token contains a placeholder marker
  (`CHANGE-ME`, `CHANGEME`, `YOUR-`, `GENERATE-RANDOM`, `PLACEHOLDER`, `REPLACE-ME`, and the `_`
  variants, in any case), is shorter than 16 characters, or when no token is configured at all.
  Check a config without starting anything:
  `python manager.py --config config.json --check-config`.
- **`collector_tokens` keys** are device names (`^[A-Za-z0-9_.-]{1,64}$`), compared with the
  collector's `name` after normalization (case-insensitive; other characters count as `_`). For a
  device the previous Manager registered with a name outside that pattern, such as `Lab NAS`, use
  `Lab_NAS`. The key `"*"` allows any device (logged as a warning at startup); avoid it.
- Every comparison is constant-time (`hmac.compare_digest`).
- 401 = missing or unknown token; 403 = a valid token without the right scope or for another
  device.
- The Manager reads its config at startup: restart it after changing tokens.

Moving an existing installation from `auth_tokens` to scoped tokens step by step:
[docs/UPGRADE.md](docs/UPGRADE.md#step-6-afterwards). Keep `auth_tokens` until you no longer need
to roll back, because the previous Manager knows only that key.

---

## Other security settings

**Manager**
- `cors_origins`: empty or missing means CORS is **off**. The Dashboard calls the Manager from its
  server, so browsers never need cross-origin access. Only list origins for a browser application
  that calls the Manager directly.
- `max_request_bytes` (32 MiB) and `max_messages_per_request` (10000) limit request size.
- Error responses carry short generic messages; details go to `manager.log`.

**Dashboard**
- `manager_token` should be a **read** token, so a compromised Dashboard host can't send data or
  delete systems.
- `auth`: optional HTTP basic auth for every page and API route (except `GET /health`). Create the
  block with `python app.py --hash-password` (it prints `{"auth": {"username", "password_hash"}}`).
  The Dashboard refuses to start with an incomplete `auth` block or a password that is not a hash,
  and warns at startup when it listens on a non-loopback address without `auth`.
- The browser never sees the Manager token or URL.

**Collectors**
- `config.json` holds the device's token. The Synology installer creates it with mode 600; the
  Windows installer makes `collector\local\` readable only by SYSTEM and Administrators.
- `manager_url` may be `http://` or `https://`. With `http://` the token travels unencrypted.

**Transport**
- The Manager and the Dashboard serve plain **HTTP** (waitress; no TLS). On an untrusted network,
  put a TLS reverse proxy in front of them and use `https://` URLs; the collectors verify the
  proxy's certificate. Otherwise keep ports 5000/5001 limited to the lab network.

**Fleet tools**
- `tools/collectors.csv` holds SSH credentials: keep it out of git (it is ignored) and prefer keys
  (`key_filename` column) over passwords.
- SSH host keys are trusted on first use and pinned in `tools/.known_hosts`; a changed key is
  refused.

---

## Manager settings that protect data

| Key | Default | Meaning |
|-----|---------|---------|
| `backup_before_migration` | `true` | Before a database written by an older Manager is changed for the first time, it is copied to `<db>.pre-upgrade-<YYYYmmddTHHMMSSZ>.bak` next to it (once per database; a copy with nothing written since is reused). If the copy fails, that database is not migrated: reads keep working, writes get `error` (collectors keep the messages), and the copy is retried on the next write after 300 s or at the next start. `false` migrates without a copy. Must be `true` or `false` |
| `history_retention_days` | `null` | `null` keeps all history forever. A number N archives rows older than N days to `data/<system>/archive/<YYYY-MM>-<data_type>.jsonl` once a day, then deletes them from the database |
| `retention_days` | — | Legacy. It only applied to the old `usage.jsonl` reports and has no effect now |
| `data_dir` | `data` (installer: `E:\Users\lab-monitor\manager-data`) | Keep it on a **local disk**, in a folder nothing else writes to. SQLite is not reliable on a network share (`disk I/O error`, failed transactions); the Manager logs a `WARNING` at every start while it is on a share or holds a collector's files ([manager/README.md](manager/README.md#recommended-layout)). An existing installation moves it in [docs/UPGRADE.md, Step 3b](docs/UPGRADE.md#step-3-upgrade-the-manager-and-dashboard) |
| `snapshot` | off | Nightly consistent copies of every database to `snapshot.dest_dir` (a NAS folder outside `data_dir`), each verified by SHA-256 and `quick_check`; 14 daily and 12 monthly kept. A database that can't be copied is reported (`partial`) while the others still are, and the last good copy of it is kept. The snapshots hold the same per-user data as the databases and are not removed by `DELETE /api/system` ([manager/README.md](manager/README.md#snapshots-to-a-nas)) |

---

## Accidental commits

If you committed a config with real tokens:

```bash
git rm --cached manager/config.json
git commit -m "Stop tracking config.json"
```

This removes it going forward, but the old commits still contain it. **Treat the tokens as exposed
and replace them**: generate new ones, update the Manager config, restart it, and update the
Dashboard and the collectors.

---

## See Also

- [INSTALLATION.md](INSTALLATION.md) — Setup instructions
- [docs/UPGRADE.md](docs/UPGRADE.md) — Upgrading an existing installation
- [manager/README.md](manager/README.md) — All Manager settings and endpoints
- `.gitignore` — What is kept out of git
