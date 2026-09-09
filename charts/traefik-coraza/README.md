# traefik-coraza

Chart *wrapper* : le chart officiel Traefik en subchart, plus les objets qui
insèrent un WAF Coraza / OWASP CRS sur la totalité du trafic entrant, sans
toucher aux Ingress applicatifs et sans déplacer la terminaison TLS.

Chaque réglage de la section `traefik:` de [values.yaml](values.yaml) corrige un
échec constaté en cluster, et le commentaire qui l'accompagne dit lequel. **Les
lire avant de modifier quoi que ce soit** : plusieurs sont contre-intuitifs.

## Ce que le chart ajoute

| Objet | Fichier | Rôle |
|---|---|---|
| `ConfigMap` `coraza-config` | [templates/configmap.yaml](templates/configmap.yaml) | `custom.conf`, monté sur `/opt/coraza/config.d` |
| `Service` `coraza` | [templates/service-coraza.yaml](templates/service-coraza.yaml) | épinglage local du sidecar (`ExternalName` ou `ClusterIP`+`nativeLB`) |
| `IngressRoute` `waf-catchall` | [templates/ingressroute-catchall.yaml](templates/ingressroute-catchall.yaml) | capte tout `websecure` en priorité 1000000 |
| `ConfigMap` `coraza-errorpages` | [templates/configmap-errorpage.yaml](templates/configmap-errorpage.yaml) | `403.html`, la page servie sur un refus du WAF |
| overrides du subchart | [values.yaml](values.yaml), section `traefik:` | DaemonSet, NodePort, entrypoint `internal`, sidecar + initContainer |

Le sidecar Coraza et l'initContainer qui patche le Caddyfile ne sont pas des
templates : ils vivent dans les values du subchart, qui les rend via
`deployment.additionalContainers` / `deployment.initContainers`.

## Clés du subchart renommées en amont

Piège si vous reprenez une configuration Traefik antérieure : quatre clés ont
changé de place, et le chart 41 a un schéma JSON qui **rejette** les anciennes.

| Ancienne clé | Clé 41.0.2 |
|---|---|
| `service.type`, `service.externalTrafficPolicy` | `service.spec.type`, `service.spec.externalTrafficPolicy` |
| `logs.general` / `logs.access` | `log` / `accessLog` |
| `ports.web.redirections` | `ports.web.http.redirections` |
| `ports.websecure.tls` | `ports.websecure.http.tls` |

Deux choix de ce chart, à ne pas « corriger » :

- `allowExternalNameServices` passe par `providers.kubernetesCRD` plutôt que par
  `additionalArguments` — le flag rendu est identique, mais il est validé par le
  schéma.
- `allowCrossNamespace` reste à `false`. Le catch-all omet le champ `namespace`
  sur sa référence de service, qui vise donc le Service du même namespace : le
  flag devient inutile, et c'est une permission cluster-wide de moins.

## Installation

```bash
helm upgrade --install traefik . -n ingress-controller --create-namespace
```

Le nom du release compte : voir « Remplacer un Traefik existant » ci-dessous.

Pas de `helm dependency update` nécessaire : `charts/traefik-41.0.2.tgz` est
versionné.

### Pourquoi le tarball du subchart est committé

