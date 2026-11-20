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
# machine déjà connue de la LAPI sans identifiants dans le PVC : l'init ne doit pas rester bloqué
SCRIPT=$(printf '%s\n' "$INIT" | yq '.command[2]')
sh -n -c "$SCRIPT" || fail "script de l'init invalide"
grep -q 'if cscli lapi register' <<<"$SCRIPT" || fail "l'échec de cscli lapi register doit être rattrapé"
grep -q 'register --machine "\$USERNAME-' <<<"$SCRIPT" || fail "pas de ré-enregistrement sous un nom suffixé en dernier recours"
grep -q 'mv .* "\$CREDS"' <<<"$SCRIPT" || fail "l'init doit persister les identifiants dans le PVC dès l'enregistrement"
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

# --- agentKubernetesAcquisition ----------------------------------------------------------
KA=(--set crowdsec.agent.enabled=false --set agentPersistentCredentials.enabled=true --set agentKubernetesAcquisition.enabled=true
  --set 'agentKubernetesAcquisition.sources[0].namespace=ingress-controller' --set 'agentKubernetesAcquisition.sources[0].selector=app.kubernetes.io/name=traefik' --set 'agentKubernetesAcquisition.sources[0].program=traefik'
  --set 'agentKubernetesAcquisition.sources[1].namespace=ingress-controller' --set 'agentKubernetesAcquisition.sources[1].selector=app.kubernetes.io/name=ingress-nginx' --set 'agentKubernetesAcquisition.sources[1].program=nginx')
OUT=$(render "${KA[@]}")
AG=$(printf '%s\n' "$OUT" | yq 'select(.kind == "Deployment" and .metadata.name == "crowdsec-agent")')
[ -n "$AG" ] || fail "acquisition API : Deployment crowdsec-agent absent"
printf '%s\n' "$AG" | yq -e '.spec.replicas == 1 and .spec.strategy.type == "Recreate" and .spec.template.spec.serviceAccountName == "crowdsec-agent"' >/dev/null \
  || fail "acquisition API : Deployment attendu à 1 réplica, Recreate, avec le ServiceAccount de l'agent"
[ -z "$(printf '%s\n' "$OUT" | yq 'select(.kind == "DaemonSet" and .metadata.name == "crowdsec-agent")')" ] || fail "acquisition API : le DaemonSet agent ne doit plus être rendu"
[ -z "$(printf '%s\n' "$AG" | yq '.spec.template.spec.volumes[] | select(.hostPath)')" ] || fail "acquisition API : plus aucun hostPath attendu"
printf '%s\n' "$AG" | yq -e '.spec.template.spec.initContainers[0].command[2] | test("du PVC")' >/dev/null || fail "acquisition API : l'init doit garder la réutilisation des identifiants"
ACQ=$(printf '%s\n' "$OUT" | yq 'select(.kind == "ConfigMap" and .metadata.name == "acquis-configmap") | .data["acquis.yaml"]')
grep -q '/var/log/containers' <<<"$ACQ" && fail "acquisition API : aucune source fichier attendue"
[ "$(yq ea '[select(.source == "kubernetes" and .namespace == "ingress-controller")] | length' <<<"$ACQ")" = 2 ] || fail "acquisition API : deux sources kubernetes attendues dans acquis.yaml"
yq -e 'select(.selector == "app.kubernetes.io/name=traefik") | .labels.type == "traefik"' <<<"$ACQ" >/dev/null || fail "acquisition API : program doit devenir labels.type"
printf '%s\n' "$OUT" | yq -e 'select(.kind == "ServiceAccount" and .metadata.name == "crowdsec-agent")' >/dev/null || fail "acquisition API : ServiceAccount absent"
[ "$(printf '%s\n' "$OUT" | yq 'select(.kind == "Role" and (.metadata.name | test("agent-log-reader$"))) | .metadata.namespace')" = "ingress-controller" ] \
  || fail "acquisition API : un seul Role attendu, dans le namespace des pods suivis"
printf '%s\n' "$OUT" | yq -e 'select(.kind == "Role" and (.metadata.name | test("agent-log-reader$"))) | .rules[] | select(.resources[0] == "pods/log") | .verbs[0] == "get"' >/dev/null \
  || fail "acquisition API : le Role doit donner get sur pods/log"
printf '%s\n' "$OUT" | yq -e 'select(.kind == "RoleBinding" and (.metadata.name | test("agent-log-reader$"))) | .subjects[0] | (.name == "crowdsec-agent" and .namespace == "crowdsec")' >/dev/null \
  || fail "acquisition API : RoleBinding non lié au ServiceAccount de l'agent"
printf '%s\n' "$OUT" | yq -e 'select(.kind == "Job" and (.metadata.name | test("restart-agent$"))) | .spec.template.spec.containers[0].command[3] == "deployment/crowdsec-agent"' >/dev/null \
  || fail "acquisition API : le job de restart doit viser le Deployment"
