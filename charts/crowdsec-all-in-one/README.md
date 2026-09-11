# crowdsec-all-in-one

Chart Helm umbrella qui package une stack **CrowdSec** complète, prête pour de
gros volumes de trafic, avec une interface d'administration et un bouncer
Kubernetes-natif optionnel pour Calico.

## Vue d'ensemble

```
                            ┌────────────────────────┐
                            │   WebUI (Deployment)   │ ◀── Ingress / HTTPRoute
                            └──────────┬─────────────┘
                                       │ HTTP
                                       ▼
  ┌─────────────┐               ┌─────────────┐         ┌───────────────┐         ┌────────────┐
  │   agent     │ ─── logs ───▶ │    LAPI     │ ──SQL──▶│   PgBouncer   │ ──SQL──▶│ PostgreSQL │
  │ (DaemonSet) │  + alertes    │ (Deployment)│         │ (transaction  │         │ (Deploy)   │
  └─────────────┘               └─────┬───────┘         │   pooling)    │         └────────────┘
                                      │ stream API      └───────────────┘
                                      ▼
                              ┌───────────────┐
                              │ calico-bouncer│ ── update ──▶ Calico GlobalNetworkPolicy
                              │ (Deployment)  │               + HostEndpoint(s)
                              └───────────────┘
```

### Pourquoi ce chart ?

Le chart CrowdSec officiel embarque une SQLite interne et n'inclut ni WebUI ni
bouncer. Pour des environnements de production qui :

- traitent un **gros volume de trafic** (plusieurs centaines/milliers de
  req/s) — la SQLite devient un goulot d'étranglement dès que le LAPI passe
  multi-replicas ou que le débit d'inserts dépasse quelques centaines/s,
- exigent une **base de données externe partagée** entre plusieurs LAPI (HA),
- veulent **piloter CrowdSec depuis une UI** (consulter alertes/décisions sans
  ouvrir un shell pour `cscli`),
- veulent **bloquer les IP au niveau réseau** (pas seulement L7) dans un
  cluster Kubernetes — typiquement parce que l'origine du trafic peut
  contourner le reverse proxy (NodePort, LoadBalancer…),

… ce chart fournit tout en un seul `helm install` :

| Composant                    | Source                             | Rôle                                                            |
|------------------------------|------------------------------------|-----------------------------------------------------------------|
| **CrowdSec** (LAPI + agent)  | sub-chart `crowdsec`               | Détection, alertes, gestion des décisions                       |
| **PostgreSQL**               | sub-chart `cloudpirates/postgres`  | Backend des décisions/alertes (remplace la SQLite par défaut)   |
| **PgBouncer**                | sub-chart `icoretech/pgbouncer`    | Pool de connexions (transaction mode) devant Postgres           |
| **WebUI**                    | image `theduffman85/crowdsec-web-ui` (templates locaux) | Interface graphique de consultation                |
| **calico-bouncer** (opt.)    | template + script Python embarqué  | Matérialise les bans dans une Calico `GlobalNetworkPolicy`      |
| **NetworkPolicy** (opt.)     | template local                     | Isolation du namespace + ouvertures contrôlées                  |
| **Jobs PostSync**            | templates locaux                   | Bootstrap (collections, register WebUI/bouncer dans le LAPI)    |

## Pré-requis

- **Kubernetes** ≥ 1.27 (testé sur 1.30+)
- **StorageClass** supportant `ReadWriteMany` (au moins 3 PVCs RWX :
  `lapi-config`, `agent-config`, `agent-data`)
- **Helm** ≥ 3.10
- Accès sortant HTTPS vers `hub.crowdsec.net` depuis les pods du namespace
  (cf. `networkPolicy.extraEgress` si besoin de relâcher)
- (Optionnel, pour le bouncer) Cluster avec **Calico** comme CNI
  (`crd.projectcalico.org/v1` disponible : `GlobalNetworkPolicy`,
  `HostEndpoint`)

## Installation

```bash
helm dep update charts/crowdsec-all-in-one
helm upgrade --install crowdsec charts/crowdsec-all-in-one \
  --namespace crowdsec --create-namespace
```

À la première install, le template `crowdsec-secrets.yaml` génère des mots de
passe aléatoires (Postgres, PgBouncer, WebUI) et les stocke dans le Secret
`crowdsec-secrets`. Les valeurs sont **préservées entre les `helm upgrade`**
grâce à un `lookup` côté template.

