#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d /tmp/endless-sentinel-tests.XXXXXX)"

cleanup() {
  case "${TEST_ROOT}" in
    /tmp/endless-sentinel-tests.*) rm -rf -- "${TEST_ROOT}" ;;
  esac
}
trap cleanup EXIT

copy_minimum_project() {
  local destination="$1"
  mkdir -p "${destination}"
  cp "${SOURCE_ROOT}/setup.sh" \
    "${SOURCE_ROOT}/.env.example" \
    "${SOURCE_ROOT}/app.py" \
    "${SOURCE_ROOT}/Dockerfile" \
    "${SOURCE_ROOT}/docker-compose.yml" \
    "${destination}/"
}

assert_env_ready() {
  local project="$1"
  [[ -f "${project}/.env" ]]
  [[ "$(stat -c '%a' "${project}/.env")" == "600" ]]
  grep -q '^FLASK_SECRET_KEY=..*' "${project}/.env"
}

test_local_no_start() {
  local project="${TEST_ROOT}/local"
  copy_minimum_project "${project}"
  (
    cd "${project}"
    ./setup.sh --non-interactive --no-start >/dev/null
  )
  assert_env_ready "${project}"
}

test_piped_clone() {
  local origin="${TEST_ROOT}/origin" install_dir="${TEST_ROOT}/installed"
  copy_minimum_project "${origin}"
  git -C "${origin}" init --initial-branch=main -q
  git -C "${origin}" add .
  git -C "${origin}" -c user.name=Verifier -c user.email=verifier@example.invalid commit -qm initial

  ENDLESS_SENTINEL_REPOSITORY_URL="file://${origin}" \
    ENDLESS_SENTINEL_INSTALL_DIR="${install_dir}" \
    bash -s -- --non-interactive --no-start < "${SOURCE_ROOT}/setup.sh" >/dev/null
  assert_env_ready "${install_dir}"
}

write_docker_mocks() {
  local fake_bin="$1" test_log="$2"
  mkdir -p "${fake_bin}"

  cat > "${fake_bin}/apt-get" <<'EOF'
#!/usr/bin/env bash
printf 'apt-get %s\n' "$*" >> "${ENDLESS_SENTINEL_TEST_LOG}"
if [[ "$*" == *docker-ce* ]]; then
  cat > "${ENDLESS_SENTINEL_TEST_FAKE_BIN}/docker" <<'DOCKER'
#!/usr/bin/env bash
case "${1:-}" in
  info) exit 0 ;;
  compose)
    if [[ "${2:-}" == "version" ]]; then exit 0; fi
    printf 'docker %s\n' "$*" >> "${ENDLESS_SENTINEL_TEST_LOG}"
    ;;
  inspect) printf 'healthy\n' ;;
esac
DOCKER
  chmod +x "${ENDLESS_SENTINEL_TEST_FAKE_BIN}/docker"
fi
EOF

  cat > "${fake_bin}/curl" <<'EOF'
#!/usr/bin/env bash
output=""
while (($#)); do
  if [[ "$1" == "-o" ]]; then output="$2"; shift 2; else shift; fi
done
[[ -n "${output}" ]] && printf 'test-signing-key\n' > "${output}"
EOF

  cat > "${fake_bin}/install" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "${fake_bin}/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "${fake_bin}/dpkg-query" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF

  chmod +x "${fake_bin}/apt-get" "${fake_bin}/curl" "${fake_bin}/install" \
    "${fake_bin}/systemctl" "${fake_bin}/dpkg-query"
  : > "${test_log}"
}

test_automatic_docker_install() {
  local project="${TEST_ROOT}/docker-host" fake_bin="${TEST_ROOT}/fake-bin" test_log="${TEST_ROOT}/docker-install.log"
  copy_minimum_project "${project}"
  write_docker_mocks "${fake_bin}" "${test_log}"

  (
    cd "${project}"
    PATH="${fake_bin}:/usr/bin:/bin" \
      ENDLESS_SENTINEL_FORCE_DOCKER_INSTALL=true \
      ENDLESS_SENTINEL_TEST_FAKE_BIN="${fake_bin}" \
      ENDLESS_SENTINEL_TEST_LOG="${test_log}" \
      ./setup.sh --non-interactive >/dev/null
  )

  grep -q 'docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin' "${test_log}"
  grep -q 'docker compose up --detach --build --remove-orphans' "${test_log}"
  assert_env_ready "${project}"
}

test_interactive_defaults() {
  command -v script >/dev/null 2>&1 || return
  local project="${TEST_ROOT}/interactive"
  copy_minimum_project "${project}"
  (
    cd "${project}"
    printf 'n\nn\ny\nn\n\n' | script -qefc './setup.sh --configure --no-start' /dev/null >/dev/null
  )
  grep -q '^PROXMOX_ENABLED=false$' "${project}/.env"
  grep -q '^K3S_ENABLED=false$' "${project}/.env"
  grep -q '^DOCKER_ENABLED=true$' "${project}/.env"
  grep -q '^DISCORD_WEBHOOK_URL=$' "${project}/.env"
  grep -q '^ENDLESS_SENTINEL_PORT=8080$' "${project}/.env"
}

test_local_no_start
test_piped_clone
test_automatic_docker_install
test_interactive_defaults
printf 'setup installer tests passed\n'
