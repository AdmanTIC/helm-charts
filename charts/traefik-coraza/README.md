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

**En l'état, avec l'image amont, la phase 4 ne s'exécute pas.** Le montage
sandwich la rend *atteignable* — c'est sa justification face à ForwardAuth ou au
plugin WASM — mais le binaire livré ne l'exécute pas.

L'image `ghcr.io/coreruleset/coraza-crs:*-caddy-*` est construite avec
`xcaddy build --with github.com/corazawaf/coraza-caddy`, **sans suffixe de
version**. Go résout donc le module v1 : `coraza-caddy` v1.2.2 (janvier 2023),
sur un `coraza/v3` pré-release. Dans cette version, `stream.go` n'appelle
`ProcessResponseHeaders()` que depuis `WriteHeader()` ; `ProcessResponseBody()`
n'est appelé **nulle part**. L'interception du corps de réponse est arrivée en
v2 (`interceptor.go`).

Constaté sur banc local avec le tag épinglé dans `values.yaml` :

| | image amont (coraza-caddy v1.2.2) | binaire reconstruit (v2.5.0 / coraza v3.7.0) |
|---|---|---|
| règle `phase:3` sur `RESPONSE_CONTENT_TYPE` | déclenche | déclenche |
| règle `phase:4` quelconque | **jamais** | déclenche |
| `RESPONSE_BODY` | **toujours vide** | contient le corps |
| entrées d'audit `"transaction"` | **absentes** | présentes |

Ce n'est ni un problème de `SecResponseBodyMimeType` (le `; charset=…` est bien
retiré par Coraza avant comparaison), ni de compression, ni du sandwich : le
même résultat s'observe sur l'image seule, sans Traefik.

Les phases 1 et 2 — l'essentiel du CRS, toute la protection des requêtes — sont
intactes.

### Correctif : compilation au démarrage du pod