> **ArgoCD** : tous les hooks portent les annotations
> `argocd.argoproj.io/hook` + `sync-wave` cohérentes avec les hooks Helm.
> `crowdsec-secrets` et le Secret du bouncer sont annotés `Prune=false` +
> `helm.sh/resource-policy: keep` pour ne pas être détruits en cas de
> resync/uninstall accidentel.

## Values les plus importantes

### 1. Base de données (Postgres + PgBouncer)

```yaml
postgres:
  enabled: true                    # mettre false pour pointer vers un Postgres externe
  persistence:
    size: 5Gi                      # à adapter au volume de décisions/alertes
    accessModes: [ReadWriteOnce]
  resources:
    requests: { cpu: 100m, memory: 384Mi }
    limits:   { cpu: 1000m, memory: 512Mi }

pgbouncer:
  enabled: true
  config:
    pgbouncer:
      pool_mode: transaction       # adapté aux requêtes courtes du LAPI
      default_pool_size: 30        # connexions Postgres par DB+user
      max_client_conn: 800         # connexions logiques côté LAPI
```

Le LAPI parle **uniquement** à PgBouncer (cf. `crowdsec.lapi.env.DB_HOST =
crowdsec-pgbouncer`), jamais à Postgres en direct.

### 2. LAPI + agent

```yaml
crowdsec:
  container_runtime: containerd    # ou docker / cri-o
  lapi:
    replicas: 1                    # peut passer en HA → la DB est externe
  agent:
    acquisition:                   # quels logs collecter
      - namespace: ingress-controller
        podName: nginx-ingress-ingress-nginx-controller-*
        poll_without_inotify: true
        program: nginx
  config:
    config.yaml.local: |           # surcharge des valeurs upstream
      api:
        server:
          trusted_ips:             # IPs jamais bannies
            - 127.0.0.1
            - 10.0.0.0/8
    profiles.yaml: |               # mapping alerte → décision (ban / captcha)
      ...
```

### 3. WebUI — exposition

Trois modes d'exposition supportés, mutuellement exclusifs :

| Mode           | Quand l'utiliser                                                    |
|----------------|---------------------------------------------------------------------|
| `ingress`      | `nginx-ingress` (ou autre controller compatible Ingress standard)   |
| `httpRoute`    | Gateway API (Istio, Envoy Gateway, Traefik en mode Gateway API…)    |
| `ingressRoute` | Traefik configuré en mode CRD natif `traefik.io/v1alpha1`           |

```yaml
webui:
  # Mode 1 — Ingress nginx
  ingress:
    enabled: true
    className: nginx
    hosts:
      - host: crowdsec.example.com
        paths: [{ path: /, pathType: ImplementationSpecific }]
    tls:
      - secretName: crowdsec-tls
        hosts: [crowdsec.example.com]

  # Mode 2 — HTTPRoute Gateway API
  httpRoute:
    enabled: true
    parentRefs:
      - name: my-gateway
        namespace: gateway-system
    hostnames: [crowdsec.example.com]
    rules:
      - matches:
          - path: { type: PathPrefix, value: / }

  # Mode 3 — IngressRoute Traefik (CRD natif)
  ingressRoute:
    enabled: true
    entryPoints: [websecure]
    routes:
      - match: Host(`crowdsec.example.com`)
    tls:
      enabled: true
      secretName: crowdsec-tls         # ou : certResolver: letsencrypt

  persistence:
    enabled: true
    size: 1Gi                          # SQLite locale (sessions, préférences)
```

Le compte admin est créé automatiquement par le hook `register-job.yaml` (login
= `admin`, mot de passe dans le Secret `crowdsec-secrets`, clé
`webuiPassword`).

### 4. WebUI — authentification (login natif + SSO OIDC)

Depuis la version `2026.7.x`, l'image `theduffman85/crowdsec-web-ui` embarque
un vrai système d'auth : login/mot de passe + TOTP + passkeys + **OIDC SSO**.
Le chart expose le volet OIDC via `webui.sso.oidc.*` — SAML n'est **pas**
supporté par l'image.

Providers documentés côté image : Authentik, Authelia, Keycloak.