`.github/workflows/publish-charts.yml` fait `helm package` sur chaque chart, sans
`helm dependency build` préalable et sans dépôt Helm déclaré dans le conteneur.
Sans le tarball dans l'arbre, le packaging échoue sur `found in Chart.yaml, but
missing in charts/ directory: traefik` — et comme la boucle tourne sous `set -e`,
c'est la publication de **tous** les charts du dépôt qui tombe.

Vendoriser évite de toucher à une CI partagée par les autres charts, supprime la
dépendance réseau au packaging, et rend le build reproductible via
`Chart.lock` + tarball.

Pour bumper la version du subchart :

```bash
helm dependency update .
```

puis committer `Chart.lock` et le nouveau `charts/*.tgz`, en retirant l'ancien.

## Remplacer un Traefik existant

Les objets du subchart portent **exactement les noms et labels qu'ils auraient eus
avec `helm install traefik traefik/traefik`** : `traefik` pour le DaemonSet, le
Service, le ServiceAccount et l'IngressClass, `traefik-<namespace>` pour le
ClusterRole et le ClusterRoleBinding. Vérifié par diff du rendu contre le chart
amont : aucun écart de nom ni de label.

Un `helm upgrade` du release existant adopte donc ses objets au lieu d'en créer de
nouveaux :

```bash
helm upgrade traefik . -n ingress-controller
```

Deux mécanismes le garantissent :

- `traefik.fullnameOverride: traefik` découple les **noms** du nom du release.
  Sans ça, un release nommé `traefik-coraza` produirait un DaemonSet
  `traefik-coraza` : Helm créerait les objets neufs et supprimerait les anciens —
  coupure de service, et nodePort réattribué sur le Service NodePort.
- [templates/naming-guard.yaml](templates/naming-guard.yaml) vérifie au rendu que
  les **labels de sélecteur** correspondent à ceux du standalone.

### Pourquoi un garde-fou plutôt qu'une note

`app.kubernetes.io/name` et `app.kubernetes.io/instance` composent
`spec.selector.matchLabels`, **immuable** sur un DaemonSet — le chart amont le
signale lui-même dans son `_helpers.tpl`. Une divergence ne dégrade pas le
service : elle fait échouer l'upgrade côté API, *après* que Helm a appliqué une
partie du release. Le garde-fou déplace cette découverte au `helm template`.

`app.kubernetes.io/instance` vaut `<release>-<namespace>` par défaut. Si le
release de ce chart ne porte pas le nom du release Traefik remplacé, le rendu
échoue avec la valeur exacte à poser :

```bash
helm upgrade traefik-coraza . -n ingress-controller --set traefik.instanceLabelOverride=traefik-ingress-controller
```

Relever la valeur réellement en place avant de choisir :

```bash
kubectl -n ingress-controller get daemonset,deployment -l app.kubernetes.io/name=traefik -o jsonpath='{range .items[*]}{.kind}{"\t"}{.metadata.name}{"\t"}{.spec.selector.matchLabels}{"\n"}{end}'
```

### Deux points à vérifier avant de basculer

- **Si l'existant est un `Deployment`** et non un DaemonSet, l'upgrade change de
  `kind` à nom constant : Helm crée le DaemonSet et supprime le Deployment. Le
  Service sélectionne les deux pendant la transition. Prévoir une fenêtre.
- **`standaloneNaming.expectedRelease`** vaut `traefik` par défaut. Pour un
  déploiement neuf sans Traefik à reprendre, le vider — et vider aussi
  `traefik.fullnameOverride` pour retrouver un nommage dérivé du nom du release.

Deux releases de ce chart ne peuvent pas coexister dans un même namespace, par
construction : `fullnameOverride` fixe les noms, et le ConfigMap `coraza-config`
comme le Service `coraza` ont eux aussi des noms fixes. C'est cohérent avec le
fait qu'il n'y a qu'un ingress controller.

## Avant la première installation

1. **Tag de l'image Coraza** — il vaut `…:TAG` et doit être épinglé avant tout
   `helm install`, **aux deux endroits** (voir la section suivante). `latest` est
   exclu : l'initContainer patche un template livré par l'image, une dérive
   silencieuse casserait le patch au pire moment.

2. **UID du sidecar** — fixé à `1000`, l'UID propriétaire de `/opt/coraza` dans
   l'image, où `/entrypoint.sh` écrit au démarrage. C'est une surcharge au niveau
   conteneur : le `podSecurityContext` du chart (`65532`) continue de s'appliquer
   à Traefik et à l'initContainer. À revérifier si vous changez d'image :

   ```bash
   docker run --rm --entrypoint sh ghcr.io/coreruleset/coraza-crs:TAG -c 'ls -ldn /opt/coraza /opt/coraza/config'
   ```

3. **Middlewares IP existants** — aucun défaut global n'existe :
   chaque `Middleware` avec `ipAllowList` ou `rateLimit` doit porter
   `ipStrategy.depth: 1`, sinon il compare le pair TCP — `127.0.0.1` après le
   passage par l'entrypoint `internal` — et renvoie 403.

   ```yaml
   apiVersion: traefik.io/v1alpha1
   kind: Middleware
   metadata:
     name: admin-only
   spec:
     ipAllowList:
       sourceRange:
         - 213.225.160.199/32
       ipStrategy:
         # Une seule valeur dans XFF grâce au `header_up X-Forwarded-For {client_ip}`
         # posé par le patch du Caddyfile : depth 1 = l'IP client réelle.
         depth: 1
   ```

## Le tag de l'image est écrit deux fois

Dans `traefik.deployment.initContainers` (patch du Caddyfile) et dans
`traefik.deployment.additionalContainers` (sidecar qui le consomme). Les deux
doivent porter le **même** tag, sinon l'initContainer patche un template issu
d'une version différente de celle qui le lit. `NOTES.txt` affiche les deux
images au déploiement et signale une divergence.

Il n'existe pas de valeur unique possible : le chart Traefik rend ces tableaux
avec `toYaml` **sans `tpl`**, donc aucune valeur du parent ne peut les alimenter,
et le sidecar doit rester dans le pod Traefik pour joindre l'entrypoint loopback.
Une ancre YAML tenait ce rôle avant ; elle a été retirée parce qu'elle est
résolue au *parse* du fichier, donc invisible au merge Helm — elle donnait
l'illusion d'un point unique de réglage.

Deux conséquences pour ArgoCD et `-f` :

- il n'y a rien à surcharger « en un point » ;
- Helm **remplace** les listes au lieu de les fusionner. Un
  `--set 'traefik.deployment.additionalContainers[0].image=…'` ne change pas le
  tag : il réduit le sidecar à cette seule clé et supprime `name`, `env`,
  `volumeMounts` et `NET_BIND_SERVICE`.

Voie normale : éditer les deux tags dans `values.yaml` et bumper `version` dans
`Chart.yaml`. Pour surcharger depuis l'extérieur, il faut redonner les deux
tableaux **entiers**.

Même logique pour `coraza.configMapName`, référencé littéralement dans
`traefik.deployment.additionalVolumes`. Changer l'un impose de changer l'autre.

## Rodage des faux positifs

Le chart démarre en `SecRuleEngine DetectionOnly` et doit y rester jusqu'à la fin
du rodage. Les exclusions se posent dans `coraza.extraRules` :

```yaml
coraza:
  extraRules: |
    SecRule REQUEST_URI "@beginsWith /api/ingest" \
        "id:1000,phase:1,pass,nolog,ctl:ruleRemoveById=920420"
```

Règles de rédaction :

- IDs dans la plage **1000–1999** uniquement.
- `ctl:ruleRemoveById` et non `SecRuleRemoveById` : `config.d` est chargé *avant* le
  CRS, donc une directive de chargement n'a encore rien à retirer, alors que
  `ctl:` agit à l'exécution.
- Regex **RE2** : pas de `(?!)`, `(?<!)`, `(?=)`, `(?<=)`, pas de backreference
  `\1`. `(?i)`, `\d`, `\w`, `\b` sont supportés.
- Pas d'`Include` du CRS : le template le charge déjà, un doublon donne
  `there is another rule with id 900000`.
- Le niveau de paranoïa ne se règle pas ici : `config.d` est inclus *avant*
  `crs-setup.conf`, un `setvar:tx.blocking_paranoia_level` y serait écrasé.
  Passer par les variables d'environnement `PARANOIA`, `BLOCKING_PARANOIA`,
  `ANOMALY_INBOUND`, `ANOMALY_OUTBOUND` (l'entrypoint les applique par `sed` sur
  `crs-setup.conf` ; le répertoire `overrides/`, lui, n'est alimenté par rien
  dans l'image).
- **Quoter `On` / `Off`.** YAML 1.1 — celui de Helm — les résout en booléens. Le
  chart les reconvertit (helper `secFlag`) et refuse au rendu toute valeur hors
  domaine pour `ruleEngine` et `audit.engine`, mais un fichier de values où on
  lit `On` et où Coraza recevrait `true` est un piège inutile.

## Inspection des réponses (phase 4)

**Réglé en amont le 2026-08-26.** L'image épinglée par ce chart embarque
désormais `coraza-caddy/v2`, la phase 4 s'exécute et les entrées d'audit
`"transaction"` sont produites.

Historique, parce qu'il explique la forme du chart : l'image
`ghcr.io/coreruleset/coraza-crs:*-caddy-*` était construite avec
`xcaddy build --with github.com/corazawaf/coraza-caddy`, **sans suffixe de
version**. Go résolvait donc le module v1 — `coraza-caddy` v1.2.2 (janvier
2023), sur un `coraza/v3` pré-release — dans lequel `ProcessResponseBody()`
n'est appelé **nulle part**. Le montage sandwich rendait la phase 4
*atteignable* ; le binaire ne l'exécutait pas.

[coreruleset/coraza-crs-docker#78](https://github.com/coreruleset/coraza-crs-docker/pull/78)
a corrigé la recette — `ARG CORAZA_VERSION` déclaré, module construit en
`coraza-caddy/v2@${CORAZA_VERSION}` — et a été **fusionnée le 2026-08-26**. Le
même commit a retiré le `tag:'${CORAZA_TAG}'` des `SecDefaultAction` par défaut,
qui empêchait les coraza récents de démarrer.

Vérifié sur le tag épinglé ici (`4.28.0-caddy-alpine-202608260808`, construit le
2026-08-26 à 21:30 UTC, donc après la fusion) :

```console
$ docker run --rm --entrypoint caddy \
    ghcr.io/coreruleset/coraza-crs:4.28.0-caddy-alpine-202608260808 build-info
dep  github.com/corazawaf/coraza-caddy/v2  v2.5.0
dep  github.com/corazawaf/coraza/v3        v3.7.0
dep  github.com/caddyserver/caddy/v2       v2.11.3
```

| | ancienne image (v1.2.2) | image du 2026-08-26 (v2.5.0) |
|---|---|---|
| règle `phase:3` sur `RESPONSE_CONTENT_TYPE` | déclenche | déclenche |
| règle `phase:4` quelconque | **jamais** | déclenche |
| `RESPONSE_BODY` | **toujours vide** | contient le corps |
| entrées d'audit `"transaction"` | **absentes** | présentes |
| `tag:` dans `SecDefaultAction` | injecté, bloquant | retiré |

### Ce que l'image ne corrige toujours pas

La version qu'elle épingle, `coraza-caddy` **v2.5.0**, porte deux défauts du
chemin de réponse :

| défaut | v2.5.0 (l'image) | v2.6.0 | v2.6.0 + patch du chart |
|---|---|---|---|
| WebSocket (statut 101 jamais écrit) | **cassé** | corrigé | corrigé |
| réponses en flux (aucun flush propagé) | **retenu** | **retenu** | au fil de l'eau |

`docker-bake.hcl` amont épingle encore `v2.5.0` alors que **v2.6.0** est publiée
depuis le 2026-08-24 : la correction WebSocket est là-haut, elle n'est
simplement pas encore prise par l'image. Et le flush n'est corrigé nulle part —
[corazawaf/coraza-caddy#344](https://github.com/corazawaf/coraza-caddy/pull/344)
est ouverte pour ça depuis le 2026-08-26.

C'est **la seule raison** pour laquelle le chart compile encore un binaire au
démarrage. Détail des deux défauts : §§ « Réponses en flux » et « WebSocket ».

### Correctif : compilation au démarrage du pod

Le chart **recompile le binaire Caddy au démarrage de chaque pod** plutôt que de
publier une image :

```
initContainer caddy-plugin-build   image officielle caddy:2.11.4-builder
  └─ clone coraza-caddy v2.6.0     (≠ v2.5.0 de l'image : corrige les WebSocket)
  └─ patch d'une ligne             (flush via http.ResponseController)
  └─ xcaddy build --with github.com/corazawaf/coraza-caddy/v2@v2.6.0
       └─ emptyDir caddy-bin  →  monté par-dessus /usr/bin/caddy dans `coraza`
```

Rien à construire, à publier, ni à maintenir : ni registre, ni chaîne de build,
ni image dérivée à re-tagger à chaque bump du CRS. Le binaire est le seul
artefact, il vit dans un `emptyDir` et meurt avec le pod.

**C'est un contournement assumé**, choisi parce que sa date de péremption est
proche. Ce qu'il coûte :

- **le démarrage du pod est allongé par la compilation** — **~2 min** mesurées
  caches vides sur l'image builder, 1 CPU, binaire de 50 Mo. Sur un
  DaemonSet, chaque nœud repasse par là à chaque rollout, et il ne sert aucun
  trafic pendant ce temps ;
- **les nœuds doivent joindre le proxy de modules Go *et* github.com au
  démarrage** — le module est cloné pour être patché avant compilation (cf.
  § « Réponses en flux »). Sans l'un des deux accès, l'initContainer échoue et le
  pod ne démarre pas — plus d'ingress sur ce nœud. L'échec est bruyant, il n'y a
  jamais de WAF silencieusement désactivé. Un `GOPROXY` interne se déclare en
  commentaire dans les values, un miroir git en changeant l'URL du clone ;
- **la compilation consomme CPU et mémoire** au démarrage (`requests: 1 CPU,
  1 Gi`).

Deux épinglages :

| épinglage | rôle |
|---|---|
| `@v2.6.0` sur le module | version de coraza-caddy. **Volontairement plus récente que le `v2.5.0` de l'image** : c'est elle qui répare les WebSocket |
| tag de `caddy:2.11.4-builder` | version de Caddy produite. `coraza-caddy` v2.6.0 exige `caddy >= v2.11.4` dans son `go.mod`, et MVS l'emporte sur le pin de xcaddy : le binaire serait 2.11.4 quel que soit ce tag, autant que le tag le dise |

L'image du sidecar est en Caddy 2.11.3 ; le binaire monté par-dessus est en
2.11.4. Seul ce dernier s'exécute, et le template `Caddyfile` de l'image est
identique entre les deux versions (vérifié par diff).

> ⚠️ **Ne pas monter au-delà de Caddy 2.11.4 sans rejouer le test WebSocket.**
> [caddyserver/caddy#7913](https://github.com/caddyserver/caddy/pull/7913),
> attendu en 2.11.5, enveloppe tout `ResponseWriter` dans un
> `IdleTimeoutWriter` qui n'expose plus que `Unwrap()` : l'assertion
> `i.w.(http.Hijacker)` de `coraza-caddy` échouera à son tour et les WebSocket
> recasseront. C'est l'autre moitié de la PR #344 amont.

Trois gardes de sortie avant de laisser le pod démarrer :

| garde | ce qu'elle rattrape |
|---|---|
| `grep -q 'NewResponseController(i.w).Flush()'` + absence de `i.w.(http.Flusher)` | l'ancre du patch a bougé en amont → flux muets |
| `grep -q hijackerTracker` | le tag cloné ne contient pas le correctif WebSocket |
| `caddy build-info \| grep coraza-caddy/v2` puis `grep '=>'` | module v1 résolu en silence, ou binaire construit sans le patch |

### Le jour où l'image suffira

Deux conditions, toutes deux en amont, aucune dans ce chart :

1. [corazawaf/coraza-caddy#344](https://github.com/corazawaf/coraza-caddy/pull/344)
   publiée (le flush par `http.NewResponseController`) — probablement en v2.7.0 ;
2. `docker-bake.hcl` de `coreruleset/coraza-crs-docker` remonté à cette version
   (il épingle encore `v2.5.0`, alors que v2.6.0 est publiée depuis le
   2026-08-24 — Renovate devrait s'en charger) et une image republiée.

Alors : reprendre le tag officiel, supprimer l'initContainer
`caddy-plugin-build`, le volume `caddy-bin` et ses deux montages. La procédure
de vérification est déjà écrite — c'est celle des deux sections « Non-régression
à rejouer à chaque bump » ci-dessous ; `scripts/verify.sh` §6 bis lit la version
du module et la présence du patch directement dans le pod.

`NOTES.txt` détecte l'absence de l'initContainer et affiche alors ce qu'il faut
vérifier sur le tag choisi.

### Pourquoi pas l'API de build de Caddy

`caddyserver.com/api/download?p=github.com/corazawaf/coraza-caddy/v2` rend
exactement ce binaire en quelques secondes, sans compilation. Elle est écartée :
le service répond `Contact the Caddy team` (HTTP 200, 22 octets) aux agents
automatisés, et ne sert le binaire qu'aux clients se présentant comme un
navigateur ou `curl`. C'est un filtrage délibéré ; l'automatiser depuis chaque
démarrage de pod reviendrait à le contourner. Si le compromis vous intéresse,
c'est une question à poser à l'équipe Caddy, pas un `User-Agent` à falsifier.

### Pourquoi pas simplement l'image officielle

C'est désormais la bonne question, et la réponse tient en une version.

L'image publiée après la fusion de la PR #78 embarque bien `coraza-caddy/v2`.
Mais `docker-bake.hcl` amont épingle

```hcl
variable "coraza-version" {
    # renovate: depName=corazawaf/coraza-caddy datasource=github-releases
    default = "v2.5.0"
}
```

et **v2.5.0 casse les WebSocket** : `corazawaf/coraza-caddy` a corrigé le défaut
dans la [PR #262](https://github.com/corazawaf/coraza-caddy/pull/262), livrée en
v2.6.0 le 2026-08-24 — deux jours avant la fusion de la PR #78, mais le pin n'a
pas suivi. Le flush, lui, n'est corrigé dans aucune version publiée.

Prendre l'image telle quelle reviendrait donc à échanger la phase 4 contre les
WebSocket, et à garder les flux muets. D'où le binaire recompilé, réduit à ce
qu'il apporte encore : une version du module plus récente, et un patch d'une
ligne.

Pour mémoire, l'historique du pin ignoré : `caddy/Dockerfile` ne déclarait aucun
`ARG CORAZA_VERSION` et construisait `--with github.com/corazawaf/coraza-caddy`,
chemin de module **v1**. Renovate mettait consciencieusement à jour un pin sans
effet (PR #37 → v2.1.0, PR #64 → v2.5.0) pendant que toutes les images publiées
embarquaient v1.2.2. La correction tenait en deux lignes :

```dockerfile
ARG CORAZA_VERSION
RUN xcaddy build --with github.com/corazawaf/coraza-caddy/v2@${CORAZA_VERSION}
```

Les autres variantes du même dépôt ne sont pas des porte-de-sortie : `nginx`
s'appuie sur `libcoraza` (moteur Go, hôte C) mais reste nginx, écarté par
l'architecture ; `apache` l'est pour la même raison. Les autres hôtes officiels
de Coraza — Envoy + `coraza-proxy-wasm`, HAProxy + `coraza-spoa` — remplaceraient
Caddy et le montage entier. `corazawaf/coraza-caddy` ne publie ni image ni
binaire (aucun asset de release), il n'y a donc pas non plus de binaire officiel
à injecter au démarrage.

## Réponses en flux (SSE, gRPC-gateway)

**Symptôme** : l'UI Argo CD ne se rafraîchit plus en arrière-plan. La page d'une
Application affiche l'état lu au chargement et n'évolue plus. Le serveur émet
bien — les `Watch` / `WatchResourceTree` d'`argocd-server` tiennent jusqu'à
776 s et finissent en `grpc.code=OK` — mais rien n'arrive au navigateur.

**Mesure en cluster.** Le sandwich fait passer chaque requête deux fois dans
Traefik, donc deux compteurs sur la MÊME requête. Colonne
`DownstreamContentSize` des logs d'accès :

| flux Argo CD | Traefik → Caddy (`internal`) | Caddy → navigateur (`websecure`) |
|---|---|---|
| `applications`, 776,1 s | 41 815 o | **0 o** |
| `resource-tree`, 558,8 s | 32 286 o | **0 o** |
| `applications`, 240,8 s | 16 717 o | **0 o** |

Côté Caddy : 26 requêtes `/api/v1/stream/…` abouties en `200` avec `size = 0`
pour **toutes**, y compris sur 26,6 s et 60,0 s. Les seules réponses avec
`size > 0` sont des `401` — courtes et complètes.

**Le discriminant n'est pas la nature SSE de la réponse.** Argo CD passe par
grpc-gateway : le client demande `Accept: text/event-stream`, le serveur répond
`Content-Type: application/json`, en chunked, sans `Content-Length`. Ce qui
déclenche la bufferisation est l'appartenance du Content-Type de la **réponse** à
`SecResponseBodyMimeType`. Un correctif conditionné au Content-Type de la réponse
ne rattraperait donc pas Argo CD, et retirer `application/json` de la liste
reviendrait à renoncer à l'inspection de toutes les réponses JSON.

### Trois défauts sur le même chemin

Reproduits sur banc local le 2026-08-19, avec l'image du sidecar épinglée,
derrière un amont qui émet une ligne JSON par seconde en chunked sans
`Content-Length`. Deux sont réglés en amont, **le deuxième ne l'est pas** :

| # | défaut | où en est-on |
|---|---|---|
| 1 | `coraza-caddy` v1.2.2 bufferise sans consulter `responseBodyAccess` | ✅ réglé — l'image embarque `/v2` depuis le 2026-08-26 |
| 2 | `coraza-caddy` v2.5.0 **et v2.6.0** ne propagent aucun flush | ❌ **ouvert en amont** ([#344](https://github.com/corazawaf/coraza-caddy/pull/344)) — patché ici en attendant |
| 3 | `read_timeout 60s` du template amont coupe les flux silencieux | ✅ contourné par `read_timeout 0` (`caddy-template-patch`) |

**1. La v1.2.2 ignore `responseBodyAccess`.** `stream.go` ne décide de laisser
passer (`stream = true`) qu'à partir de `IsResponseBodyProcessable()`, donc du
seul Content-Type ; `IsResponseBodyAccessible()` n'est **jamais** consulté. La
règle `id:1003` livrée par ce chart —
`ctl:responseBodyAccess=Off` sur `Accept: text/event-stream` — ne protège donc
rien. Pire, `coraza.go` ne recopie le tampon vers le client qu'**après** le
retour de `next.ServeHTTP`, et le jette purement et simplement si celui-ci
remonte une erreur : d'où les `size = 0` même sur des flux terminés.

**2. Ni la v2.5.0 ni la v2.6.0 ne propagent de flush.** Le `/v2` corrige la
phase 4 et rend `id:1003` opérante — vérifié : `p4=0` et `RESPONSE_BODY` vide
sur une requête portant l'en-tête `Accept`, donc Coraza ne bufferise plus. Le
flux reste pourtant bloqué, parce que `rwInterceptor.Flush()` fait :

```go
if flusher, ok := i.w.(http.Flusher); ok {
	flusher.Flush()
}
```

or Caddy n'implémente plus `Flush()` sur ses ResponseWriter depuis 2.6.3 :
`caddyhttp.responseRecorder` expose `FlushError() error`, et
`caddyhttp.ResponseWriterWrapper` seulement `Unwrap()`. L'assertion échoue
**toujours**, aucun flush n'atteint le client, et le `flush_interval` de la
ReverseProxy — pourtant bien à « immédiat », `Content-Length` étant absent — n'a
rien à pousser. Conséquence : **toute** réponse en flux traversant `coraza_waf`
est retenue jusqu'à la fin de la réponse amont, y compris celles que le WAF ne
bufferise pas. Le correctif est d'une ligne, et part en PR tel quel :

```diff
 	i.flushWriteHeader()
-	if flusher, ok := i.w.(http.Flusher); ok {
-		flusher.Flush()
-	}
+	//nolint:errcheck
+	http.NewResponseController(i.w).Flush()
```

`http.ResponseController` reconnaît `FlushError()`, `Flush()` et `Unwrap()` : il
marche avec les ResponseWriter de `net/http` comme avec ceux de Caddy.

C'est **exactement** le correctif de la PR
[#344](https://github.com/corazawaf/coraza-caddy/pull/344) ouverte en amont le
2026-08-26, qui y ajoute la recherche de `http.Hijacker` le long de la chaîne
d'`Unwrap()` — nécessaire à partir de Caddy 2.11.5. Il n'y a donc rien à
reverser : ce chart applique la même ligne sur une copie locale, en attendant la
publication.

> ⚠️ **v2.6.0 n'y change rien.** Elle réorganise `Flush()` (garde
> `allowFlushing`, propagation conditionnée au flush des en-têtes) mais garde
> l'assertion `i.w.(http.Flusher)`, seulement renommée `fl`. Mesuré le
> 2026-08-27 : v2.6.0 vierge retient toujours tout le flux, y compris avec
> `ctl:responseBodyAccess=Off` — donc hors de toute bufferisation par le WAF.
> C'est aussi pourquoi l'ancre du patch diffère entre les deux versions du
> module : la variable y est renommée `fl`.

**3. Le template amont coupe les flux silencieux à 60 s.** Le `Caddyfile` de
l'image pose `read_timeout ${PROXY_TIMEOUT}` (60 s par défaut) sur le transport
du `reverse_proxy`, et Caddy réarme ce délai avant **chaque** lecture amont : un
flux qui n'émet rien pendant plus de 60 s est coupé, avec exactement l'erreur
observée en cluster (`aborting with incomplete response … i/o timeout`).
Mesuré : coupure à 60,0 s pile avec le template amont, flux intact au-delà de
70 s de silence avec `read_timeout 0`. La contrepartie est documentée dans les
values : `dial_timeout` et `write_timeout` gardent leurs 60 s, un backend
injoignable échoue donc toujours vite.

### Résultats de banc

| chaîne | flux JSON chunked | réponse courte | WebSocket |
|---|---|---|---|
| Caddy seul, sans `coraza_waf` | 1 ligne/s | ok | ok |
| ancienne image (v1.2.2) | **tout à la fin** | phases 3+5, `p4=0`, corps vide | ok |
| image du 2026-08-26 (v2.5.0) | **tout à la fin** | phases 3+4+5, corps lu | **cassé** |
| v2.6.0 vierge | **tout à la fin** | phases 3+4+5, corps lu | ok |
| v2.6.0 + patch de flush | **1 ligne/s** | phases 3+4+5, corps lu | ok |
| + `read_timeout 0` | survit à 70 s de silence | inchangé | inchangé |

Les deux dernières lignes sont ce que le chart déploie. Mesures du 2026-08-19
(les trois premières) et du 2026-08-27 (v2.6.0), lecture socket brute horodatée
côté client — un client HTTP qui bufferise masque le résultat.

### Non-régression, à rejouer à chaque bump

1. un amont qui émet une ligne par seconde en `Content-Type: application/json`,
   en chunked, **sans** `Content-Length` ;
2. à travers le WAF, `curl -N --no-buffer -H 'Accept: text/event-stream'` : les
   lignes doivent arriver **une par seconde**. Un client qui bufferise lui-même
   masque le résultat — préférer une lecture socket brute horodatée ;
3. **la même requête sans l'en-tête `Accept`** doit rester bufferisée et
   inspectée : c'est ce qui prouve que le correctif ne désarme pas l'inspection ;
4. une réponse courte et complète doit continuer à déclencher les phases 3, 4 et
   5 (règle `id:1201` des values, `RESPONSE_BODY` non vide dans l'audit) ;
5. un flux silencieux plus de 60 s doit survivre.

Critère de sortie en cluster : sur `/api/v1/stream/applications`, le
`DownstreamContentSize` de la passe `websecure` devient non nul et suit celui de
la passe `internal`.

### Ce qui n'est pas concerné

Le périmètre de ce défaut-ci est la réponse HTTP **en flux**. La bascule de
protocole (WebSocket) emprunte un autre chemin dans le module, et souffre d'un
défaut distinct — voir la section suivante.

## WebSocket

`coraza-caddy` v2.5.0 — la version qu'épingle l'image officielle — **casse les
WebSocket**. Le chart s'en sort en construisant contre **v2.6.0**, où le défaut
est corrigé en amont. Ce n'est donc pas un patch maison : celui qui avait été
écrit ici a été jeté au profit du correctif amont, meilleur.

### Symptôme

À travers `coraza_waf`, un handshake WebSocket ne reçoit **jamais** sa ligne de
statut : le client attend, puis expire. Aucune erreur côté Caddy, la requête
apparaît normalement dans le log d'accès. Le même binaire, même configuration,
sans la directive `coraza_waf`, sert le 101 immédiatement — et une requête HTTP
ordinaire passe dans les deux cas. Le défaut est donc bien dans le module, sur
le seul chemin de bascule.

Mesuré sur banc, amont qui répond 101 puis détourne la connexion et échange des
trames :

| chaîne | handshake | trames |
|---|---|---|
| amont direct, sans WAF | 101 immédiat | échangées |
| `coraza-caddy` v1.2.2 (ancienne image) | 101 immédiat | échangées |
| **v2.5.0 (image officielle du 2026-08-26)** | **rien, timeout client** | — |
| v2.5.0 + patch maison (écarté) | 101 immédiat | échangées |
| **v2.6.0 vierge (ce que le chart déploie)** | 101 immédiat | échangées |

### Cause

`rwInterceptor.WriteHeader()` se contente d'**enregistrer** le statut. Il n'est
écrit en aval qu'au premier `Write()` — à dessein : une règle de phase 4 doit
pouvoir le remplacer par un 403 tant que rien n'est parti.

Or sur une bascule de protocole, `reverse_proxy` appelle `WriteHeader(101)` puis
détourne immédiatement la connexion (`Hijack`) pour pomper les octets dans les
deux sens. Aucun `Write()` ne suivra jamais : le 101 meurt dans l'intercepteur.
La v1.2.2 n'avait pas ce défaut — son `streamRecorder` écrit le statut en aval
dès que le corps est jugé non inspectable, ce qui est le cas d'un 101.

### Défaut générique, correctif amont

Rien là-dedans ne tient à ce montage : ni au sandwich, ni à Traefik, ni au CRS.
Tout `coraza_waf` devant un backend WebSocket, en `On` comme en `DetectionOnly`,
tombe dessus. C'était donc à corriger en amont — et ça l'a été, indépendamment,
par [corazawaf/coraza-caddy#262](https://github.com/corazawaf/coraza-caddy/pull/262)
(« fix: support WebSocket connections when WAF is active »), livrée en **v2.6.0**
le 2026-08-24.

Le correctif amont va plus loin que celui que le chart portait :

| | patch maison écarté | v2.6.0 |
|---|---|---|
| flush du 101 avant le `Hijack` | oui | oui |
| détection du `Hijack` (`hijackerTracker`) | non | oui |
| post-traitement sauté sur connexion détournée | non | oui |
| `response.WriteHeader on hijacked connection` | subsiste dans les logs | supprimé |

Le chart a donc **retiré son patch** et construit contre v2.6.0. Il n'y a rien à
proposer en amont : le défaut y était déjà connu et réglé.

### Ce que le WAF inspecte encore

Le handshake est une **requête HTTP ordinaire** : URI, en-têtes, cookies, IP
source. Les phases 1 et 2 s'exécutent en entier, le CRS aussi, et un refus rend
un 403 au lieu du 101. Vérifié sur banc avec une règle piège en `deny` sur l'URI
du handshake.

Ce qui suit le 101 échappe au WAF. Ce n'est pas un arbitrage : après le `Hijack`
la connexion n'est plus du HTTP, et le CRS n'a pas de règles de trames
WebSocket. Ce que le WAF filtre sur ce trafic, c'est l'**accès à l'endpoint**.

### Non-régression, à rejouer à chaque bump

1. handshake WebSocket à travers le WAF → `101 Switching Protocols`, puis
   trames échangées dans les deux sens ;
2. règle piège en `deny` sur l'URI du handshake → 403, ce qui prouve que la
   requête reste inspectée ;
3. réponse HTTP courte → phases 3, 4 et 5, `RESPONSE_BODY` non vide : aucune
   inspection n'est désarmée par la bascule ;
4. le `grep -q hijackerTracker` de l'initContainer : si le tag cloné perdait le
   correctif, le build échoue au lieu de livrer un WAF qui coupe les WebSocket.

En cluster, un `curl` suffit à voir le handshake :

```bash
curl -sSi --http1.1 -m 10 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  https://<host>/<chemin-ws> | head -1
```

## Prise en compte des changements de configuration

Modifier `coraza.config` ou `coraza.extraRules` met à jour la ConfigMap, donc le
fichier monté — mais **Coraza ne relit ses `include` qu'au provisioning du module
Caddy**. Sans mécanisme dédié, un `helm upgrade` ne change rien tant que le pod
n'est pas recréé, ce qui sur un DaemonSet coupe le trafic du nœud *et* relance la
compilation du binaire.

Une `livenessProbe` sur le conteneur `coraza` fait redémarrer **ce conteneur
seulement** :

1. elle compare l'empreinte de `/opt/coraza/config.d/*.conf` à celle qu'a
   effectivement chargée le processus en cours ;
2. si elle a changé, elle exécute `caddy validate` — qui provisionne réellement
   le module, donc compile les règles et charge le CRS, sans ouvrir de port ;
3. si la configuration est valide, la sonde échoue et kubelet redémarre le
   conteneur. Sinon elle réussit : **rien ne bouge**, le WAF continue sur la
   configuration précédente et le refus est journalisé.

Ce que ça coûte et ce que ça évite :

| | |
|---|---|
| coupure du WAF | **~2 s**, mesurée, fail-closed (502) |
| pod recréé | non — Traefik continue de servir |
| binaire recompilé | non — il vit dans un `emptyDir` de pod, qui survit au redémarrage du conteneur |
| redémarrages par changement | exactement un |

Le redémarrage **coupe les connexions en cours**, y compris les réponses en flux
et les WebSocket décrits plus haut : les clients doivent se reconnecter. Sur du
SSE c'est transparent — les navigateurs réessaient — mais c'est à savoir avant de
pousser une modification de règles en heure de pointe.

**Les deux gardes anti-CrashLoopBackOff**, chacune pour un mode d'échec observé :

- **la validation préalable.** Sans elle, une règle invalide poussée dans la
  ConfigMap empêcherait le conteneur de repartir : redémarrage, échec,
  redémarrage… et le WAF étant fail-closed, le nœud ne servirait plus rien du
  tout. Une erreur de syntaxe dans `extraRules` ne doit jamais pouvoir couper
  l'ingress.
- **la référence ancrée sur le processus.** La sonde mémorise l'empreinte avec
  l'heure de démarrage de PID 1, et non dans un fichier supposé neuf à chaque
  redémarrage : selon le runtime, la couche inscriptible du conteneur **survit**
  au redémarrage. La version naïve rebouclait — reproduit, puis corrigé.

### Pourquoi pas `caddy reload`

Un rechargement gracieux ne coûterait aucune coupure. Il a été essayé, et il ne
marche pas ici : coraza-caddy v2 conserve ses instances WAF dans un pool dont la
clé ne hache que les **chemins** d'`include` et la chaîne `directives` — jamais
le contenu des fichiers. Le rechargement journalise
`reusing existing WAF instance from pool`, réutilise le WAF existant et **ne relit
pas les règles** : il paraît réussir sans rien changer. C'est le mode d'échec le
plus traître de tout ce montage.

Il est contournable — inscrire l'empreinte de la configuration dans le Caddyfile
via `directives` change la clé du pool — mais cela repose sur un détail interne
d'amont, et l'écart de coupure avec le redémarrage est de 2 s. Le redémarrage a
été préféré.

## Page d'erreur servie sur un refus

Sans réglage, un refus de Coraza rend le `403 Forbidden` nu de Caddy : une ligne
de texte noir sur blanc. Pour un utilisateur légitime pris par un faux positif,
ce message ressemble à un refus de **droits** — il croit son compte bloqué et
signale la mauvaise chose.

Le chart sert donc une page à la place. Trois morceaux, qui vont ensemble :

| Morceau | Où |
|---|---|
| la page | `ConfigMap` `coraza-errorpages`, clé `403.html` |
| le montage | `traefik.deployment` — volume + `volumeMount` sur `/opt/coraza/errorpages` |
| le service de la page | bloc `handle_errors 403` injecté dans le Caddyfile par l'initContainer |

Pour la remplacer, une seule clé :

```yaml
coraza:
  errorPage:
    html: |
      <!DOCTYPE html>
      …
```

Vide → celle livrée par le chart ([files/error-403.html](files/error-403.html)),
neutre et sans marque.

### Ce que la page peut afficher

Elle est rendue comme un template Go par le module `templates` de Caddy, ce qui
donne accès à ce qu'elle ne peut pas savoir d'elle-même :

| Placeholder | Valeur |
|---|---|
| `{{.ClientIP}}` | IP réelle du client — celle de la ligne d'audit Coraza, pas celle de Traefik |
| `{{.RemoteIP}}` | IP du pair TCP, donc Traefik. Sans intérêt ici |
| `{{.Req.Host}}` | hôte demandé |

`.ClientIP` est résolu par le même `trusted_proxies` / `client_ip_headers` que
le reste du Caddyfile. **Mesuré sur banc** : sans `X-Forwarded-For` il rend l'IP
du pair, avec, celle du client.

L'URL et l'heure, elles, s'écrivent en JavaScript — la page est servie sur
l'URL d'origine, `window.location.href` la porte déjà.

> 🛑 Corollaire : toute double accolade ouvrante qui n'est pas un placeholder
> connu fait échouer le rendu **à la requête**, et Caddy répond 500. Un refus du
> WAF deviendrait une panne, sur la seule page que personne ne surveille. Le
> chart refuse de rendre dans ce cas plutôt que de le laisser partir.

**Un seul fichier, tout en ligne** — CSS dans la page, images en `data:` URI. Le
client qui reçoit ce refus n'obtiendra pas davantage une feuille de style ou un
logo servis à part : ils repasseraient par le WAF, sur une requête du même
client que celui qu'il vient de refuser. Une page à moitié rendue est pire que
pas de page.

### Portée exacte, mesurée sur banc

| Cas | Résultat |
|---|---|
| refus en phase 1 ou 2 — règle explicite **et** score d'anomalie CRS | la page, code 403 |
| `403` émis par l'application derrière le WAF | **intact**, l'appli garde le sien |
| backend injoignable (502) | page Caddy par défaut |
| refus en phase 3 ou 4 (côté réponse) | **pas de page** : connexion coupée |

Les deux lignes du milieu tiennent au filtre `403` de `handle_errors` et au fait
qu'une réponse amont n'est pas une erreur Caddy. La dernière est sans remède :
les en-têtes sont déjà partis quand la phase 3 refuse. Sans conséquence
pratique, les faux positifs que rencontre un humain étant tous côté requête.

### Modifier la page ne coupe rien

La page vit dans **sa propre** ConfigMap, montée en **répertoire** — pas en
`subPath`. Kubelet resynchronise le volume, `file_server` relit le fichier à la
requête suivante : pas de rollout, pas de redémarrage du sidecar, pas de coupure
du WAF. Compter la minute de propagation du volume.

C'est la raison d'être de la ConfigMap séparée : la sonde de redémarrage hache
`config.d/*.conf`, une page rangée là-bas ferait redémarrer le WAF à chaque
retouche de texte.

### Pour revenir à la page par défaut de Caddy

Vider `html` ne suffit pas — il faut retirer la stanza `handle_errors` de l'awk
de l'initContainer **et** son `grep -q` de garde. `initContainers` et
`additionalContainers` sont rendus par le chart Traefik avec `toYaml` **sans**
`tpl` : aucune value ne peut les conditionner.

## Mises à jour transparentes (rollout)

Le démarrage d'un pod Traefik de ce chart est **long et dépendant du réseau**
(patch du Caddyfile, ~2 min de compilation Caddy, chargement du CRS, et —
bouncer activé — téléchargement + compilation Yaegi du plugin). Une mise à
jour de configuration ne doit jamais coûter cette phase d'init au trafic :

- `traefik.updateStrategy` est **épinglé** à `maxUnavailable: 0` +
  `maxSurge: 1` (c'est le défaut du chart 41.0.2, mais un bump amont ne doit
  pas pouvoir le changer en silence) : le pod remplaçant est créé **à côté**
  de l'ancien, qui sert jusqu'à Ready + `minReadySeconds` ;
- `traefik.deployment.minReadySeconds: 30` : un remplaçant qui devient Ready
  puis meurt (plugin mal chargé, OOM au chargement du CRS) n'a pas déjà fait
  tuer l'ancien ;
- `templates/rollout-guard.yaml` **refuse de rendre** toute combinaison qui
  casserait cette transparence : stratégie absente ou sans surge,
  `maxUnavailable > 0`, `hostPort` sur un entrypoint (deux pods coexistent
  sur le nœud pendant le surge — un hostPort bloquerait le rollout). La
  bascule du trafic est atomique via le Service NodePort
  `externalTrafficPolicy: Local`, qui ne route que vers les pods Ready ;
- bouncer activé, la garde exige en plus
  `traefik.experimental.abortOnPluginFailure: true` : un plugin qui échoue au
  démarrage ne produit jamais un pod Ready sans bouncer qui remplacerait
  l'ancien en silence — le remplaçant ne démarre pas, l'ancien continue de
  servir, l'échec est bruyant.

Pour ASSUMER une stratégie disruptive (par ex. déploiement en `hostPort`, où
le surge est impossible) : `rollout.allowDisruptiveUpdates: true` désactive
ces gardes.

## Bouncers CrowdSec (`bouncer`)

Deux bouncers L7 **optionnels et indépendants** (désactivés par défaut),
appuyés sur le plugin Traefik
[`maxlerebourg/crowdsec-bouncer-traefik-plugin`](https://github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin),
pensés pour fonctionner avec la stack du chart voisin `crowdsec-all-in-one` :

- **`bouncer.ban`** — rejet pur des décisions `ban` (403 + page HTML) ;
- **`bouncer.captcha`** — les décisions `captcha` suspendent la requête le
  temps d'un contrôle (page + widget, pensé pour [Cap](https://capjs.js.org)
  auto-hébergé) ; les `ban` restent rejetés par la même instance.

**Rien ne tourne ici** : le bouncer est du code exécuté *dans* le processus
Traefik. Le chart pose les Middlewares, la ConfigMap des pages et le Secret
des clés ; il génère les clés d'API au rendu (préservées ensuite), en pose
une copie dans le namespace CrowdSec et y enregistre les bouncers par un job
PostSync (équivalent `cscli bouncers add`, idempotent).

### Ce qui est vérifié avant de rendre

Un bouncer activé sans son câblage échouerait en cluster de façon opaque
(routeur en erreur, pod en `ContainerCreating`). Le chart refuse donc de
rendre, avec le geste exact à faire :

- `values.schema.json` — **`bouncer.crowdsec.namespace` et
  `bouncer.crowdsec.lapiHost` n'ont pas de défaut** et sont exigés dès qu'un
  bouncer est activé : il n'existe aucune valeur sûre à deviner ;
- `templates/bouncer/guard.yaml` — le plugin doit être déclaré dans
  `traefik.experimental.plugins.<pluginAlias>`, la ConfigMap des pages et le
  Secret des clés doivent être montés (`traefik.deployment.additionalVolumes`
  + `traefik.additionalVolumeMounts` — le subchart rend ces listes sans `tpl`,
  ce chart ne peut pas les poser lui-même), les URL du captcha `custom`
  doivent être renseignées, et `attach` doit être cohérent (catch-all actif,
  middleware activé).

🛑 Le plugin est **téléchargé et compilé au démarrage de Traefik**
(plugins.traefik.io puis GitHub) : une dépendance de démarrage de l'entrée
publique de plus, qui s'ajoute à celles des initContainers Coraza.

### Activation minimale

```yaml
bouncer:
  crowdsec:
    namespace: crowdsec
    lapiHost: crowdsec-service.crowdsec.svc.cluster.local:8080
  ban:
    enabled: true
  attach:
    enabled: true
    middleware: ban

traefik:
  experimental:
    abortOnPluginFailure: true   # requis (garde) : jamais de pod Ready sans bouncer
    plugins:
      bouncer:
        moduleName: github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin
        version: v1.4.4          # épingler une version relevée
  deployment:
    additionalVolumes:
      - name: crowdsec-bouncer-pages
        configMap: { name: crowdsec-bouncer-pages }
      - name: crowdsec-bouncer-keys
        secret: { secretName: crowdsec-bouncer-keys }
  additionalVolumeMounts:
    - { name: crowdsec-bouncer-pages, mountPath: /crowdsec/pages, readOnly: true }
    - { name: crowdsec-bouncer-keys,  mountPath: /crowdsec/keys,  readOnly: true }
```

Côté stack CrowdSec : ouvrir la LAPI (port 8080) aux pods Traefik dans la
NetworkPolicy de son namespace (`networkPolicy.extraIngress` de
crowdsec-all-in-one), et router les scénarios voulus vers une décision
`captcha` dans ses `profiles.yaml` si le bouncer captcha est utilisé.

### Accrochage (`bouncer.attach`)

Pose un routeur `catchAll.match` à `catchAll.priority + 1` vers le service
Coraza — le sandwich WAF est conservé — portant le middleware choisi, et, si
`bypassHosts` est renseigné, un routeur **sans** bouncer à `+ 2`. Entrypoints,
service, priorité et TLS sont repris de `catchAll`/`coraza` : rien à recopier.
Si le middleware ne se résout pas, seul ce routeur tombe et le `waf-catchall`
reprend le trafic (sans bouncer) : l'échec dégrade au lieu de couper.

🛑 Un serveur de captcha auto-hébergé doit être listé dans `bypassHosts` : le
plugin n'a pas d'exclusion par hôte, et un visiteur en remédiation doit
pouvoir charger le widget. Un hôte listé n'est plus protégé par CrowdSec —
Coraza, lui, l'inspecte toujours.

Alternative : laisser `attach.enabled: false` et référencer le middleware
depuis vos Ingress/IngressRoute applicatifs (noms stables tant que
`bouncer.hashSuffix` reste `false`).

### Charte graphique

Les pages embarquées sont neutres et s'habillent via `bouncer.branding`
(`siteName`, `logo` — URL ou data-URI, recommandé pour rester sans dépendance
réseau —, `primaryColor`, `backgroundColor`, `supportContact`, `lang`). Pour
un contrôle total : `ban.overrideHtml` / `captcha.overrideHtml`.

⚠️ Ces pages sont des templates **Go rendus par le plugin** (`{{ .ClientIP }}`,
`{{ .SiteKey }}`, `{{ .FrontendJS }}`) : ne pas retirer ces marqueurs d'un
HTML custom, et ne jamais les passer par `tpl`.

Le plugin ne relit une page qu'à la **création** de l'instance du middleware :
après modification, soit activer `bouncer.hashSuffix` (nom du Middleware
suffixé de l'empreinte des pages → objet recréé → page relue ; avec ArgoCD,
activer aussi `pages.waitJob` pour laisser le kubelet propager la ConfigMap),
soit redémarrer les pods Traefik.

### Rotation des clés d'API

```bash
kubectl delete secret crowdsec-bouncer-keys                  # namespace du release
kubectl -n <ns-crowdsec> delete secret crowdsec-bouncer-keys # la copie
helm upgrade ...    # régénère les clés et relance le register-job
```

### Adhérence à CrowdSec

Toute l'adhérence est dans `bouncer.crowdsec` : le namespace de la stack, la
LAPI, et l'accès DB du job d'enregistrement (défauts alignés sur
crowdsec-all-in-one : Secret `crowdsec-secrets`, service `crowdsec-pgbouncer`).
⚠️ Le job suppose le **schéma interne** de la table `bouncers` (validé contre
CrowdSec v1.7.x, la version épinglée par crowdsec-all-in-one) : à revérifier
sur un bump majeur de la stack. Pour s'en passer :
`bouncer.registerJob.enabled: false` et enregistrer soi-même
(`cscli bouncers add`, puis reporter les clés dans le Secret).

## Limites connues

- **Règles à état non fiables.** Les collections `IP` / `SESSION` / `USER` de
  Coraza sont en mémoire par process, non partagées. DOS-protection et détection
  de brute force sont inopérantes en pratique, même avec l'épinglage local,
  puisque plusieurs nœuds voient du trafic. Le scoring d'anomalie CRS est
  per-transaction, donc intact.
- **Audit log** : les entrées `"transaction"` étaient absentes avec les images
  d'avant le 2026-08-26 — même cause que la phase 4. Réglé en amont.
- **Non testés** : gRPC, upload au-delà de `SecRequestBodyLimit`,
  renouvellement ACME réel, ajout d'un Ingress avec un nouveau host, kill du
  sidecar. WebSocket et SSE, eux, sont mesurés sur banc — cf. § « Réponses en
  flux » et § « WebSocket ».
- **Réponses compressées** : si le client demande `gzip`, Caddy relaie la
  réponse compressée telle quelle et la phase 4 inspecte des octets compressés —
  les règles RESPONSE-95x sont aveugles sur ces réponses. **Mesuré** sur banc :
  une règle `phase:4` cherchant un marqueur en clair ne se déclenche pas, et
  `RESPONSE_BODY` contient bien du gzip. La phase 4 vaut donc pour les réponses
  non compressées, pas comme protection générale.

  Remède mesuré, **non appliqué** : neutraliser la compression en amont du WAF
  (`header_up Accept-Encoding identity` dans le patch du Caddyfile) et
  recompresser pour le client avec un middleware `compress` de Traefik sur
  l'IngressRoute catch-all — Coraza voit alors le corps en clair, le client
  reçoit du gzip. Deux points vérifiés : la recompression doit être faite par
  Traefik et **pas** par un `encode` dans le sidecar, `order coraza_waf first`
  rendant `coraza_waf` le handler le plus externe (un `encode` interne
  compresserait avant l'inspection) ; et le `compress` de Traefik propage bien
  `Flush()`, il ne recasse donc pas les flux. À trancher d'abord : la
  compression migre des applications vers l'edge, Coraza transporte des corps
  pleine taille, `responseBodyLimit` (512 Kio) est atteint bien plus souvent
  donc `ProcessPartial` aussi, et un middleware `compress` déjà accroché à un
  Ingress applicatif compresserait sur la passe `internal`, donc avant Coraza.
- Le patch du Caddyfile par initContainer est un contournement. Un point
  d'extension propre côté `coreruleset/coraza-crs-docker` serait préférable.
- **`DetectionOnly` neutralise aussi les `deny` explicites**, y compris ceux des
  règles maison : le smoke test de blocage ne renvoie 403 qu'avec
  `coraza.config.ruleEngine: "On"`. La règle est bien évaluée et journalisée
  dans les deux cas.
