#!/usr/bin/env bash
# Run the complete development gate against a private, disposable PostgreSQL 18 cluster.
set -Eeuo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

mode="check"
install_packages=true
for argument in "$@"; do
  case "$argument" in
    --setup-only) mode="setup" ;;
    --offline) mode="offline" ;;
    --skip-apt) install_packages=false ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done

python_version="$(< .python-version)"
node_version="$(< .node-version)"
if [[ ! "$python_version" =~ ^3\.14$ || ! "$node_version" =~ ^24\.[0-9]+\.[0-9]+$ ]]; then
  echo "Unsupported Python or Node version pin" >&2
  exit 1
fi

if "$install_packages"; then
  postgres_package_present=false
  if dpkg-query -W -f='${Status}' postgresql-18 2>/dev/null | grep -q 'install ok installed'; then
    postgres_package_present=true
  fi
  missing_packages=()
  for package in postgresql-18 python3.14-venv ca-certificates curl; do
    if ! dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
      missing_packages+=("$package")
    fi
  done
  if (( ${#missing_packages[@]} )); then
    sudo apt-get update
    sudo apt-get install -y "${missing_packages[@]}"
  fi
  # Ubuntu may auto-start a default cluster during its first package install.
  # Stop only the cluster created by this installation; never touch a prior one.
  if ! "$postgres_package_present" && pg_lsclusters -h 2>/dev/null \
    | grep -Eq '^18[[:space:]]+main[[:space:]]+[0-9]+[[:space:]]+online[[:space:]]'; then
    sudo pg_ctlcluster 18 main stop
  fi
fi

for program in "python$python_version" curl sha256sum tar; do
  if ! command -v "$program" >/dev/null 2>&1; then
    echo "Missing $program; install prerequisites or rerun without --skip-apt" >&2
    exit 1
  fi
done

pg_bin="/usr/lib/postgresql/18/bin"
for program in initdb pg_ctl createdb; do
  if [[ ! -x "$pg_bin/$program" ]]; then
    echo "Missing PostgreSQL 18 tool: $pg_bin/$program" >&2
    exit 1
  fi
done

cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/primarysignal-dev"
mkdir -p "$cache_dir"
chmod 700 "$cache_dir"

uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$repo_dir/.venv/bin/uv" ]]; then
  uv_bin="$repo_dir/.venv/bin/uv"
fi
if [[ -z "$uv_bin" ]]; then
  uv_bootstrap="$cache_dir/uv-bootstrap"
  "python$python_version" -m venv "$uv_bootstrap"
  "$uv_bootstrap/bin/pip" install --disable-pip-version-check --no-input 'uv==0.12.21'
  uv_bin="$uv_bootstrap/bin/uv"
fi
# uv may currently live in .venv, which sync is free to replace.
if [[ "$uv_bin" == "$repo_dir/.venv/bin/uv" ]]; then
  install -m 755 "$uv_bin" "$cache_dir/uv"
  uv_bin="$cache_dir/uv"
fi
uv_version="$("$uv_bin" --version)"
if [[ "$uv_version" =~ ^uv\ 0\.12\.([0-9]+)(\ |$) ]]; then
  uv_patch="${BASH_REMATCH[1]}"
else
  uv_patch=0
fi
if (( uv_patch < 21 )); then
  echo "uv >=0.12.21,<0.13 is required; found $uv_version" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64) node_arch=x64 ;;
  aarch64) node_arch=arm64 ;;
  *) echo "Unsupported Node architecture: $(uname -m)" >&2; exit 1 ;;
esac
node_name="node-v${node_version}-linux-${node_arch}"
node_home="$cache_dir/$node_name"
if [[ ! -x "$node_home/bin/node" ]]; then
  download_dir="$(mktemp -d "${TMPDIR:-/tmp}/primarysignal-node.XXXXXXXX")"
  trap 'rm -rf -- "$download_dir"' EXIT
  node_base="https://nodejs.org/dist/v${node_version}"
  curl --fail --silent --show-error --location "$node_base/SHASUMS256.txt" \
    --output "$download_dir/SHASUMS256.txt"
  curl --fail --silent --show-error --location "$node_base/${node_name}.tar.xz" \
    --output "$download_dir/${node_name}.tar.xz"
  (cd "$download_dir" && grep -E "^[a-f0-9]{64}  ${node_name}\\.tar\\.xz$" SHASUMS256.txt \
    | sha256sum --check --status)
  tar -xJf "$download_dir/${node_name}.tar.xz" -C "$download_dir"
  mv "$download_dir/$node_name" "$node_home"
  rm -rf -- "$download_dir"
  trap - EXIT