```yaml
webui:
  sso:
    enabled: true
    oidc:
      issuerUrl: "https://keycloak.example.com/realms/main"
      clientId: "crowdsec-webui"
      # Option A — inline (test uniquement, secret en clair dans values) :
      clientSecret: "s3cr3t"
      # Option B — Secret pré-existant (prod : sealed-secrets / ExternalSecrets) :
      # existingSecret: my-oidc-secret
      # existingSecretKey: clientSecret

      scope: "openid profile email"
      groupsClaim: groups

      adminGroups: ["crowdsec-admins", "sre"]
      readOnlyGroups: ["crowdsec-viewers"]

      # Rôle par défaut pour un user OIDC qui ne match aucun groupe.
      # deny (recommandé) | admin | read-only
      unmatchedRole: deny
```

**Ce que le chart produit** :

| Ressource                       | Activée si…                                                       |
|---------------------------------|-------------------------------------------------------------------|
| `Secret <fullname>-sso`         | `sso.enabled && !sso.oidc.existingSecret && sso.oidc.clientSecret` |
| Env `CONFIG_AUTH_OIDC_*` sur le pod | `sso.enabled`                                                 |

Les listes `adminGroups` / `readOnlyGroups` sont projetées en variables
indexées (`CONFIG_AUTH_OIDC_ADMIN_GROUPS_0`, `_1`, …) comme attendu par
l'image.

**Callback URL à déclarer côté IdP** : `https://<host>/api/auth/oidc/callback`
(cf. doc image pour la valeur exacte selon la version).

Le compte admin local reste créé automatiquement par le hook `register-job.yaml`
(cf. section 3) — utile en fallback si l'IdP est indisponible.

### 5. Collections / parsers / scenarios

Deux sources d'items possibles, complémentaires :

**a) Depuis le hub CrowdSec** — via `cscli` en hook PostSync (sync-wave 0),
idempotent :

```yaml
collectionsJob:
  enabled: true
  collections:
    - crowdsecurity/nginx
    - crowdsecurity/traefik
    - crowdsecurity/linux
  parsers: []
  postoverflows: []
```

**b) Custom (fournis par toi)** — passthrough vers le sub-chart : chaque map
`filename → contenu` devient un ConfigMap monté en `subPath` dans le DaemonSet
agent, sous `/etc/crowdsec/{scenarios,parsers/<stage>,postoverflows/<stage>}/`.
CrowdSec les charge en plus des items hub.

```yaml
crowdsec:
  config:
    # Scenarios custom — dossier /etc/crowdsec/scenarios/
    scenarios:
      my-admin-crawl.yaml: |
        type: leaky
        name: myorg/http-admin-crawl
        description: "Crawls suspects sur /admin"
        filter: "evt.Meta.log_type == 'http_access-log' && evt.Meta.http_path startsWith '/admin'"
        leakspeed: 10s
        capacity: 5
        groupby: evt.Meta.source_ip
        labels: { type: scan, remediation: true }

    # Parsers custom — organisés par stage (s00-raw / s01-parse / s02-enrich)
    parsers:
      s01-parse:
        my-app.yaml: |
          onsuccess: next_stage
          filter: "evt.Parsed.program == 'my-app'"
          nodes:
            - grok: { pattern: '%{IPORHOST:source_ip} - .*', apply_on: message }

    # Whitelists / enrichissements — stages s00-enrich / s01-whitelist
    postoverflows:
      s01-whitelist:
        office-ips.yaml: |
          name: myorg/office-whitelist
          description: "IPs bureau à ne jamais bannir"
          whitelist:
            reason: "office subnet"
            cidr: ["203.0.113.0/24"]
```

À chaque `helm upgrade`, le hook `restart-agent-job` (sync-wave 5) fait un
`kubectl rollout restart` du DaemonSet, donc les changements de scenarios
custom sont pris en compte automatiquement sans action manuelle.

Pour vérifier après upgrade :

```bash
kubectl exec -n <ns> ds/<release>-agent -- ls /etc/crowdsec/scenarios/
kubectl exec -n <ns> ds/<release>-agent -- cscli scenarios list | grep myorg
```

### 6. NetworkPolicy

