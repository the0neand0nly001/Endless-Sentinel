#!/usr/bin/env bash
# Endless Sentinel fresh-host installer and interactive bootstrap.
#
# When downloaded or piped to Bash, this script installs the small set of host
# prerequisites needed to clone the repository. From the local checkout it can
# install Docker Engine and Compose on supported Debian/Ubuntu hosts, collect
# monitoring settings, build the application, and wait for its health check.
set -Eeuo pipefail

REPOSITORY_URL="${ENDLESS_SENTINEL_REPOSITORY_URL:-https://github.com/the0neand0nly/Endless-Sentinel.git}"
REPOSITORY_BRANCH="${ENDLESS_SENTINEL_BRANCH:-main}"

DEFAULT_INSTALL_DIR="${PWD}/Endless-Sentinel"
if [[ "${EUID}" -eq 0 && -d /opt ]]; then
  DEFAULT_INSTALL_DIR="/opt/endless-sentinel"
fi
INSTALL_DIR="${ENDLESS_SENTINEL_INSTALL_DIR:-${DEFAULT_INSTALL_DIR}}"

CONFIGURE=false
START=true
INTERACTIVE=true
AUTO_INSTALL_DOCKER="${ENDLESS_SENTINEL_AUTO_INSTALL_DOCKER:-true}"
FORCE_DOCKER_INSTALL="${ENDLESS_SENTINEL_FORCE_DOCKER_INSTALL:-false}"
ROOT_COMMAND=()
DOCKER_COMMAND=()
COMPOSE_COMMAND=()
LXC_NOTICE_SHOWN=false

is_true() {
  case "${1,,}" in
    1|true|y|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

usage() {
  printf '%s\n' \
    "Usage: ./setup.sh [--configure] [--no-start] [--non-interactive]" \
    "" \
    "  --configure           Prompt for Proxmox, k3s, Docker, Discord, and port settings." \
    "  --no-start            Prepare configuration without installing or starting Docker." \
    "  --non-interactive     Accept defaults and values already present in .env." \
    "  --skip-docker-install Require an existing Docker Engine instead of installing it." \
    "  --help                Show this help." \
    "" \
    "Standalone installer environment variables:" \
    "  ENDLESS_SENTINEL_INSTALL_DIR         Clone destination (root default: /opt/endless-sentinel)." \
    "  ENDLESS_SENTINEL_REPOSITORY_URL      Alternate Git repository URL." \
    "  ENDLESS_SENTINEL_BRANCH              Git branch to clone (default: main)." \
    "  ENDLESS_SENTINEL_AUTO_INSTALL_DOCKER Set false to require an existing Docker installation."
}

for argument in "$@"; do
  case "${argument}" in
    --configure) CONFIGURE=true ;;
    --no-start) START=false ;;
    --non-interactive) INTERACTIVE=false ;;
    --skip-docker-install) AUTO_INSTALL_DOCKER=false ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown option: %s\n' "${argument}" >&2; usage >&2; exit 2 ;;
  esac
done

fail() {
  printf 'Endless Sentinel setup error: %s\n' "$1" >&2
  exit 1
}

on_error() {
  local exit_status=$?
  printf 'Endless Sentinel setup stopped near line %s (exit %s).\n' "${BASH_LINENO[0]:-${LINENO}}" "${exit_status}" >&2
  exit "${exit_status}"
}
trap on_error ERR

resolve_script_dir() {
  local source_path="${BASH_SOURCE[0]:-}"
  if [[ -n "${source_path}" && -f "${source_path}" ]]; then
    cd -- "$(dirname -- "${source_path}")" && pwd
  fi
}

project_is_complete() {
  local project_root="$1"
  [[ -f "${project_root}/.env.example" \
    && -f "${project_root}/setup.sh" \
    && -f "${project_root}/app.py" \
    && -f "${project_root}/Dockerfile" \
    && -f "${project_root}/docker-compose.yml" ]]
}

configure_root_command() {
  if [[ "${EUID}" -eq 0 ]]; then
    ROOT_COMMAND=()
  elif command -v sudo >/dev/null 2>&1; then
    ROOT_COMMAND=(sudo)
  else
    fail "Root access is required to install Docker. Run this command as root inside the LXC, or install Docker first."
  fi
}