Le correctif est un `/v2` dans une ligne du Dockerfile amont, proposé en
[coreruleset/coraza-crs-docker#78](https://github.com/coreruleset/coraza-crs-docker/pull/78).
En attendant sa publication, le chart **recompile le binaire Caddy au démarrage
de chaque pod** plutôt que de publier une image :

```
initContainer caddy-plugin-build   image officielle caddy:<version>-builder
  └─ xcaddy build --with github.com/corazawaf/coraza-caddy/v2@v2.5.0
       └─ emptyDir caddy-bin  →  monté par-dessus /usr/bin/caddy dans `coraza`
```

Rien à construire, à publier, ni à maintenir : ni registre, ni chaîne de build,
ni image dérivée à re-tagger à chaque bump du CRS. Le binaire est le seul
artefact, il vit dans un `emptyDir` et meurt avec le pod.

**C'est un contournement assumé**, choisi parce que sa date de péremption est
proche. Ce qu'il coûte :

- **le démarrage du pod est allongé par la compilation** — **~2 min** mesurées
  caches vides sur `caddy:2.11.3-builder`, 1 CPU, binaire de 50 Mo. Sur un
  DaemonSet, chaque nœud repasse par là à chaque rollout, et il ne sert aucun
  trafic pendant ce temps ;
- **les nœuds doivent joindre le proxy de modules Go au démarrage.** Sans accès,
  l'initContainer échoue et le pod ne démarre pas — plus d'ingress sur ce nœud.
  L'échec est bruyant, il n'y a jamais de WAF silencieusement désactivé. Un
  `GOPROXY` interne se déclare en commentaire dans les values ;
- **la compilation consomme CPU et mémoire** au démarrage (`requests: 1 CPU,
  1 Gi`).

Deux épinglages, à garder alignés :

| épinglage | rôle |
|---|---|
| tag de `caddy:<version>-builder` | version de Caddy produite — doit suivre celle de l'image du sidecar |
| `@v2.5.0` sur le module | version de coraza-caddy |

Une garde de sortie vérifie `caddy build-info | grep coraza-caddy/v2` avant de
laisser le pod démarrer : sans le suffixe `/v2`, Go résoudrait le module v1
**sans la moindre erreur**, et on repartirait pour une phase 4 morte. C'est très
exactement le piège dans lequel l'image amont est tombée.

### Le jour où la correction amont est publiée

Reprendre le tag officiel dans les deux conteneurs, supprimer l'initContainer
`caddy-plugin-build`, le volume `caddy-bin` et ses deux montages. `NOTES.txt`
détecte l'absence de l'initContainer et redescend l'avertissement « phase 4
inactive » si le tag utilisé n'embarque pas encore le correctif.

Même chose si vous préférez à tout moment une image prête à l'emploi : il suffit
d'appliquer le patch de la PR #78 à `caddy/Dockerfile` amont, de publier l'image
où vous voulez et de retirer l'initContainer.

### Pourquoi pas l'API de build de Caddy

`caddyserver.com/api/download?p=github.com/corazawaf/coraza-caddy/v2` rend
exactement ce binaire en quelques secondes, sans compilation. Elle est écartée :
le service répond `Contact the Caddy team` (HTTP 200, 22 octets) aux agents
automatisés, et ne sert le binaire qu'aux clients se présentant comme un
navigateur ou `curl`. C'est un filtrage délibéré ; l'automatiser depuis chaque
démarrage de pod reviendrait à le contourner. Si le compromis vous intéresse,
c'est une question à poser à l'équipe Caddy, pas un `User-Agent` à falsifier.

### Pourquoi pas une image officielle

Il n'en existe pas qui convienne, et **aucun tag plus récent n'y changera rien** :
le défaut est dans la recette de construction, pas dans une version.

`coreruleset/coraza-crs-docker` épingle pourtant bien la bonne version. Son
`docker-bake.hcl` déclare

```hcl
variable "coraza-version" {
    # renovate: depName=corazawaf/coraza-caddy datasource=github-releases
    default = "v2.5.0"
}
```

et la passe au build comme `CORAZA_VERSION`. Mais `caddy/Dockerfile` ne déclare
aucun `ARG CORAZA_VERSION`, ne l'utilise nulle part, et construit
`--with github.com/corazawaf/coraza-caddy` — chemin de module **v1**. L'argument
est donc ignoré : Renovate met consciencieusement à jour un pin sans effet
(PR #37 → v2.1.0, PR #64 → v2.5.0) pendant que toutes les images publiées
embarquent v1.2.2. Vérifié sur le tag épinglé ici avec `caddy build-info`.

La correction amont tient en deux lignes — déclarer l'`ARG` et l'utiliser :

```dockerfile
ARG CORAZA_VERSION
RUN xcaddy build --with github.com/corazawaf/coraza-caddy/v2@${CORAZA_VERSION}
```

Construit en local : le binaire obtenu embarque bien coraza-caddy v2.5.0 et
coraza v3.7.0, soit exactement la version que le `docker-bake.hcl` amont croit
déjà livrer.

C'est cette correction qui fait l'objet de la PR #78, et c'est elle qui rendra
l'initContainer de compilation inutile.

Les autres variantes du même dépôt ne sont pas des porte-de-sortie : `nginx`
s'appuie sur `libcoraza` (moteur Go, hôte C) mais reste nginx, écarté par
l'architecture ; `apache` l'est pour la même raison. Les autres hôtes officiels
de Coraza — Envoy + `coraza-proxy-wasm`, HAProxy + `coraza-spoa` — remplaceraient
Caddy et le montage entier. `corazawaf/coraza-caddy` ne publie ni image ni
binaire (aucun asset de release), il n'y a donc pas non plus de binaire officiel
à injecter au démarrage.

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

## Limites connues

- **Règles à état non fiables.** Les collections `IP` / `SESSION` / `USER` de
  Coraza sont en mémoire par process, non partagées. DOS-protection et détection
  de brute force sont inopérantes en pratique, même avec l'épinglage local,
  puisque plusieurs nœuds voient du trafic. Le scoring d'anomalie CRS est
  per-transaction, donc intact.
- **Audit log** : les entrées `"transaction"` sont absentes avec l'image amont —
  même cause que la phase 4, et même correctif (voir ci-dessus).
- **Non testés** : WebSocket, SSE, gRPC, upload au-delà de
  `SecRequestBodyLimit`, renouvellement ACME réel, ajout d'un Ingress avec un
  nouveau host, kill du sidecar.
- Le patch du Caddyfile par initContainer est un contournement. Un point
  d'extension propre côté `coreruleset/coraza-crs-docker` serait préférable.
- **`DetectionOnly` neutralise aussi les `deny` explicites**, y compris ceux des
  règles maison : le smoke test de blocage ne renvoie 403 qu'avec
  `coraza.config.ruleEngine: "On"`. La règle est bien évaluée et journalisée
  dans les deux cas.
