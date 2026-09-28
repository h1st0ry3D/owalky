#!/usr/bin/env bash
# Run the test suite. No dependencies beyond the standard library.
#
#   ./run_tests.sh                 unit tests: codec, file layer, state, identity, socket
#   ./run_tests.sh --hardware      also drive the belt, needs a MAC
#   ./run_tests.sh --mac AA:...    the pad to use for --hardware
#
# --hardware moves the belt. Do not run it while walking on the pad, and expect
# to power-cycle the pad first: it only advertises for about 30 seconds.
set -euo pipefail
cd "$(dirname "$0")"

HARDWARE=0
MAC=""
VERBOSITY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --hardware) HARDWARE=1 ;;
    --mac) shift; MAC="${1:-}" ;;
    --mac=*) MAC="${1#--mac=}" ;;
    -v|--verbose) VERBOSITY="-v" ;;
    -h|--help) sed -n '2,10p' "$0" | cut -c3-; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

PYTHON="${PYTHON:-/usr/bin/python3}"

echo "=== unit tests ==="
"$PYTHON" -m unittest discover -s tests -t . $VERBOSITY

if [ "$HARDWARE" -eq 1 ]; then
  if [ -z "$MAC" ]; then
    echo
    echo "--hardware needs the pad's MAC address: ./run_tests.sh --hardware --mac AA:BB:CC:DD:EE:FF" >&2
    exit 2
  fi
  if ! "$PYTHON" -c "import bleak" >/dev/null 2>&1; then
    echo "python-bleak is missing: sudo pacman -S python-bleak" >&2
    exit 3
  fi
  echo
  echo "=== hardware flow on $MAC ==="
  echo "Clear the belt. Power-cycle the pad now, then it advertises for about 30 seconds."
  OWALKY_TEST_MAC="$MAC" "$PYTHON" -m unittest tests.test_hardware $VERBOSITY
fi
