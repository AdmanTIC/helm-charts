# =============================================================================
#  Image WAF avec inspection des réponses (phase 4)
# =============================================================================
#  L'image amont `ghcr.io/coreruleset/coraza-crs:*-caddy-*` est construite avec
#
#      xcaddy build --with github.com/corazawaf/coraza-caddy
#
#  sans suffixe de version. Go résout donc le module **v1** : coraza-caddy
#  v1.2.2 (janvier 2023), adossé à un coraza v3 pré-release. Cette version
#  n'appelle jamais ProcessResponseBody() — seul streamRecorder.WriteHeader()
#  déclenche ProcessResponseHeaders(). Conséquences mesurées :
#
#    - la phase 4 ne s'exécute pas, RESPONSE_BODY reste vide ;
#    - toutes les règles RESPONSE-95x du CRS sont inertes ;
#    - les entrées d'audit "transaction" ne sont jamais produites.
#
#  C'est-à-dire exactement les deux gains qui justifient le montage sandwich
#  face à ForwardAuth ou au plugin WASM de Traefik.
#
#  Ce Dockerfile ne remplace QUE le binaire Caddy, en le reconstruisant avec le
#  module v2. Tout le reste — CRS, crs-setup.conf, entrypoint, templates,
#  arborescence /opt/coraza, UID 1000 — vient de l'image amont, inchangé : le
#  chart et ses procédures de diagnostic restent valables tels quels.
#
#  Vérifié en local sur BASE=4.28.0-caddy-alpine-202607180107 :
#    coraza-caddy v2.5.0 / coraza v3.7.0, phase 4 exécutée, RESPONSE_BODY lu,
#    entrées d'audit "transaction" présentes.
#
#  ⚠️ Exige `CORAZA_DEFAULT_PHASE1_ACTION` / `CORAZA_DEFAULT_PHASE2_ACTION` sans
#     `tag:` — les coraza récents refusent les actions de métadonnée dans
#     SecDefaultAction et le conteneur ne démarre pas. Déjà posé dans
#     values.yaml sur le sidecar.
#
#  Construction :
#    docker build -t <votre-registry>/coraza-crs-v2:4.28.0-<date> charts/traefik-coraza/image
#    docker push  <votre-registry>/coraza-crs-v2:4.28.0-<date>
#  puis reporter ce tag aux TROIS emplacements de values.yaml (initContainer,
#  sidecar, reloader) et passer `coraza.imageSupportsPhase4` à true.
#
#  Pourquoi pas d'image officielle : `docker-bake.hcl` amont épingle pourtant
#  bien coraza-caddy v2.5.0 et le passe en `CORAZA_VERSION`, mais
#  `caddy/Dockerfile` ne déclare aucun `ARG CORAZA_VERSION` et construit le
#  chemin de module v1. L'argument est ignoré, toutes les images publiées
#  embarquent v1.2.2, et aucun tag plus récent n'y changera rien. Correction
#  amont, deux lignes :
#      ARG CORAZA_VERSION
#      RUN xcaddy build --with github.com/corazawaf/coraza-caddy/v2@${CORAZA_VERSION}
#  Une fois publiée : reprendre le tag officiel et supprimer ce répertoire.
# =============================================================================

ARG BASE=ghcr.io/coreruleset/coraza-crs:4.28.0-caddy-alpine-202607180107

# La version de Caddy doit correspondre à celle de l'image de base, sinon le
# Caddyfile livré peut ne plus être adapté par le binaire.
ARG CADDY_VERSION=2.11.3

FROM caddy:${CADDY_VERSION}-builder AS builder
# Le `/v2` est TOUTE la correction.
RUN xcaddy build --with github.com/corazawaf/coraza-caddy/v2

FROM ${BASE}
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