run_root() {
  "${ROOT_COMMAND[@]}" "$@"
}

load_supported_os() {
  [[ -r /etc/os-release ]] || fail "Cannot identify this operating system because /etc/os-release is missing."
  # shellcheck disable=SC1091
  . /etc/os-release
  DETECTED_OS_ID="${ID:-}"
  DETECTED_OS_CODENAME="${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}"
  case "${DETECTED_OS_ID}" in
    debian|ubuntu) ;;
    *) fail "Automatic Docker installation supports Debian and Ubuntu. Install Docker Engine and Compose manually, then rerun with --skip-docker-install." ;;
  esac
  [[ -n "${DETECTED_OS_CODENAME}" ]] || fail "The Debian/Ubuntu release codename could not be detected."
  command -v apt-get >/dev/null 2>&1 || fail "apt-get is required for automatic installation on Debian/Ubuntu."
}

install_host_prerequisites() {
  if command -v git >/dev/null 2>&1 \
    && command -v curl >/dev/null 2>&1 \
    && command -v openssl >/dev/null 2>&1 \
    && [[ -s /etc/ssl/certs/ca-certificates.crt ]]; then
    return
  fi

  load_supported_os
  configure_root_command
  printf 'Installing Git, TLS certificates, and setup prerequisites…\n'
  run_root apt-get update
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates curl git gnupg openssl
}

is_lxc() {
  [[ "$(systemd-detect-virt --container 2>/dev/null || true)" == "lxc" ]] \
    || grep -aqE 'container=(lxc|liblxc)' /proc/1/environ 2>/dev/null
}

print_lxc_requirements() {
  if [[ "${LXC_NOTICE_SHOWN}" == "true" ]] || ! is_lxc; then
    return
  fi
  LXC_NOTICE_SHOWN=true
  printf '%s\n' \
    "" \
    "Proxmox LXC detected." \
    "Docker inside an LXC requires Nesting; an unprivileged LXC also requires Keyctl." \
    "In Proxmox: select the LXC → Options → Features → enable Nesting and Keyctl," \
    "then fully restart the LXC. The installer will verify Docker before continuing." \
    ""
}