Activée par défaut. Autorise par défaut intra-namespace + DNS + HTTPS sortant.
Toute autre exposition (ingress controller → WebUI/LAPI, bouncer externe →
LAPI, etc.) doit être déclarée dans `networkPolicy.extraIngress` /
`networkPolicy.extraEgress`.

## Bouncer Calico (`calicoBouncer`)

### Ce qu'il fait

Le bouncer est un Deployment qui :

1. **stream** l'API `/v1/decisions/stream` du LAPI (delta, pas de polling
   full),
2. maintient un état interne `decision-id → CIDR`,
3. reconcilie une **Calico `GlobalNetworkPolicy`** unique (par défaut
   `crowdsec-bans`) contenant un `Deny` par IP/range banni·e,
4. utilise un **`coordination.k8s.io/Lease`** pour l'élection de leader (un
   seul replica écrit la GNP à la fois, les autres sont en standby chaud).

Caractéristiques :

- **delta-based** : redémarrage → un re-stream `startup=true` ramène l'état
  complet, les IP expirées disparaissent, les nouvelles sont ajoutées,
- **gestion des suppressions** (champ `deleted` du stream),
- **backoff exponentiel** sur erreurs LAPI / API K8s,
- **types de décisions configurables** (`ban` par défaut, `captcha`
  optionnel — cf. plus bas),
- compatible **dataplane iptables ET eBPF** (réglages dans
  `calicoBouncer.policy.preDNAT` / `applyOnForward`),
- **2 replicas** par défaut avec anti-affinity → bascule en quelques secondes
  si un nœud meurt.

### Pré-requis spécifiques

1. **Calico installé** en tant que CNI (typha + felix). Manifests
   `tigera-operator` ou manifests legacy, peu importe — il faut que les CRD
   `globalnetworkpolicies.crd.projectcalico.org` et
   `hostendpoints.crd.projectcalico.org` existent.

