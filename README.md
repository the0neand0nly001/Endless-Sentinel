# Endless Sentinel

Endless Sentinel is a secure Flask operations dashboard for Proxmox VE, Kubernetes/k3s and local or remote Docker Engines. A single resilient polling coordinator normalises live infrastructure state, retains the last good result when a platform becomes unreachable, marks old data stale, evaluates CPU/RAM and resource-health alerts, and sends deduplicated Discord alerts and recoveries.

## Screenshots

Add real dashboard, infrastructure, alerts, setup, login and mobile screenshots here after deployment. No simulated screenshots are included.

## Requirements

- Linux on x86-64, ARM64 or AArch64
- Docker Engine with Compose v2 (recommended), or Python 3.11+
- Network access from the service to each enabled API
- A Proxmox API token, kubeconfig/service account, or Docker endpoint as applicable

## Quick installation

The project currently has no confirmed public remote, so a truthful copy-paste remote installer cannot be published yet. After the maintainer sets `REPOSITORY_RAW_URL` to this repository's real raw URL, the one-line form is:

```bash
curl -fsSL "REPLACE_WITH_REAL_REPOSITORY_RAW_URL/setup.sh" | sudo bash -s -- --quick
```

Do **not** publish that command until the placeholder is replaced with the real repository URL. From a checked-out copy, use the working command:

```bash
sudo ./setup.sh --quick
```

The wizard securely prompts for the administrator password. Only its Werkzeug hash is stored. Run `sudo ./setup.sh` without arguments for the PE Helper Scripts–style menu, or choose `--advanced`. Other actions are `--update`, `--reconfigure`, `--status`, `--logs`, `--restart` and `--uninstall`. Add `--install-dir /path` where appropriate. Reconfiguration creates a timestamped `.env` backup and writes the replacement atomically with mode `600`.

## Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('choose-a-password'))"
# Put both results in .env, then:
flask --app app run --host 127.0.0.1 --port 8080
```

Production without Docker:

```bash
gunicorn --workers 1 --threads 8 --bind 0.0.0.0:8080 app:app
```

Keep exactly one worker: the polling scheduler lives in-process and multiple workers would duplicate polls. Threads provide request concurrency.

## Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
```

The image runs as an unprivileged user, drops Linux capabilities and has a health check. Uncomment only the mounts required by your configuration. Mounting `/var/run/docker.sock` effectively grants root-equivalent control of the host to the container; use a restricted socket proxy or a TLS-protected remote Docker API where possible.

## Configuration reference

| Variable | Default | Purpose |
|---|---:|---|
| `APP_SECRET_KEY` | required | Random session/CSRF signing secret |
| `ADMIN_USERNAME` | `admin` | Sole administrator login |
| `ADMIN_PASSWORD_HASH` | required | Werkzeug password hash, never plaintext |
| `SESSION_LIFETIME_MINUTES` | `480` | Authenticated session duration |
| `SESSION_COOKIE_SECURE` | `false` | Send session cookie over HTTPS only |
| `TRUST_PROXY_COUNT` | `0` | Number of explicitly trusted reverse proxies |
| `BIND_ADDRESS`, `WEB_PORT` | `0.0.0.0`, `8080` | Local server bind settings |
| `POLL_INTERVAL_SECONDS` | `30` | Background poll interval |
| `CPU_WARNING_PERCENT`, `CPU_CRITICAL_PERCENT` | `80`, `95` | CPU thresholds |
| `RAM_WARNING_PERCENT`, `RAM_CRITICAL_PERCENT` | `80`, `95` | RAM thresholds |
| `ALERT_COOLDOWN_SECONDS` | `900` | Operational deduplication cooldown |
| `STALE_DATA_SECONDS` | `120` | Age at which last-good data is marked stale |
| `DISCORD_WEBHOOK_URL` | blank | Infrastructure alerts only |
| `SECURITY_DISCORD_WEBHOOK_URL` | blank | Authentication events only; must differ |
| `LOGIN_RATE_LIMIT_MINUTE`, `LOGIN_RATE_LIMIT_HOUR` | `5`, `20` | POST `/login` limits per trusted client IP |
| `LOGIN_FAILURE_ALERT_THRESHOLD` | `5` | Failures before security notification |
| `SECURITY_ALERT_COOLDOWN_SECONDS` | `900` | Security notification cooldown |
| `RATELIMIT_STORAGE_URI` | `memory://` | Limiter backend; use Redis for multi-instance deployment |
| `PROXMOX_ENABLED` | `false` | Enable Proxmox polling |
| `PROXMOX_HOST`, `PROXMOX_PORT` | blank, `8006` | Proxmox address |
| `PROXMOX_USER`, `PROXMOX_TOKEN_NAME`, `PROXMOX_TOKEN_VALUE` | blank | API token credentials |
| `PROXMOX_VERIFY_SSL` | `true` | Verify Proxmox TLS |
| `K3S_ENABLED` | `false` | Enable Kubernetes polling |
| `KUBECONFIG_PATH`, `K3S_CONTEXT` | blank | Explicit kubeconfig/context; in-cluster config is automatic |
| `DOCKER_ENABLED` | `false` | Enable Docker polling |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Local socket or TCP endpoint |
| `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH` | `false`, blank | Remote Docker TLS settings |