installed_conflicting_docker_packages() {
  local package status conflicts=()
  for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
    status="$(dpkg-query -W -f='${Status}' "${package}" 2>/dev/null || true)"
    [[ "${status}" == "install ok installed" ]] && conflicts+=("${package}")
  done
  if ((${#conflicts[@]})); then
    printf '%s' "${conflicts[*]}"
  fi
}

configure_docker_repository() {
  local architecture key_file source_file
  load_supported_os
  configure_root_command
  architecture="$(dpkg --print-architecture)"
  key_file="$(mktemp /tmp/endless-sentinel-docker-key.XXXXXX)"
  source_file="$(mktemp /tmp/endless-sentinel-docker-source.XXXXXX)"

  curl -fsSL "https://download.docker.com/linux/${DETECTED_OS_ID}/gpg" -o "${key_file}" \
    || fail "Could not download Docker's official signing key. Check DNS and internet access."

  printf '%s\n' \
    "Types: deb" \
    "URIs: https://download.docker.com/linux/${DETECTED_OS_ID}" \
    "Suites: ${DETECTED_OS_CODENAME}" \
    "Components: stable" \
    "Architectures: ${architecture}" \
    "Signed-By: /etc/apt/keyrings/docker.asc" > "${source_file}"

  run_root install -m 0755 -d /etc/apt/keyrings
  run_root install -m 0644 "${key_file}" /etc/apt/keyrings/docker.asc
  run_root install -m 0644 "${source_file}" /etc/apt/sources.list.d/docker.sources
  rm -f "${key_file}" "${source_file}"
}

install_docker_engine() {
  local conflicts
  install_host_prerequisites
  print_lxc_requirements
  conflicts="$(installed_conflicting_docker_packages)"
  if [[ -n "${conflicts}" ]]; then
    fail "Conflicting container packages are installed (${conflicts}). This automatic installer will not remove an existing container stack. Remove or migrate them, then rerun setup."
  fi

  printf 'Installing Docker Engine, Buildx, and Docker Compose from Docker’s official repository…\n'
  configure_docker_repository
  run_root apt-get update
  run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

start_docker_daemon() {
  configure_root_command
  if command -v systemctl >/dev/null 2>&1; then
    run_root systemctl enable docker >/dev/null 2>&1 || true
    run_root systemctl start docker >/dev/null 2>&1 || true
  elif command -v service >/dev/null 2>&1; then
    run_root service docker start >/dev/null 2>&1 || true
  fi
}

select_docker_command() {
  local attempt
  for attempt in {1..12}; do
    if docker info >/dev/null 2>&1; then
      DOCKER_COMMAND=(docker)
      return 0
    fi
    if [[ "${EUID}" -ne 0 ]] && run_root docker info >/dev/null 2>&1; then
      DOCKER_COMMAND=("${ROOT_COMMAND[@]}" docker)
      return 0
    fi
    sleep 1
  done
  return 1
}

docker_failure_message() {
  if is_lxc; then
    fail "Docker could not start inside this LXC. On the Proxmox host, enable Nesting and Keyctl for this LXC, fully restart it, then rerun the same curl command. If they are already enabled, inspect: journalctl -u docker --no-pager -n 80"
  fi
  fail "Docker is installed but its daemon is unavailable. Inspect: journalctl -u docker --no-pager -n 80"
}

ensure_docker() {
  configure_root_command
  print_lxc_requirements

  if is_true "${FORCE_DOCKER_INSTALL}" || ! command -v docker >/dev/null 2>&1; then
    if is_true "${AUTO_INSTALL_DOCKER}"; then
      install_docker_engine
    else
      fail "Docker is not installed. Remove --skip-docker-install or install Docker Engine and Compose first."
    fi
  fi

  start_docker_daemon
  select_docker_command || docker_failure_message

  if "${DOCKER_COMMAND[@]}" compose version >/dev/null 2>&1; then
    COMPOSE_COMMAND=("${DOCKER_COMMAND[@]}" compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_COMMAND=(docker-compose)
  else
    fail "Docker is running, but Docker Compose is missing. Install the Docker Compose plugin and rerun setup."
  fi
  printf 'Docker Engine and Docker Compose are ready.\n'
}

bootstrap_project() {
  [[ "${REPOSITORY_BRANCH}" =~ ^[A-Za-z0-9._/-]+$ ]] \
    || fail "ENDLESS_SENTINEL_BRANCH contains unsupported characters."

  if project_is_complete "${INSTALL_DIR}"; then
    printf 'Using the existing Endless Sentinel checkout at %s\n' "${INSTALL_DIR}"
  else
    install_host_prerequisites
    if [[ -e "${INSTALL_DIR}" && ! -d "${INSTALL_DIR}" ]]; then
      fail "${INSTALL_DIR} exists and is not a directory. Choose another path with ENDLESS_SENTINEL_INSTALL_DIR."
    fi
    if [[ -d "${INSTALL_DIR}" && -n "$(find "${INSTALL_DIR}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
      fail "${INSTALL_DIR} exists and is not an empty Endless Sentinel checkout. Choose another path with ENDLESS_SENTINEL_INSTALL_DIR."
    fi
    mkdir -p "$(dirname -- "${INSTALL_DIR}")"
    printf 'Downloading Endless Sentinel into %s…\n' "${INSTALL_DIR}"
    git clone --depth 1 --branch "${REPOSITORY_BRANCH}" "${REPOSITORY_URL}" "${INSTALL_DIR}"
  fi

  project_is_complete "${INSTALL_DIR}" \
    || fail "The downloaded repository is incomplete. Confirm the GitHub repository contains the full project."

  if [[ -t 1 && -r /dev/tty && -w /dev/tty ]]; then
    exec env ENDLESS_SENTINEL_BOOTSTRAPPED=1 bash "${INSTALL_DIR}/setup.sh" "$@" </dev/tty >/dev/tty
  fi
  exec env ENDLESS_SENTINEL_BOOTSTRAPPED=1 bash "${INSTALL_DIR}/setup.sh" "$@"
}

SCRIPT_DIR="$(resolve_script_dir)"
if [[ -z "${SCRIPT_DIR}" ]] || ! project_is_complete "${SCRIPT_DIR}"; then
  bootstrap_project "$@"
  exit 0
fi

ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"
[[ -f "${ENV_EXAMPLE}" ]] || fail ".env.example is missing from ${SCRIPT_DIR}."
cd "${SCRIPT_DIR}"

if [[ "${START}" == "true" ]]; then
  ensure_docker
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  [[ -t 0 ]] && CONFIGURE=true
  printf 'Created .env with owner-only permissions.\n'
fi

set_env() {
  local key="$1" value="$2" temporary
  [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || fail "${key} contains a newline."
  temporary="$(mktemp "${SCRIPT_DIR}/.env.XXXXXX")"
  awk -v key="${key}" -v value="${value}" '
    BEGIN { found = 0 }
    index($0, key "=") == 1 { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "${ENV_FILE}" > "${temporary}"
  chmod 600 "${temporary}"
  mv "${temporary}" "${ENV_FILE}"
}

get_env() {
  local key="$1"
  awk -v key="${key}" 'index($0, key "=") == 1 { sub("^[^=]*=", ""); print; exit }' "${ENV_FILE}"
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import secrets; print(secrets.token_hex(32))'
  else
    fail "openssl or python3 is required to generate FLASK_SECRET_KEY."
  fi
}

if [[ -z "$(get_env FLASK_SECRET_KEY)" ]]; then
  set_env FLASK_SECRET_KEY "$(generate_secret)"
fi

if [[ -S /var/run/docker.sock ]] && command -v stat >/dev/null 2>&1; then
  SOCKET_GID="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || stat -f '%g' /var/run/docker.sock 2>/dev/null || true)"
  [[ -n "${SOCKET_GID}" ]] && set_env DOCKER_GID "${SOCKET_GID}"
fi

prompt_text() {
  local label="$1" key="$2" current answer
  current="$(get_env "${key}")"
  read -r -p "${label}${current:+ [${current}]}: " answer
  set_env "${key}" "${answer:-${current}}"
}

prompt_required_text() {
  local label="$1" key="$2"
  while true; do
    prompt_text "${label}" "${key}"
    [[ -n "$(get_env "${key}")" ]] && return
    printf '%s is required.\n' "${label}"
  done
}

prompt_secret() {
  local label="$1" key="$2" answer
  read -r -s -p "${label} (leave blank to keep the current value): " answer
  printf '\n'
  [[ -n "${answer}" ]] && set_env "${key}" "${answer}"
}

prompt_required_secret() {
  local label="$1" key="$2"
  while true; do
    prompt_secret "${label}" "${key}"
    [[ -n "$(get_env "${key}")" ]] && return
    printf '%s is required.\n' "${label}"
  done
}

prompt_yes_no() {
  local label="$1" default="$2" answer suffix
  suffix="[y/N]"
  is_true "${default}" && suffix="[Y/n]"
  read -r -p "${label} ${suffix}: " answer
  answer="${answer:-${default}}"
  is_true "${answer}"
}

prompt_port() {
  local label="$1" key="$2" value
  while true; do
    prompt_text "${label}" "${key}"
    value="$(get_env "${key}")"
    if [[ "${value}" =~ ^[0-9]+$ ]] && ((value >= 1 && value <= 65535)); then
      return
    fi
    printf 'Enter a port from 1 to 65535.\n'
  done
}

if [[ "${CONFIGURE}" == "true" && "${INTERACTIVE}" == "true" && ! -t 0 ]]; then
  fail "Interactive configuration needs a terminal. Run the curl command from an SSH session or the LXC console."
fi

if [[ "${CONFIGURE}" == "true" && "${INTERACTIVE}" == "true" ]]; then
  printf '\nConfigure Endless Sentinel. Secrets are written only to %s.\n\n' "${ENV_FILE}"

  if prompt_yes_no "Monitor the Proxmox cluster through its API?" "$(get_env PROXMOX_ENABLED)"; then
    set_env PROXMOX_ENABLED true
    printf 'Use a dedicated read-only Proxmox API token with the PVEAuditor role.\n'
    prompt_required_text "Proxmox host or URL" PROXMOX_HOST
    prompt_required_text "Proxmox user (for example sentinel@pve)" PROXMOX_USER
    prompt_required_text "Proxmox API token name" PROXMOX_TOKEN_NAME
    prompt_required_secret "Proxmox API token value" PROXMOX_TOKEN_VALUE
    if prompt_yes_no "Verify the Proxmox TLS certificate? Answer no for its default self-signed certificate." "$(get_env PROXMOX_VERIFY_SSL)"; then
      set_env PROXMOX_VERIFY_SSL true
    else
      set_env PROXMOX_VERIFY_SSL false
    fi
  else
    set_env PROXMOX_ENABLED false
  fi

  if prompt_yes_no "Monitor a k3s/Kubernetes cluster using a kubeconfig file?" "$(get_env K3S_ENABLED)"; then
    set_env K3S_ENABLED true
    while true; do
      prompt_required_text "Absolute path to the kubeconfig inside this LXC" K3S_KUBECONFIG_HOST
      KUBECONFIG_HOST_VALUE="$(get_env K3S_KUBECONFIG_HOST)"
      if [[ -f "${KUBECONFIG_HOST_VALUE}" && -r "${KUBECONFIG_HOST_VALUE}" ]]; then
        break
      fi
      printf 'That kubeconfig is not a readable file. Copy it into this LXC, then enter its absolute path.\n'
      set_env K3S_KUBECONFIG_HOST ""
    done
    set_env K3S_KUBECONFIG /config/kubeconfig
    set_env K3S_IN_CLUSTER false
  else
    set_env K3S_ENABLED false
    set_env K3S_KUBECONFIG_HOST /dev/null
  fi

  if prompt_yes_no "Monitor Docker containers running inside this LXC?" "$(get_env DOCKER_ENABLED)"; then
    set_env DOCKER_ENABLED true
    set_env DOCKER_HOST unix:///var/run/docker.sock
    set_env DOCKER_SOCKET_PATH /var/run/docker.sock
    set_env DOCKER_TLS_VERIFY false
    set_env DOCKER_CERT_PATH /config/docker-certs
    set_env DOCKER_CERT_PATH_HOST /dev/null
  else
    set_env DOCKER_ENABLED false
  fi

  if prompt_yes_no "Send threshold and recovery alerts to Discord?" "$([[ -n "$(get_env DISCORD_WEBHOOK_URL)" ]] && printf true || printf false)"; then
    prompt_required_secret "Discord webhook URL" DISCORD_WEBHOOK_URL
    prompt_text "Discord webhook display name" DISCORD_USERNAME
  else
    set_env DISCORD_WEBHOOK_URL ""
  fi

  prompt_port "Dashboard port" ENDLESS_SENTINEL_PORT
  set_env ENDLESS_SENTINEL_BIND 0.0.0.0
  printf '\nConfiguration saved.\n'
elif [[ "${CONFIGURE}" == "true" && "${INTERACTIVE}" == "false" ]]; then
  printf 'Non-interactive mode: retaining values from .env.\n'
fi

if [[ "${START}" != "true" ]]; then
  printf 'Configuration ready at %s\n' "${ENV_FILE}"
  exit 0
fi

printf 'Building and starting Endless Sentinel…\n'
"${COMPOSE_COMMAND[@]}" up --detach --build --remove-orphans

PORT="$(get_env ENDLESS_SENTINEL_PORT)"
PORT="${PORT:-8080}"
BIND="$(get_env ENDLESS_SENTINEL_BIND)"
BIND="${BIND:-127.0.0.1}"
DISPLAY_HOST="${BIND}"
if [[ "${DISPLAY_HOST}" == "0.0.0.0" || "${DISPLAY_HOST}" == "::" ]]; then
  DISPLAY_HOST="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  DISPLAY_HOST="${DISPLAY_HOST:-127.0.0.1}"
fi

healthy=false
for _attempt in {1..30}; do
  if "${DOCKER_COMMAND[@]}" inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' endless-sentinel 2>/dev/null | grep -q '^healthy$'; then
    healthy=true
    break
  fi
  sleep 2
done

if [[ "${healthy}" == "true" ]]; then
  printf '%s\n' \
    "" \
    "Endless Sentinel is healthy." \
    "Dashboard: http://${DISPLAY_HOST}:${PORT}" \
    "Configuration: ${ENV_FILE}" \
    "Logs: ${COMPOSE_COMMAND[*]} logs --follow endless-sentinel"
else
  printf 'Endless Sentinel started but has not reported healthy yet. Check: %s logs endless-sentinel\n' "${COMPOSE_COMMAND[*]}" >&2
  exit 1
fi