render | yq -e 'select(.kind == "Job" and (.metadata.name | test("restart-agent$"))) | .spec.template.spec.containers[0].command[3] == "daemonset/crowdsec-agent"' >/dev/null \
  || fail "par défaut, le job de restart doit viser le DaemonSet"
[ -z "$(render | yq 'select(.metadata.name | test("agent-log-reader$"))')" ] || fail "par défaut, aucun RBAC d'acquisition API"
# gardes
if render --set agentKubernetesAcquisition.enabled=true >/dev/null 2>&1; then fail "acquisition API : doit exiger agentPersistentCredentials"; fi
if render --set crowdsec.agent.enabled=false --set agentPersistentCredentials.enabled=true --set agentKubernetesAcquisition.enabled=true >/dev/null 2>&1; then fail "acquisition API : doit exiger des sources"; fi
if render "${KA[@]}" --set 'agentKubernetesAcquisition.sources[1].selector=' >/dev/null 2>&1; then fail "acquisition API : doit exiger selector"; fi
if render "${KA[@]}" --set 'agentPersistentCredentials.acquisition[0].namespace=x' >/dev/null 2>&1; then fail "acquisition API : doit refuser les sources fichiers locales"; fi

# --- NetworkPolicy LAPI : namespace de l'ingress controller d'office ----------------------
NP_LAPI() { yq 'select(.kind == "NetworkPolicy" and (.metadata.name | test("-lapi-ingress$")))'; }
render | NP_LAPI | yq -e '.spec.ingress[0].from[] | select(.namespaceSelector.matchLabels["kubernetes.io/metadata.name"] == "ingress-controller")' >/dev/null \
  || fail "la NP LAPI doit autoriser le namespace ingress-controller par défaut"
render --set networkPolicy.lapi.ingressControllerNamespace=traefik --set 'networkPolicy.lapi.from[0].podSelector.matchLabels.app=bouncer' | NP_LAPI \
  | yq -e '(.spec.ingress[0].from | length) == 2 and (.spec.ingress[0].from[0].namespaceSelector.matchLabels["kubernetes.io/metadata.name"] == "traefik")' >/dev/null \
  || fail "ingressControllerNamespace surchargé + from doivent coexister"
[ -z "$(render --set networkPolicy.lapi.ingressControllerNamespace= | NP_LAPI)" ] \
  || fail "ingressControllerNamespace vide et from vide : pas de NP LAPI"

# --- WebUI : bouton « tout supprimer pour cette IP » masqué ----------------------------------
WEBUI() { yq 'select(.kind == "Deployment" and .metadata.name == "crowdsec-webui") | .spec.template.spec'; }
SPEC=$(render | WEBUI)
[ -n "$SPEC" ] || fail "Deployment crowdsec-webui absent"
SCRIPT=$(yq '.initContainers[] | select(.name == "hide-delete-all-for-ip") | .command[2]' <<<"$SPEC")
sh -n -c "$SCRIPT" || fail "WebUI : script de l'init invalide"
grep -q 'svg.lucide-shield-ban' <<<"$SCRIPT" || fail "WebUI : la règle CSS doit cibler l'icône lucide-shield-ban"
yq -e '.containers[0].volumeMounts[] | select(.name == "index-patch") | (.mountPath == "/app/dist/client/index.html" and .subPath == "index.html")' <<<"$SPEC" >/dev/null \
  || fail "WebUI : index.html patché non monté par-dessus l'original"
yq -e '.containers[0].volumeMounts[] | select(.name == "data" and .mountPath == "/app/data")' <<<"$SPEC" >/dev/null || fail "WebUI : le montage data doit rester"
yq -e '.volumes[] | select(.name == "index-patch") | has("emptyDir")' <<<"$SPEC" >/dev/null || fail "WebUI : volume index-patch absent"
# le sed doit réellement injecter la règle
TMP=$(mktemp -d); mkdir -p "$TMP/out"; printf '<html><head><title>x</title>\n</head><body></body></html>\n' > "$TMP/index.html"
sh -c "$(sed -e "s|/app/dist/client/index.html|$TMP/index.html|" -e "s|/index-patch/|$TMP/out/|" <<<"$SCRIPT")" || fail "WebUI : le script de l'init échoue"
grep -q '<style>button:has(> svg.lucide-shield-ban){display:none !important}</style></head>' "$TMP/out/index.html" || fail "WebUI : règle CSS non injectée"
rm -rf "$TMP"
# désactivable
SPEC=$(render --set webui.hideDeleteAllForIp=false | WEBUI)
[ "$(yq '.initContainers | length' <<<"$SPEC")" = 0 ] || fail "WebUI : pas d'init attendu quand hideDeleteAllForIp=false"
[ "$(yq '[.containers[0].volumeMounts[], .volumes[]] | map(select(.name == "index-patch")) | length' <<<"$SPEC")" = 0 ] || fail "WebUI : aucun volume index-patch attendu quand hideDeleteAllForIp=false"
echo "OK : tests de rendu passés"