fi
export PATH="$node_home/bin:$repo_dir/.venv/bin:$PATH"
if [[ "$(node --version)" != "v$node_version" ]]; then
  echo "Node version does not match .node-version" >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "The pinned Node distribution does not contain npm" >&2
  exit 1
fi

"$uv_bin" sync --locked --all-groups --python "$python_version"
npm ci --ignore-scripts
if [[ "$mode" == setup ]]; then
  echo "Development tools installed. Run scripts/dev_test_env.sh to test."
  exit 0
fi

# Keep the Unix socket path short enough for PostgreSQL on every supported VM.
cluster_dir="$(mktemp -d /tmp/primarysignal-pg.XXXXXXXX)"
chmod 700 "$cluster_dir"
mkdir -m 700 "$cluster_dir/socket"
cluster_started=false
cleanup() {
  local result=$?
  trap - EXIT INT TERM
  if "$cluster_started"; then
    "$pg_bin/pg_ctl" -D "$cluster_dir/data" -m immediate -w stop >/dev/null || true
  fi
  rm -rf -- "$cluster_dir"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$pg_bin/initdb" -D "$cluster_dir/data" -U ps_dev_admin -A trust --no-instructions \
  >/dev/null
# The socket directory is owner-only, and TCP is disabled for this cluster.
"$pg_bin/pg_ctl" -D "$cluster_dir/data" -l "$cluster_dir/server.log" \
  -o "-c listen_addresses='' -c unix_socket_directories=$cluster_dir/socket" \
  -w start >/dev/null
cluster_started=true
"$pg_bin/createdb" -h "$cluster_dir/socket" -U ps_dev_admin primary_signal_test

database_base="postgresql+psycopg://"
database_host="/primary_signal_test?host=$cluster_dir/socket"
export PRIMARY_SIGNAL_TEST_DATABASE_URL="${database_base}ps_dev_admin@${database_host}"
export PRIMARY_SIGNAL_TEST_DATABASE_EXPECTED_ROLE=ps_dev_admin
export PRIMARY_SIGNAL_TEST_SCHEDULER_DATABASE_URL="${database_base}scheduler_test@${database_host}"
export PRIMARY_SIGNAL_TEST_PROCESSOR_DATABASE_URL="${database_base}processor_test@${database_host}"
export PRIMARY_SIGNAL_TEST_INVENTORY_DATABASE_URL="${database_base}inventory_test@${database_host}"
export PRIMARY_SIGNAL_TEST_PUBLIC_DATABASE_URL="${database_base}public_test@${database_host}"
export PRIMARY_SIGNAL_TEST_HEALTH_DATABASE_URL="${database_base}health_test@${database_host}"
export PRIMARY_SIGNAL_TEST_EDITORIAL_DATABASE_URL="${database_base}editorial_test@${database_host}"
export PRIMARY_SIGNAL_TEST_ADMIN_SESSION_DATABASE_URL="${database_base}admin_session_test@${database_host}"
export PRIMARY_SIGNAL_TEST_PUBLICATION_DECISION_DATABASE_URL="${database_base}publication_decision_test@${database_host}"
export PRIMARY_SIGNAL_REQUIRE_RESTRICTED_ROLE_TESTS=true
# This cluster is created for this gate and removed when it exits.
export PRIMARY_SIGNAL_TEST_DATABASE_DISPOSABLE=true

python scripts/bootstrap_test_database_roles.py
PRIMARY_SIGNAL_DATABASE_URL="$PRIMARY_SIGNAL_TEST_DATABASE_URL" \
  PRIMARY_SIGNAL_DATABASE_EXPECTED_ROLE=ps_dev_admin primary-signal-migrate

if [[ "$mode" == offline ]]; then
  poe check-offline
else
  poe check
fi
echo "Development gate passed with disposable PostgreSQL 18."
