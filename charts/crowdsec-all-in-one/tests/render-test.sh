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
# acquisition locale avec containerName (glob sur le container, défaut *)
ACQ=$(render --set crowdsec.agent.enabled=false --set agentPersistentCredentials.enabled=true \
  --set 'agentPersistentCredentials.acquisition[0].namespace=ingress-controller' \
  --set 'agentPersistentCredentials.acquisition[0].podName=traefik-*' \
  --set 'agentPersistentCredentials.acquisition[0].containerName=traefik' \
  --set 'agentPersistentCredentials.acquisition[0].program=traefik' \
  --set 'agentPersistentCredentials.acquisition[1].namespace=ingress-controller' \
  --set 'agentPersistentCredentials.acquisition[1].podName=nginx-*' \
  --set 'agentPersistentCredentials.acquisition[1].program=nginx' \
  | yq 'select(.kind == "ConfigMap" and .metadata.name == "acquis-configmap") | .data["acquis.yaml"]')
grep -qF '/var/log/containers/traefik-*_ingress-controller_traefik-*.log' <<<"$ACQ" || fail "containerName non pris en compte dans le glob"
grep -qF '/var/log/containers/nginx-*_ingress-controller_*-*.log' <<<"$ACQ" || fail "containerName par défaut doit être *"
grep -qF 'nginx-ingress-ingress-nginx-controller' <<<"$ACQ" && fail "agentPersistentCredentials.acquisition doit remplacer crowdsec.agent.acquisition"

# --- NetworkPolicy LAPI : namespace de l'ingress controller d'office ----------------------
NP_LAPI() { yq 'select(.kind == "NetworkPolicy" and (.metadata.name | test("-lapi-ingress$")))'; }
render | NP_LAPI | yq -e '.spec.ingress[0].from[] | select(.namespaceSelector.matchLabels["kubernetes.io/metadata.name"] == "ingress-controller")' >/dev/null \
  || fail "la NP LAPI doit autoriser le namespace ingress-controller par défaut"
render --set networkPolicy.lapi.ingressControllerNamespace=traefik --set 'networkPolicy.lapi.from[0].podSelector.matchLabels.app=bouncer' | NP_LAPI \
  | yq -e '(.spec.ingress[0].from | length) == 2 and (.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"] == "traefik")' >/dev/null \
  || fail "ingressControllerNamespace surchargé + from doivent coexister"
[ -z "$(render --set networkPolicy.lapi.ingressControllerNamespace= | NP_LAPI)" ] \
  || fail "ingressControllerNamespace vide et from vide : pas de NP LAPI"
echo "OK : tests de rendu passés"