2. **Au moins un `HostEndpoint`** matchant le `selector` de la GNP. Sans HE,
   la GNP est inerte (Calico ne sait pas où l'appliquer). Trois options
   couvertes par le chart :

   - **AutoHostEndpoints** (recommandé pour un cluster homogène) : laisser
     Calico les créer pour tous les nœuds. **Hors du chart**, patcher la
     `KubeControllersConfiguration` :
     ```bash
     kubectl patch kubecontrollersconfiguration default --type=merge -p \
       '{"spec":{"controllers":{"node":{"hostEndpoint":{"autoCreate":"Enabled"}}}}}'
     ```
     Puis garder le selector par défaut :
     ```yaml
     calicoBouncer:
       policy:
         selector: "has(kubernetes.io/hostname)"
     ```

   - **HostEndpoints manuels** : créer / labelliser les HE soi-même
     (`crowdsec-protected: "true"` par défaut) et adapter le selector.

   - **HostEndpoints templatés par ce chart** : activer
     `calicoBouncer.hostEndpoints.manage = true` et lister les entries.

3. **`pgcrypto`** disponible dans Postgres (déjà géré par `postgres.initdb`
   de ce chart : `CREATE EXTENSION IF NOT EXISTS pgcrypto;`). Utilisé par le
   `register-job` pour hasher l'API key en SHA-512 hex (comme `cscli bouncers
   add`).

4. **PyPI accessible** depuis les pods bouncer (l'image `python:3.12-alpine`
   par défaut fait un `pip install requests kubernetes` au démarrage). Pour
   un cluster air-gapped, builder une image custom qui inclut déjà les libs
   et la pointer via `calicoBouncer.image.repository` / `tag`.

### Activer le bouncer

Minimum vital :

```yaml
calicoBouncer:
  enabled: true
```

Avec ça vous obtenez :

- 2 replicas du bouncer (anti-affinity + leader election),
- un Secret `crowdsec-calico-bouncer-secret` avec une API key aléatoire de
  64 caractères (préservée entre upgrades),
- un job PostSync qui INSERT la clé hashée dans la table `bouncers` du LAPI,
- une `GlobalNetworkPolicy` `crowdsec-bans` créée/maintenue dynamiquement.

**⚠️ La GNP seule ne bloque rien.** Il faut aussi des HostEndpoints — voir
le pré-requis 2 ci-dessus. Vérifier après déploiement :

```bash
kubectl get hostendpoints                 # doit retourner ≥ 1 entrée matchant le selector
kubectl get globalnetworkpolicy crowdsec-bans -o yaml | grep -c 'cidr:'
kubectl logs -l app.kubernetes.io/name=crowdsec-calico-bouncer
```

### Réglages courants

```yaml
calicoBouncer:
  enabled: true

  # Réagir aussi aux captchas (le profil default_captcha de profiles.yaml
  # gère les scanners HTTP en mode "soft" → si vous n'avez pas de bouncer L7
  # pour servir le captcha, autant les bloquer au L3/L4).
  actOnTypes: [ban, captcha]

  pollIntervalSeconds: 10           # cadence du stream (plus court = + de requêtes)

  # Délai de grâce avant qu'un nouveau ban n'entre dans la GNP : laisse un
  # bouncer L7 (traefik/nginx) servir une 403 propre ou un captcha avant que
  # le réseau ne coupe. 0 = immédiat.
  banApplyDelaySeconds: 120

  policy:
    name: crowdsec-bans
    selector: "has(kubernetes.io/hostname)"  # pour auto-HE
    # selector: "crowdsec-protected == 'true'"   # pour HE manuels / templatés
    preDNAT: true                   # mettre false en dataplane eBPF standard
    applyOnForward: true

  # Pour des clusters sans auto-HE et sans HE manuels, templater ici :
  hostEndpoints:
    manage: false
    entries: []
    # entries:
    #   - name: node1-ext
    #     node: node1
    #     interfaceName: eth0
```

### Comment fonctionne le `actOnTypes`

CrowdSec produit plusieurs types de décisions (`ban`, `captcha`, `throttle`…).
Calico travaille en L3/L4 et ne sait que `Allow` / `Deny`. Le bouncer
matérialise donc en **deny dur** uniquement les décisions dont le `type` est
listé dans `actOnTypes`. Par défaut : `[ban]`.

Stratégies typiques :

- **vous avez un bouncer L7** (nginx, traefik, HAProxy…) capable de servir un
  captcha → laisser `actOnTypes: [ban]`. Le L7 sert les captchas, le Calico
  bloque les bans durs.
- **pas de bouncer L7** → `actOnTypes: [ban, captcha]` (voire ajouter
  `throttle`). Toute décision active devient un deny réseau strict.

À garder en tête : le `profiles.yaml` par défaut (cf. `crowdsec.config.
profiles.yaml`) envoie les scénarios scanner-like (`http-probing`,
`http-crawl-non_statics`, …) vers une décision `captcha`. Avec
`actOnTypes: [ban]`, ces IPs **n'apparaissent pas** dans la GNP — c'est
voulu côté CrowdSec.

### Rotation de l'API key du bouncer

```bash
kubectl delete secret crowdsec-calico-bouncer-secret
helm upgrade ...                  # régénère le secret et relance le register-job
```

Le job `register-job.yaml` est en `ON CONFLICT (name) DO UPDATE` → la nouvelle
clé hashée écrase l'ancienne dans la table `bouncers`.

## Désinstallation

```bash
helm uninstall crowdsec -n crowdsec
```

Restent (volontairement) :

- les PVCs (`lapi-config`, `agent-config`, `agent-data`, `webui-data`,
  Postgres data) — supprimer manuellement si vraiment fini,
- le Secret `crowdsec-secrets` (annoté `helm.sh/resource-policy: keep`),
- le Secret `crowdsec-calico-bouncer-secret` (idem),
- la **`GlobalNetworkPolicy` `crowdsec-bans`** : créée dynamiquement par le
  pod bouncer via l'API Kubernetes, elle n'est pas trackée par Helm et donc
  pas supprimée par `helm uninstall`. Si vous désinstallez réellement le
  bouncer, la supprimer à la main :
  ```bash
  kubectl delete globalnetworkpolicy crowdsec-bans
  ```
  (Idem pour les `HostEndpoint`s si vous aviez utilisé
  `calicoBouncer.hostEndpoints.manage: true` — ceux-là **sont** trackés par
  Helm et donc supprimés normalement, mais des HE créés via auto-HE Calico
  ou manuellement hors chart ne le sont pas.)

Cette politique protège contre les `helm uninstall` accidentels (les mots de
passe DB restent cohérents avec les données déjà écrites, et les bans en
place ne sautent pas tant que la GNP n'est pas explicitement retirée).
