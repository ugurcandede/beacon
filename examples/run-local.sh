#!/bin/bash
# Self-update end to end on this Mac, nothing published: builds both demo apps
# at 1.0.0 and 1.1.0, serves 1.1.0 from a local HTTP server with a manifest
# signed by a throwaway key, installs 1.0.0 into <work>/Applications and opens
# it with auto_update on. Each app should replace itself with 1.1.0 and relaunch.
#
# INSTALL=brew does it the Homebrew way instead: 1.0.0 is installed from a
# throwaway local tap (beacon-test/local) into <work>/Applications, the casks
# are bumped to 1.1.0, and each app should update itself with `brew upgrade`.
# The tap and the casks are removed at the end; the real taps are not touched.
#
#   examples/run-local.sh <work-dir> [python]
#
# `python` must have beacon (pip install -e "python[build]") and PySide6.
# Both demos are built with python -m beacon.build from their beacon.toml.
# BEACON_ENDPOINT points analytics at a Worker (default: wrangler dev's port).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=${1:?usage: run-local.sh <work-dir> [python]}
PY=${2:-python3}
PORT=8765
ENDPOINT=${BEACON_ENDPOINT:-http://127.0.0.1:8787/v1/events}

rm -rf "$WORK"
mkdir -p "$WORK"/{build,serve/py,serve/swift,Applications}
# Start from a fresh install: no update log, analytics queue or install id.

KEYS=$("$PY" -m beacon.release keygen)
export BEACON_SIGNING_KEY=$(awk '/^private/ {print $NF}' <<<"$KEYS")
PUBLIC=$(awk '/^public/ {print $NF}' <<<"$KEYS")

config() { # version base_url cask -- the demo keeps its data under $WORK/data/<cask>, not in ~/Library
  printf '{"version": "%s", "public_key": "%s", "base_url": "%s", "endpoint": "%s", "auto_update": true, "cask": %s, "data_dir": "%s"}\n' \
    "$1" "$PUBLIC" "$2" "$ENDPOINT" "$([ "${INSTALL:-}" = brew ] && echo "\"$3\"" || echo null)" "$WORK/data/$3"
}

