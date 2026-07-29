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

1. **Tag de l'image Coraza** — `coraza.image` vaut `…:TAG` et doit être épinglé
   avant tout `helm install`. `latest` est exclu : l'initContainer patche un
   template livré par l'image, une dérive silencieuse casserait le patch au pire
   moment.

2. **UID du sidecar** — le `podSecurityContext` du chart impose
   `runAsUser: 65532`, qui n'est pas forcément propriétaire de `/opt/coraza`, où
   `/entrypoint.sh` écrit. Relever l'UID réel :

   ```bash
   docker run --rm --entrypoint sh ghcr.io/coreruleset/coraza-crs:TAG -c 'ls -ldn /opt/coraza /opt/coraza/config'
   ```

   Si le sidecar échoue sur un « permission denied », ajouter un `runAsUser`
   explicite dans `traefik.deployment.additionalContainers[0].securityContext`.
   Contrainte : la valeur doit rester différente de 0 (`runAsNonRoot: true` au
   niveau du pod).

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

## Le piège de l'ancre `&corazaImage`

`values.yaml` définit l'image une fois avec une ancre YAML et l'aliase dans
l'initContainer et le sidecar. C'est délibéré : une divergence de tag produirait
un template Caddyfile patché issu d'une version différente de celle qui le
consomme.

Conséquence à connaître : **les ancres sont résolues au parse du fichier, pas au
merge Helm**. Un `--set coraza.image=…` ou un `-f` externe ne change *que* la
valeur documentaire `coraza.image`, pas les deux conteneurs. Pour changer de tag,
éditer `values.yaml`, ou surcharger explicitement les trois emplacements.

Même logique pour `coraza.configMapName` : le nom du ConfigMap est référencé
littéralement dans `traefik.deployment.additionalVolumes`, parce que les values
d'un subchart ne passent pas par le moteur de template. Changer l'un impose de
changer l'autre.

## Rodage des faux positifs

Le chart démarre en `SecRuleEngine DetectionOnly` et doit y rester jusqu'à la fin
du rodage. Les exclusions se posent dans `coraza.extraRules` :

```yaml
coraza:
  extraRules: |
    SecRule REQUEST_URI "@beginsWith /api/ingest" \
        "id:1000,phase:1,pass,nolog,ctl:removeById=920420"
```

Règles de rédaction :

- IDs dans la plage **1000–1999** uniquement.
- `ctl:removeById` et non `SecRuleRemoveById` : `config.d` est chargé *avant* le
  CRS, donc une directive de chargement n'a encore rien à retirer, alors que
  `ctl:` agit à l'exécution.
- Regex **RE2** : pas de `(?!)`, `(?<!)`, `(?=)`, `(?<=)`, pas de backreference
  `\1`. `(?i)`, `\d`, `\w`, `\b` sont supportés.
- Pas d'`Include` du CRS : le template le charge déjà, un doublon donne
  `there is another rule with id 900000`.
- Le niveau de paranoïa ne se règle pas ici : `config.d` est inclus *avant*
  `crs-setup.conf`, un `setvar:tx.blocking_paranoia_level` y serait écrasé.
  Passer par les variables d'environnement (`overrides/`, inclus en dernier).

## Limites connues

- **Règles à état non fiables.** Les collections `IP` / `SESSION` / `USER` de
  Coraza sont en mémoire par process, non partagées. DOS-protection et détection
  de brute force sont inopérantes en pratique, même avec l'épinglage local,
  puisque plusieurs nœuds voient du trafic. Le scoring d'anomalie CRS est
  per-transaction, donc intact.
- **Audit log** : les entrées `"transaction"` n'ont pas été retrouvées en
  cluster. Bloquant à terme : c'est l'exigence qui a fait
  écarter le plugin WASM de Traefik.
- **Non testés** : WebSocket, SSE, gRPC, upload au-delà de
  `SecRequestBodyLimit`, renouvellement ACME réel, ajout d'un Ingress avec un
  nouveau host, kill du sidecar.
- Le patch du Caddyfile par initContainer est un contournement. Un point
  d'extension propre côté `coreruleset/coraza-crs-docker` serait préférable.
