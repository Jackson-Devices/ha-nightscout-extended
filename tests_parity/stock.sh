#!/usr/bin/env bash
# Start or stop a disposable stock Nightscout and MongoDB for the parity tests.
#
#   tests_parity/stock.sh start <profile>
#   tests_parity/stock.sh stop
#
# Profiles set the site settings the parity tests compare against. The images are pinned by digest; they
# are the same pins as the stock-contract harness (tests_contract/harness/versions.lock on the QA branches).
set -euo pipefail
umask 077

NS_IMAGE="docker.io/nightscout/cgm-remote-monitor:15.0.6@sha256:868f024f0aaf746aa9c6f639b4add7a78b07bcf4382f9955c5af61d413cad989"
MONGO_IMAGE="docker.io/library/mongo:5.0.32@sha256:5e3e87afd24d75e722884d777c5713d254f7e88ba65381b5d6484f75a21b73e3"
STATE_DIR="${PARITY_STATE_DIR:-${RUNNER_TEMP:-/tmp}/ns-parity}"
NET=parity-net
MONGO=parity-mongo
NS=parity-ns
PORT=1337
LABEL=org.nightscout.parity.disposable

stop() {
  local container
  for container in "$NS" "$MONGO"; do
    if docker container inspect "$container" >/dev/null 2>&1; then
      if [[ $(docker inspect --format '{{ index .Config.Labels "org.nightscout.parity.disposable" }}' "$container") != true ]]; then
        echo "Refusing to remove unrelated container $container" >&2
        return 1
      fi
      docker rm -fv "$container" >/dev/null
    fi
  done
  rm -f -- "$STATE_DIR/secret" "$STATE_DIR/profile"
  if docker network inspect "$NET" >/dev/null 2>&1; then
    if [[ $(docker network inspect --format '{{ index .Labels "org.nightscout.parity.disposable" }}' "$NET") != true ]]; then
      echo "Refusing to remove unrelated network $NET" >&2
      return 1
    fi
    docker network rm "$NET" >/dev/null
  fi
}

start() {
  local profile="$1"
  local -a site
  case "$profile" in
    mgdl-default)
      site=(-e DISPLAY_UNITS=mg/dl) ;;
    mgdl-custom)
      site=(-e DISPLAY_UNITS=mg/dl -e BG_HIGH=250 -e BG_TARGET_TOP=170 -e BG_TARGET_BOTTOM=75 -e BG_LOW=60
            -e ALARM_TIMEAGO_WARN_MINS=20 -e THEME=colors) ;;
    mmol-custom)
      site=(-e DISPLAY_UNITS=mmol -e BG_HIGH=14 -e BG_TARGET_TOP=10 -e BG_TARGET_BOTTOM=4 -e BG_LOW=3.5
            -e THEME=colorblindfriendly) ;;
    *)
      echo "unknown profile: $profile" >&2; exit 2 ;;
  esac

  stop
  mkdir -p "$STATE_DIR"
  local secret
  secret="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  printf '%s' "$secret" > "$STATE_DIR/secret"
  printf '%s' "$profile" > "$STATE_DIR/profile"

  docker network create --label "$LABEL=true" "$NET" >/dev/null
  docker run -d --name "$MONGO" --label "$LABEL=true" --network "$NET" "$MONGO_IMAGE" >/dev/null
  docker run -d --name "$NS" --label "$LABEL=true" --network "$NET" -p "127.0.0.1:$PORT:1337" \
    -e NODE_ENV=production -e TZ=Etc/UTC -e INSECURE_USE_HTTP=true \
    -e MONGO_CONNECTION="mongodb://$MONGO:27017/nightscout" \
    -e API_SECRET="$secret" -e AUTH_DEFAULT_ROLES=readable \
    -e CUSTOM_TITLE=Parity -e ENABLE="careportal iob cob ar2 basal" \
    "${site[@]}" "$NS_IMAGE" >/dev/null

  for _ in $(seq 1 90); do
    if curl -fsS "http://127.0.0.1:$PORT/api/v1/status.json" >/dev/null 2>&1; then
      echo "stock Nightscout ($profile) is up"
      return 0
    fi
    sleep 2
  done
  echo "stock Nightscout did not start" >&2
  docker inspect --format 'container {{.Name}}: {{.State.Status}} (exit {{.State.ExitCode}})' "$NS" "$MONGO" >&2
  exit 1
}

case "${1:-}" in
  start) start "${2:?profile}" ;;
  stop) stop ;;
  *) echo "usage: $0 start <profile> | stop" >&2; exit 2 ;;
esac