unpack() { # zip dir -> path of the .app inside
  ditto -x -k "$1" "$2" && ls -d "$2"/*.app
}

build_py() { # version -> path of the .app
  local out="$WORK/build/py-$1"
  mkdir -p "$out"
  config "$1" "http://127.0.0.1:$PORT/py/" beacon-demo-py >"$ROOT/examples/demo-python/demo_config.json"
  # The beacon source goes on the path explicitly: Nuitka does not see through
  # an editable (pip install -e) install.
  PYTHONPATH="$ROOT/python" "$PY" -m beacon.build --version "$1" --config "$ROOT/examples/demo-python/beacon.toml" \
    --out "$out/dist" >"$out/build.log" 2>&1 || { tail -30 "$out/build.log" >&2; exit 1; }
  unpack "$out/dist/BeaconDemoPy-macos-$(uname -m | sed 's/x86_64/x64/').zip" "$out/app"
}

build_swift() { # version -> path of the .app
  local out="$WORK/build/swift-$1"
  mkdir -p "$out"
  config "$1" "http://127.0.0.1:$PORT/swift/" beacon-demo-swift >"$ROOT/examples/demo-swift/demo_config.json"
  PYTHONPATH="$ROOT/python" "$PY" -m beacon.build --version "$1" --config "$ROOT/examples/demo-swift/beacon.toml" \
    --out "$out/dist" >"$out/build.log" 2>&1 || { tail -30 "$out/build.log" >&2; exit 1; }
  unpack "$out/dist/BeaconDemoSwift-macos.zip" "$out/app"
}

publish() { # app dir-under-serve
  local zip="$WORK/serve/$2/$(basename "$1" .app)-1.1.0.zip"
  ditto -c -k --keepParent "$1" "$zip"
  "$PY" -m beacon.release manifest --repo ugurcandede/beacon-demo --tag v1.1.0 --out "$WORK/serve/$2" \
    --base-url "http://127.0.0.1:$PORT/$2/" "macos-$(uname -m | sed 's/x86_64/x64/')=$zip" 2>/dev/null
}

echo "building..."
PY_OLD=$(build_py 1.0.0); PY_NEW=$(build_py 1.1.0)
SWIFT_OLD=$(build_swift 1.0.0); SWIFT_NEW=$(build_swift 1.1.0)
publish "$PY_NEW" py
publish "$SWIFT_NEW" swift

"$PY" -m http.server "$PORT" --bind 127.0.0.1 --directory "$WORK/serve" >"$WORK/server.log" 2>&1 &
SERVER=$!
# Quit the demos with the server, or they show a failed check against it.
trap 'pkill -f "$WORK/Applications/BeaconDemo" 2>/dev/null; kill $SERVER 2>/dev/null' EXIT
sleep 1

TAP=beacon-test/local
cask() { # name app version -- written into the throwaway tap
  local zip="$WORK/serve/$4/$2-$3.zip"
  [ -f "$zip" ] || ditto -c -k --keepParent "$5" "$zip"
  cat >"$(brew --repository)/Library/Taps/beacon-test/homebrew-local/Casks/$1.rb" <<CASK
cask "$1" do
  version "$3"
  sha256 "$(shasum -a 256 "$zip" | awk '{print $1}')"
  url "http://127.0.0.1:$PORT/$4/$2-#{version}.zip"
  name "$2"
  homepage "https://github.com/ugurcandede/beacon"
  app "$2.app"
  auto_updates true   # as the real casks should be: brew leaves the update to the app
  postflight do
    system_command "/usr/bin/xattr", args: ["-cr", "#{appdir}/$2.app"]
  end
end
CASK
}

if [ "${INSTALL:-}" = brew ]; then
  trap 'pkill -f "$WORK/Applications/BeaconDemo" 2>/dev/null; kill $SERVER 2>/dev/null
        brew uninstall --cask --force beacon-demo-py beacon-demo-swift >/dev/null 2>&1; brew untap "$TAP" >/dev/null 2>&1' EXIT
  brew untap "$TAP" >/dev/null 2>&1 || true
  brew tap-new --no-git "$TAP" >/dev/null
  mkdir -p "$(brew --repository)/Library/Taps/beacon-test/homebrew-local/Casks"
  cask beacon-demo-py BeaconDemoPy 1.0.0 py "$PY_OLD"
  cask beacon-demo-swift BeaconDemoSwift 1.0.0 swift "$SWIFT_OLD"
  HOMEBREW_NO_AUTO_UPDATE=1 brew install --cask --appdir="$WORK/Applications" "$TAP/beacon-demo-py" "$TAP/beacon-demo-swift" >"$WORK/brew-install.log" 2>&1
  cask beacon-demo-py BeaconDemoPy 1.1.0 py "$PY_NEW"
  cask beacon-demo-swift BeaconDemoSwift 1.1.0 swift "$SWIFT_NEW"
else
  ditto "$PY_OLD" "$WORK/Applications/BeaconDemoPy.app"
  ditto "$SWIFT_OLD" "$WORK/Applications/BeaconDemoSwift.app"
fi
echo "opening 1.0.0 builds..."
open "$WORK/Applications/BeaconDemoPy.app"
open "$WORK/Applications/BeaconDemoSwift.app"

result=0
for app in BeaconDemoPy BeaconDemoSwift; do
  bundle="$WORK/Applications/$app.app"
  for _ in $(seq 1 180); do  # brew update + upgrade can take a while
    grep -rqs --include=demo_config.json '"version": "1.1.0"' "$bundle/Contents" && pgrep -f "$bundle/Contents/MacOS" >/dev/null && break
    sleep 1
  done
  if grep -rqs --include=demo_config.json '"version": "1.1.0"' "$bundle/Contents" && pgrep -f "$bundle/Contents/MacOS" >/dev/null; then
    echo "PASS $app: 1.1.0 installed and running"
  else
    echo "FAIL $app"; result=1
  fi
done
echo "--- update logs"
cat "$WORK/data/beacon-demo-py/update.log" 2>/dev/null
cat "$WORK/data/beacon-demo-swift/update.log" 2>/dev/null
if [ -t 0 ]; then
  read -r -p "Demos are running against the local server; press Enter to quit them. "
fi
exit $result
