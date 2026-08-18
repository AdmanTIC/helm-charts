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
   `helm install`, **aux trois endroits** (voir la section suivante). `latest` est
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

## Le tag de l'image est écrit trois fois

Dans `traefik.deployment.initContainers` (patch du Caddyfile) et deux fois dans
`traefik.deployment.additionalContainers` : le sidecar `coraza` qui consomme le
template patché, et `coraza-reload` dont le binaire `caddy` réadapte le Caddyfile
pour l'API admin. Les trois doivent porter le **même** tag, sinon l'initContainer
patche un template issu d'une version différente de celle qui le lit.
`NOTES.txt` affiche les trois images au déploiement et signale une divergence.

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

Voie normale : éditer les trois tags dans `values.yaml` et bumper `version` dans
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

### Correctif

[image/Dockerfile](image/Dockerfile) reconstruit le seul binaire Caddy avec
`github.com/corazawaf/coraza-caddy/v2` et le recopie dans l'image amont. Tout le
reste (CRS, entrypoint, templates, UID) reste celui de l'amont.

```bash
docker build -t <registry>/coraza-crs-v2:4.28.0-<date> charts/traefik-coraza/image
docker push  <registry>/coraza-crs-v2:4.28.0-<date>
```

Puis, dans `values.yaml` : reporter le tag aux **trois** emplacements
(initContainer, sidecar, reloader) et passer `coraza.imageSupportsPhase4` à
`true` — ce drapeau ne pilote que l'avertissement affiché par `NOTES.txt`, pour
que le chart cesse d'annoncer un gain qu'il n'a pas.

Les `CORAZA_DEFAULT_PHASE{1,2}_ACTION` déjà posées sur le sidecar sont
**indispensables** avec un coraza récent : l'image injecte `tag:'coraza'` dans
le `SecDefaultAction` de `crs-setup.conf`, et les versions récentes refusent les
actions de métadonnée à cet endroit — le conteneur ne démarre pas du tout.

La correction amont tient en un `/v2` dans le Dockerfile de
`coreruleset/coraza-crs-docker`.

## Rechargement à chaud de la configuration

Modifier `coraza.config` ou `coraza.extraRules` met à jour la ConfigMap, donc le
fichier monté — mais **Coraza ne relit ses `include` qu'au provisioning du
module Caddy**. Sans mécanisme dédié, un `helm upgrade` ne change rien tant que
le pod n'est pas recréé, ce qui coupe le trafic du nœud sur un DaemonSet.

Le conteneur `coraza-reload` (dans `traefik.deployment.additionalContainers`)
supprime ce redémarrage :

1. il surveille l'empreinte de `/opt/coraza/config.d/*.conf` ;
2. au changement, il appelle l'API admin de Caddy — `127.0.0.1:2019`, atteignable
   parce que les conteneurs d'un pod partagent la pile réseau, et jamais exposée
   hors du pod ;
3. `caddy reload --force`. Le `--force` n'est pas optionnel : Caddy compare le
   **JSON adapté**, pas les fichiers inclus. Le Caddyfile n'ayant pas bougé, sans
   `--force` il répond `config is unchanged` et ne recharge rien.

Le rechargement est gracieux : les connexions en cours sont préservées. Si la
nouvelle configuration est invalide, le rechargement est refusé, le WAF continue
sur la précédente et la boucle réessaie — l'échec est journalisé,
`kubectl logs … -c coraza-reload`.

Latence : propagation kubelet de la ConfigMap (~1 min) + l'intervalle de la
boucle (10 s).

C'est aussi la raison du volume `caddy-etc` : le reloader a besoin du Caddyfile
**rendu** par l'entrypoint du sidecar, et les systèmes de fichiers des
conteneurs d'un pod sont cloisonnés.

**Pour s'en passer** — infrastructures où un changement de configuration doit
passer par un redémarrage explicite et daté : supprimer le conteneur
`coraza-reload` et le volume `caddy-etc` des values. `NOTES.txt` le détecte et
rappelle alors la commande de redémarrage. Un `helm upgrade` seul ne recréera
pas les pods : il faut un
`kubectl -n <ns> rollout restart daemonset/traefik`.

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