The authenticated **Setup** page can enable only integrations that were disabled during installation. This permits initial setup from the browser without allowing an active integration's secrets to be revealed or overwritten. Restart after saving. Use `./setup.sh --reconfigure` for already-enabled integrations.

## Authentication and password reset

Sessions are HTTP-only and SameSite=Lax, logout is POST-only with CSRF, and redirect targets are restricted to the same host. Proxy headers are ignored unless `TRUST_PROXY_COUNT` is non-zero. Set secure cookies only after HTTPS is working.

Reset a password on the host:

```bash
python -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass()))"
```

Replace `ADMIN_PASSWORD_HASH` in `.env` with the output and restart. Never put the plaintext password in `.env`. Failed attempts log timestamp, trusted IP, sanitised agent and normalised attempted username—never the password. Operational and security webhooks are structurally separate; if identical URLs are supplied, the security webhook is rejected.

## Platform configuration

### Proxmox

In Proxmox, create a dedicated user and API token under **Datacenter → Permissions → API Tokens**, grant only audit/read permissions required for nodes and guests, and disable privilege separation only if your permission model explicitly requires it. Store the token name/value in `.env`. Prefer a valid internal CA and keep SSL verification enabled.

### k3s / Kubernetes

For Compose, copy a least-privilege kubeconfig readable by the container and mount it at `/config/kubeconfig`; ensure the server URL is reachable from inside Docker. For an in-cluster deployment, the example `deploy/k3s.yaml` creates read-only node/pod RBAC. Create the environment Secret separately, replace the image marker, then apply it. Metrics Server is optional: core health still works without it.

### Docker

Local monitoring requires mounting the socket and matching its permissions. Remote monitoring should use `tcp://host:2376`, TLS verification, and read-limited infrastructure. Never expose an unauthenticated Docker TCP socket.

## Reverse proxy

Proxy to `http://127.0.0.1:8080`, terminate TLS, set `SESSION_COOKIE_SECURE=true`, and set `TRUST_PROXY_COUNT` to the exact number of trusted proxy hops. Do not increase it speculatively: that would let untrusted clients forge source IPs used by login limiting.

## Routes

| Route | Access | Function |
|---|---|---|
| `/login` | public | Rate-limited administrator sign-in |
| `/healthz` | public | Minimal `{status: ok}` liveness response |
| `/` | authenticated | Overall health overview |
| `/infrastructure` | authenticated | Resource details |
| `/alerts` | authenticated | Active/recent alert timeline |
| `/settings` | authenticated | Enable integrations disabled at install time |
| `/logout` | authenticated POST | CSRF-protected logout |
| `/api/status` | authenticated | Normalised dashboard JSON, no secrets |
| `/api/refresh` | authenticated POST | CSRF-protected asynchronous manual poll |

## Updating and troubleshooting

Use `sudo ./setup.sh --update`; existing `.env` values are retained. Status/log/restart commands are printed by the wizard. For connection failures, check container DNS/routing, API permissions, certificate chains and mounted-file permissions. A failed platform does not terminate other collectors; the UI reports the error and retains the last good resource list with a stale marker.

Discord delivery failures are logged without webhook URLs and never interrupt polling or authentication. Memory-based rate limits are appropriate for the required one-worker container; use Redis for multiple replicas. The optional k3s example uses one replica to avoid duplicate schedulers.

## Security considerations

Keep `.env` mode `600`, rotate tokens/webhooks after suspected exposure, use least-privilege API identities, deploy behind HTTPS, do not expose port 8080 directly to the internet, and treat Docker socket access as host-root access. API output and templates never include stored credentials. Application errors may contain upstream connection descriptions, so restrict dashboard access and logs.
