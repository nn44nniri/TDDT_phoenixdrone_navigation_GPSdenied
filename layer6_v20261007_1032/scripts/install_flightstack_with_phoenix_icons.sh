#!/usr/bin/env bash
# Standard FlightStack installation followed by bundle-local presentation assets.
set -Eeuo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bundle_root="$(cd "$script_dir/../.." && pwd)"
base_dir="$PWD"
icons_only=0
check_only=0
forward=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-dir) base_dir="${2:?missing --base-dir}"; forward+=("$1" "$2"); shift 2 ;;
    --assets-dir) forward+=("$1" "${2:?missing --assets-dir}"); shift 2 ;;
    --icons-only) icons_only=1; shift ;;
    --check-icons) check_only=1; shift ;;
    -h|--help)
      echo 'Usage: install_flightstack_with_phoenix_icons.sh --base-dir LAYER6 [standard installer options] [--icons-only | --check-icons]'
      echo '--icons-only installs the visual patch on an existing stack without rebuild/reset.'
      echo '--check-icons verifies installed resources without writing.'
      exit 0 ;;
    *) forward+=("$1"); shift ;;
  esac
done
[[ "$icons_only" == 0 || "$check_only" == 0 ]] || { echo 'Choose one icons mode' >&2; exit 2; }
flightstack="$(cd "$base_dir" && pwd)/FlightStack"
native_python="$flightstack/ardupilot/venv-ardupilot-debian13/bin/python"
if [[ "$check_only" == 1 ]]; then
  exec "$native_python" "$bundle_root/presentation/phoenix_mavproxy_icons/install.py" --flightstack-dir "$flightstack" --check
fi
if [[ "$icons_only" == 0 ]]; then
  bash "$script_dir/install_flightstack_rev1.sh" "${forward[@]}"
else
  bash "$script_dir/install_flightstack_rev1.sh" --base-dir "$base_dir" --check-replay-map
fi
"$native_python" "$bundle_root/presentation/phoenix_mavproxy_icons/install.py" --flightstack-dir "$flightstack"
