# crowdsec-bouncer — ce qu'il faut préparer à la main

Ce fichier ne décrit **que** ce qui n'est pas dans le chart : les objets à créer
ailleurs, les valeurs à renseigner, et le branding des deux pages HTML. Le
*pourquoi* de chaque réglage — et il y en a beaucoup, plusieurs
contre-intuitifs — est en commentaire dans [values.yaml](values.yaml) ; **les
lire avant d'allumer**.

> 🛑 Installé tel quel, ce chart pose la ConfigMap des pages, le Middleware et
> les deux IngressRoutes — mais le Middleware **met les routeurs en erreur** si
> le plugin Traefik n'est pas chargé, et le bouncer laisse tout passer si les
> clés ne sont pas montées. Les cinq points de la section suivante ne sont pas
> optionnels : ils conditionnent le fonctionnement, et l'un d'eux
> ([§ 4](#4-la-déclaration-du-plugin-dans-la-configuration-statique-de-traefik))
> peut couper l'entrée publique s'il est mal fait.

> 🛑 **Ce chart s'installe dans le namespace du pod Traefik**, et ce n'est pas
> négociable : c'est ce pod qui monte la ConfigMap des pages, et les IngressRoutes
> désignent le Service du sidecar Coraza **sans champ `namespace`** — donc dans le
> leur.

---

## À créer hors du chart

### 1. Le Secret des trois clés

Le chart **ne le crée pas et ne le nomme même pas** : il n'écrit que le chemin
où le pod Traefik le monte. Les trois clés sont lues comme des *fichiers*, pour
qu'aucune ne se retrouve en clair dans un `kubectl get middleware -o yaml`.

| Clé du Secret | Value qui en donne le nom | Où la trouver |
|---|---|---|
| `lapi-key` | `secrets.lapiKeyFile` | § 2 ci-dessous |
| `captcha-site-key` | `secrets.captchaSiteKeyFile` | § 3, clé **publique** du site Cap |
| `captcha-secret-key` | `secrets.captchaSecretKeyFile` | § 3, clé **secrète** du site Cap |

- Le nom de fichier **est** le nom de la clé : renommer une value impose de
  renommer la clé du Secret.
- À poser dans le même namespace que le pod Traefik — donc que ce chart.
- Chiffré (SopsSecret, Sealed Secrets, External Secrets…) : ce dépôt ne contient
  aucune clé en clair, et rien ici n'a besoin d'y changer ça.

### 2. La clé d'API du bouncer, côté CrowdSec

Elle se déclare à la main auprès de la LAPI, qui la rend une seule fois :

```bash
kubectl -n crowdsec exec deploy/crowdsec-lapi -- cscli bouncers add traefik-edge
```

🛑 **Sur le cluster qui héberge la LAPI.** Dans notre montage elle est sur un
autre cluster que ce Traefik — d'où `lapi.scheme: https` et un hôte public.

### 3. Le serveur Cap, et sa clé de site

Le fournisseur de captcha est [Cap](https://capjs.js.org) auto-hébergé. **Il
n'est pas dans ce chart**, et rien ici ne le déploie. Il lui faut :

- un **Valkey/Redis** (il y stocke défis et jetons) et un volume ;
- un **Ingress public** sur l'hôte de `captcha.host` — c'est le *navigateur du
  visiteur* qui doit le joindre, pas seulement le cluster ;
- `ENABLE_ASSETS_SERVER=true`, pour que le widget et son WebAssembly viennent de
  nos serveurs et non d'un CDN. ⚠️ Le serveur, lui, va les chercher sur jsdelivr
  une fois par jour (`CACHE_HOST` permet de couper ce lien aussi) ;
- une **clé de site créée à la main dans l'IHM de Cap** : elle n'est pas
  déclarative. Elle donne le couple clé publique / clé secrète du § 1.

### 4. La déclaration du plugin dans la configuration statique de Traefik

Le bouncer n'est pas un composant : c'est un **plugin Traefik**, du code
interprété par Yaegi dans le processus Traefik. Il se déclare en configuration
*statique*, donc dans les values du chart Traefik — **rien dans ce chart ne peut
le faire**, et il n'y est pas de série :

```yaml
traefik:
  experimental:
    plugins:
      # 🛑 Cette clé est l'alias : il doit dire le même mot que `middleware.pluginAlias`.
      bouncer:
        moduleName: github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin
        version: vX.Y.Z   # à épingler explicitement
```

🛑 **Ce que ça coûte, à savoir avant :** un plugin est **téléchargé et compilé
au démarrage**, depuis `plugins.traefik.io` puis GitHub. Un nœud d'entrée sans
sortie Internet au démarrage n'a **plus de Traefik**, donc plus d'entrée
publique. C'est la raison pour laquelle cette déclaration reste absente tant
qu'on ne s'en sert pas.

⚠️ Laisser `experimental.abortOnPluginFailure` à `false` (son défaut) : Traefik
démarre alors même si le plugin échoue, sans bouncer, au lieu de ne pas démarrer.

⚠️ Le rollout de Traefik qui applique ceci **redémarre les pods d'entrée**. Avec
un seul nœud d'entrée, c'est une coupure, pas un roulement.

### 5. Les deux montages dans le pod Traefik

Le plugin lit des **fichiers**, pas des objets Kubernetes. La ConfigMap des
pages et le Secret des clés doivent donc être montés dans le pod Traefik, aux
chemins exacts que ce chart écrit dans le Middleware :

```yaml
traefik:
  volumes:
    - name: crowdsec-captcha-page   # = `page.configMapName`
      mountPath: /captcha           # = `page.mountPath`
      type: configMap
    - name: <nom-du-Secret-du-§-1>
      mountPath: /secrets/crowdsec  # = `secrets.mountPath`
      type: secret
```

🛑 Ces trois valeurs sont **recopiées de part et d'autre** et rien ne vérifie
qu'elles s'accordent : un chemin qui diverge donne un plugin qui ne trouve ni sa
page ni ses clés, et le dit seulement dans les journaux de Traefik.

---

## Les values à renseigner

De série, quatre valeurs sont **vides ou locales à notre plateforme** : le chart
rend sans erreur, mais le captcha ne fonctionne pas.

| Value | De série | À y mettre |
|---|---|---|
| `domain` | `custom-organization.local` | **votre domaine**. Tous les hôtes et URL en dérivent |
| `organization` | `CrowdSec` | voir [Branding](#branding) |
| `lapi.host` | `""` | l'hôte **et le port** de la LAPI, ex. `'crowdsec-lapi.{{ .Values.domain }}:443'`. ⚠️ Pas de champ `port` séparé |
| `captcha.jsURL` | `""` | `'https://{{ include "crowdsec-bouncer.captchaHost" . }}/assets/widget.js'` |
| `captcha.validateURL` | `""` | `'https://{{ include "crowdsec-bouncer.captchaHost" . }}/<CLÉ_DE_SITE>/siteverify'` — la clé de site **publique**, en clair dans le chemin |
| `sonde.forwardedHeadersTrustedIPs` | `10.40.0.41/32` | l'IP **SNATée** du nœud qui porte la sonde, ou `sonde.enabled: false` |

⚠️ `domain`, `captcha.host`, `lapi.host` et les deux URL sont des **gabarits Go**
rendus par `tpl` : `{{ .Values.domain }}` y est résolu au rendu. Une valeur
écrite en dur passe aussi — mais une accolade non fermée fait échouer le rendu de
l'Application entière. `organization`, lui, n'est **pas** un gabarit : c'est du
texte recopié tel quel dans la page.

### Les valeurs recopiées ailleurs, que rien ne vérifie

Elles sont justes pour notre montage. Si le vôtre diffère, elles doivent suivre —
et une divergence est **silencieuse** :

| Value | Doit s'accorder avec |
|---|---|
| `middleware.pluginAlias` | la clé de `experimental.plugins.<alias>` ([§ 4](#4-la-déclaration-du-plugin-dans-la-configuration-statique-de-traefik)) |
| `page.configMapName`, `page.mountPath`, `secrets.mountPath` | les `volumes` du pod Traefik ([§ 5](#5-les-deux-montages-dans-le-pod-traefik)) |
| `attach.basePriority` | la priorité du catch-all du WAF (`catchAll.priority` de `traefik-coraza`). Trop basse, nos routeurs passent **derrière** et le bouncer est inerte sans que rien ne le dise |
| `attach.corazaService.name` / `.port` | le Service du sidecar Coraza (`coraza.service.name`, port `8090`) |
| `sonde.headerName` | le `headerName` du chart de la sonde |

### Les interrupteurs

| Value | De série | Effet |
|---|---|---|
| `middleware.enabled` | `true` | à `false`, le chart ne pose **que** la ConfigMap des pages |
| `attach.enabled` | `true` | pose les deux IngressRoutes. C'est le geste qui **allume** vraiment |
| `sonde.enabled` | `true` | la porte d'en-tête de confiance pour la sonde de vérification |

⚠️ **Commencer par un seul Ingress, jamais par l'entrypoint.** Un middleware
d'entrypoint mal résolu met en erreur **tous** les routeurs publics d'un coup —
et il intercepterait aussi le serveur de captcha, ce qui empêche le visiteur en
remédiation de charger le widget (erreur CORS constatée). C'est pour ça que
l'accrochage passe par des routeurs, pas par l'entrypoint.

---

## Branding

### Ce qui est dans les values

| Value | Où ça se voit |
|---|---|
| `organization` | le `<title>` des deux pages, l'`alt` du logo, et la phrase de la page captcha qui dit au visiteur que le contrôle tourne sur **nos** serveurs |

Le nom n'est écrit **nulle part** dans les fichiers HTML : ils portent un jeton
`__ORGANIZATION__`, substitué au rendu de la ConfigMap.

⚠️ **Accord grammatical :** la phrase de la page captcha se lit « Ce contrôle
tourne sur les serveurs de la **`organization`** ». Un nom qui ne prend pas
« la » demande de retoucher la phrase dans
[files/captcha.html](files/captcha.html) — la value seule n'y suffit pas.

⚠️ Ce n'est pas `tpl` mais un `replace` : les fichiers sont des templates du
*plugin* (`{{ .SiteKey }}`, `{{ .ClientIP }}`), que Helm ne saurait pas résoudre.
`CAP_CUSTOM_WASM_URL`, dans la page captcha, n'est pas un jeton à nous : c'est le
nom de la variable que lit le widget Cap, et le mot « CUSTOM » qu'il contient
n'est pas substitué.

### Ce qui reste à changer dans les fichiers

Assumé, pas un oubli : ces pages n'ont **aucune dépendance réseau** (hors le
widget Cap), tout y est en ligne.

| Élément | Où | Note |
|---|---|---|
| **Le logo** | [files/captcha.html:166](files/captcha.html), [files/ban.html:125](files/ban.html) | un PNG en base64 **en ligne**, 114 KiB décodés, le même dans les deux pages |
| **La couleur de charte** `#003da5` | 11 occurrences dans les deux fichiers | bordures, icônes, titres |
| **Le service à contacter** | [files/captcha.html:198](files/captcha.html), [files/ban.html:145](files/ban.html) | « Support technique fédéral » |
| **Les textes** | les deux fichiers | français, ton non accusatoire — voir ci-dessous |

⚠️ **Le ton de la page captcha est un choix, pas du remplissage.** Celui qui la
reçoit est le plus souvent innocent : il partage une adresse IP (4G, réseau
associatif, VPN d'entreprise) avec quelqu'un qui a scanné le site. La page
explique, ne reproche rien, et dit que le contrôle est local. La page de refus
suit la même règle.

### Deux conséquences de toute retouche de branding

- **Le nom du Middleware change.** Il est suffixé de l'empreinte des pages
  *rendues* : changer `organization`, `domain`, `captcha.host` ou un octet des
  fichiers recrée le Middleware — et c'est **voulu**, c'est la seule chose qui
  fasse relire les pages au plugin. Une IP qui a déjà résolu le captcha reste
  laissée passer jusqu'à la fin de sa grâce (`captcha.gracePeriodSeconds`) : le
  cache du plugin est global au processus.
- **La synchronisation prend ~90 s de plus** (`page.propagationDelaySeconds`) :
  un Job dort le temps que le kubelet pousse la ConfigMap dans les pods Traefik,
  avant que le Middleware qui la lit ne soit recréé. Sans cette attente, le
  plugin sert l'**ancienne** page jusqu'au changement suivant.

⚠️ La ConfigMap rendue pèse **~315 KiB**, dont l'essentiel est le logo dupliqué
dans les deux pages. La limite d'un objet etcd est de 1 MiB : un logo plus lourd
s'en approche vite.

---

## Vérifier avant d'installer

```bash
helm template crowdsec-bouncer . -n ingress | less
```

À regarder, dans cet ordre :

1. le `<title>` et l'`alt` des deux pages portent bien votre `organization` ;
2. `data-cap-api-endpoint` et `CAP_CUSTOM_WASM_URL` portent votre hôte Cap, et
   les `{{ .SiteKey }}` / `{{ .ClientIP }}` du plugin sont **intacts** ;
3. `crowdsecLapiHost`, `captchaCustomJsUrl` et `captchaCustomValidateUrl` ne sont
   pas des chaînes vides ;
4. le `Middleware` et l'`IngressRoute` nomment le **même** `crowdsec-captcha-<empreinte>`.
