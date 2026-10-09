#!/usr/bin/env bash
set -euo pipefail

project="primary-signal-preview-smoke-$$"
port="${PRIMARY_SIGNAL_PREVIEW_PORT:-18080}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose -f "$root/compose.preview.yaml" -p "$project")
response_dir="$(mktemp -d)"

cleanup() {
  "${compose[@]}" down --remove-orphans --rmi local >/dev/null
  rm -rf -- "$response_dir"
}
trap cleanup EXIT

PRIMARY_SIGNAL_PREVIEW_PORT="$port" "${compose[@]}" config --quiet
PRIMARY_SIGNAL_PREVIEW_PORT="$port" "${compose[@]}" up --build --detach --wait
"${compose[@]}" exec -T public python -c \
  "from importlib.resources import files; from alembic.config import Config; from alembic.script import ScriptDirectory; from primary_signal.entrypoints.migrate import migration_config_path; root = files('primary_signal'); assert root.joinpath('db/migrations/versions/20261009_18_retention_cleanup.py').is_file(); assert ScriptDirectory.from_config(Config(migration_config_path())).get_current_head() == '20261009_18'"
test "$("${compose[@]}" exec -T public id -u)" = 10001

curl --fail --silent --show-error "http://127.0.0.1:$port/__dev/preview" \
  -o "$response_dir/page.html"
grep -q 'Latest reporting' "$response_dir/page.html"
grep -q 'Identity service fix follows targeted exploitation reports' "$response_dir/page.html"
curl --fail --silent --show-error "http://127.0.0.1:$port/assets/public/public.css" \
  -o "$response_dir/public.css"
grep -q -- '--ps-brand-blue' "$response_dir/public.css"
printf 'Container preview, packaged migration, synthetic page, and CSS passed on loopback port %s.\n' "$port"
