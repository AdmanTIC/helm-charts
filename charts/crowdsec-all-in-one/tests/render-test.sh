#!/usr/bin/env bash
# Tests de rendu (helm template) du chart. Usage : tests/render-test.sh
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "ÉCHEC : $*" >&2; exit 1; }
render() { helm template crowdsec . --namespace crowdsec "$@"; }

# --- agentPersistentCredentials -------------------------------------------------------
OUT=$(render --set crowdsec.agent.enabled=false --set agentPersistentCredentials.enabled=true)
DS=$(printf '%s\n' "$OUT" | yq 'select(.kind == "DaemonSet" and .metadata.name == "crowdsec-agent")')
[ -n "$DS" ] || fail "DaemonSet crowdsec-agent absent quand agentPersistentCredentials.enabled"
INIT=$(printf '%s\n' "$DS" | yq '.spec.template.spec.initContainers[0]')
printf '%s\n' "$INIT" | yq -e '.command[2] | test("local_api_credentials.yaml")' >/dev/null \
  || fail "l'init ne réutilise pas local_api_credentials.yaml du PVC"
printf '%s\n' "$INIT" | yq -e '.volumeMounts[] | select(.name == "crowdsec-agent-config" and .mountPath == "/etc/crowdsec_data")' >/dev/null \
  || fail "l'init ne monte pas le PVC de config"
printf '%s\n' "$INIT" | yq -e '.env[] | select(.name == "USERNAME") | .value == "crowdsec-agent"' >/dev/null \
  || fail "nom de machine non stable (attendu crowdsec-agent)"
printf '%s\n' "$OUT" | yq -e 'select(.kind == "ConfigMap" and .metadata.name == "acquis-configmap") | .data["acquis.yaml"] | test("nginx")' >/dev/null \
  || fail "acquis-configmap absent ou vide"
printf '%s\n' "$OUT" | yq -e 'select(.kind == "PersistentVolumeClaim" and .metadata.name == "crowdsec-agent-config-pvc")' >/dev/null \
  || fail "PVC crowdsec-agent-config-pvc absent"
# machineName surchargé
render --set crowdsec.agent.enabled=false --set agentPersistentCredentials.enabled=true --set agentPersistentCredentials.machineName=bkp \
  | yq -e 'select(.kind == "DaemonSet") | .spec.template.spec.initContainers[0].env[] | select(.name == "USERNAME") | .value == "bkp"' >/dev/null \
  || fail "machineName non pris en compte"
# désactivé par défaut : rien de nouveau rendu, agent upstream intact
DEFAULT_CMD=$(render | yq 'select(.kind == "DaemonSet" and .metadata.name == "crowdsec-agent") | .spec.template.spec.initContainers[0].command[2]')
case "$DEFAULT_CMD" in
  *"du PVC"*|"") fail "par défaut, le DaemonSet upstream doit être rendu tel quel" ;;
esac
# garde : les deux agents en même temps = refus
if render --set agentPersistentCredentials.enabled=true >/dev/null 2>&1; then fail "doit refuser agentPersistentCredentials sans crowdsec.agent.enabled=false"; fi
echo "OK : tests de rendu passés"
