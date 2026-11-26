#!/bin/sh
# Stability checks of the running deployment: what a restart, a crash, a feed outage, an approval-policy change
# and a burst of submissions do to it. Run on the Docker host, from the repository root, after `up`:
#
#   TAFFEE_RUN=/absolute/host/path sh deploy/stability.sh
#
# It changes the running services (kills, restarts and recreates the registry, stops the feed) and leaves them
# running with auto approval. Each check prints PASS or FAIL; the script stops at the first FAIL.
# Client steps run in the registry image (deploy/client.py), so the host needs only docker.
set -eu
: "${TAFFEE_RUN:?set TAFFEE_RUN to the absolute host path of the deployment}"
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f deploy/compose.yaml"
IDS="$TAFFEE_RUN/stability"

pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; exit 1; }
client() { $COMPOSE --profile smoke run --rm --no-deps -T --entrypoint python smoke /app/deploy/client.py "$@"; }
smoke() { $COMPOSE --profile smoke run --rm --no-deps -T smoke > "$IDS/smoke.log" 2>&1 || { cat "$IDS/smoke.log"; return 1; }; }
health() { docker inspect -f '{{.State.Health.Status}}' cap-registry 2>/dev/null || echo absent; }
wait_healthy() {
  tries=0
  while [ "$(health)" != healthy ]; do
    tries=$((tries + 1)); [ $tries -le 90 ] || fail "cap-registry not healthy after 180s"; sleep 2
  done
}
# Sandbox containers the registry started, in any state: a container left in Created by a kill counts too.
orphans() { docker ps -aq --filter label=org.taffee.sandbox.owner | wc -l | tr -d ' '; }
wait_no_orphans() {
  tries=0
  while [ "$(orphans)" != 0 ]; do
    tries=$((tries + 1)); [ $tries -le 60 ] || fail "sandbox containers still running after 120s: $(orphans)"; sleep 2
  done
}

mkdir -p "$IDS"
wait_healthy
client audit > /dev/null || fail "the deployment was not consistent before the checks: $(client audit)"
smoke || fail "baseline: smoke passes"
pass "baseline: smoke passes"

# 1. A restart keeps everything and writes nothing new.
before=$(client head)
docker restart cap-registry > /dev/null
wait_healthy
after=$(client head)
[ "$before" = "$after" ] || fail "restart: the store head moved from $before to $after"
client audit || fail "restart: store head unchanged ($after), every row verifies"
pass "restart: store head unchanged ($after), every row verifies"

# 2. docker stop (SIGTERM) exits cleanly.
docker stop cap-registry > /dev/null
code=$(docker inspect -f '{{.State.ExitCode}}' cap-registry)
[ "$code" = 0 ] || fail "docker stop: exit code $code, not 0"
docker logs --tail 5 cap-registry 2>&1 | grep -q "registry stopped" || fail "docker stop: no clean shutdown in the log"
docker start cap-registry > /dev/null
wait_healthy
pass "docker stop: the service exits 0 after a clean shutdown and starts again"

# 3. A crash in the middle of admissions: kill -9 while submissions are being built and tested.
client submit crash 8 --until-busy > "$IDS/crash.ids"
docker kill cap-registry > /dev/null
docker start cap-registry > /dev/null
wait_healthy
client wait-all "$IDS/crash.ids" --state admitted || fail "crash: not every submission was admitted after the restart"
wait_no_orphans
client audit || fail "crash: all 8 submissions resumed and admitted, each committed once, no orphaned sandbox"
pass "crash: all 8 submissions resumed and admitted, each committed once, no orphaned sandbox"

# 4. A feed outage: resolve fails closed, then recovers by itself.
name=$(client name-of "$IDS/crash.ids")
client wait-status "$name" 200 30 > /dev/null || fail "feed: $name does not resolve before the outage"
docker stop cap-feed > /dev/null
client wait-status "$name" 503 90 || fail "feed outage: resolve answers 503 (fails closed)"
pass "feed outage: resolve answers 503 (fails closed)"
docker start cap-feed > /dev/null
client wait-status "$name" 200 90 || fail "feed outage: resolve recovers without a registry restart"
pass "feed outage: resolve recovers without a registry restart"

# 5. Manual approval.
TAFFEE_APPROVAL=manual $COMPOSE up -d registry > /dev/null 2>&1
wait_healthy
client manual || fail "manual approval works on the running service"
pass "manual approval works on the running service"
TAFFEE_APPROVAL=auto $COMPOSE up -d registry > /dev/null 2>&1
wait_healthy

# 6. A burst of 20 simultaneous submissions from 20 agents.
client burst 20 || fail "burst: 20 at once"
pass "burst: 20 at once"
wait_no_orphans

# 7. down and up: the data survives, the start-up import writes nothing.
before=$(client head)
$COMPOSE down > /dev/null 2>&1
$COMPOSE up -d > /dev/null 2>&1
wait_healthy
after=$(client head)
[ "$before" = "$after" ] || fail "down/up: the store head moved from $before to $after"
smoke || fail "down/up: data kept (head $after), smoke passes again"
pass "down/up: data kept (head $after), smoke passes again"

client audit || fail "final audit"

pass "final audit"
echo "stability: all checks passed"
