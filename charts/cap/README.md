# cap — serveur de captcha auto-hébergé

[Cap](https://capjs.js.org) (Apache-2.0) en mode « standalone » : un serveur Bun
et un Valkey où il range défis, jetons, clés de site et sessions
d'administration.

Captcha à **preuve de travail** : pas d'énigme visuelle, pas de traceur, et
surtout **aucun script de Google ou de Cloudflare chargé dans le navigateur du
visiteur**. C'est le seul fournisseur du plugin bouncer CrowdSec pour Traefik
(`provider: custom`) qui tienne cette propriété.

## Chemins du trafic

```
navigateur ──GET  /assets/widget.js, /assets/cap_wasm_bg.wasm──▶ cap  (public)
navigateur ──POST /<siteKey>/challenge, /<siteKey>/redeem─────▶ cap  (public)
Traefik ─────POST /<siteKey>/siteverify (secret + response)───▶ cap  (interne, Service)
admin ───────GET  /, /server/*, /auth/*, /swagger ────────────▶ cap  (authentifié)
```

## Installation minimale

```yaml
cap:
  adminKey:
    existingSecret: cap-admin     # ou adminKey.value dans des values chiffrées
ingress:
  host: captcha.example.com
  tls:
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt-production
  admin:
    middlewares: ["sso-forwardauth-authelia@kubernetescrd"]
valkey:
  persistence:
    storageClass: ""              # vide → StorageClass par défaut
```

Le chart ne présume ni de l'hôte, ni du fournisseur d'authentification, ni de
la classe de stockage : tout est en values, sans défaut implicite pour ce qui
engage la sécurité.

## 🛑 La clé de site n'est pas déclarative

Elle **ne peut pas** être posée par le chart. Elle se crée dans l'IHM (ou par
`POST /server/keys` avec une session) et donne un couple clé publique / clé
secrète que **Cap ne redonne jamais**.

Conséquence directe : **perdre le Valkey, c'est perdre la clé de site**. Le
bouncer ne validerait alors plus aucun captcha, et tout visiteur en remédiation
« captcha » serait bloqué sans recours. D'où le PVC, et pas un `emptyDir` — la
garde de rendu refuse `valkey.persistence.enabled: false`, et le PVC porte
`helm.sh/resource-policy: keep`.

Les clés de site et les sessions d'administration vivent dans Valkey **sans
TTL**, contrairement aux défis et aux jetons. D'où aussi `maxmemory-policy
noeviction` : plutôt refuser une écriture que jeter une clé de site quand la
mémoire est pleine.

## Le point délicat : deux Ingress, un hôte

C'est ici que le déploiement se rate silencieusement.

| Ingress | Chemins | Authentification |
|---|---|---|
| `<release>-public` | `/` en **Prefix** | aucune — délibérément |
| `<release>-admin` | `/` en **Exact**, `/server`, `/auth`, `/swagger`, `/public` | `ingress.admin.middlewares` |

L'Ingress public n'est pas authentifié **par conception** : c'est un visiteur
banni qui doit prouver qu'il est humain, il n'a pas de compte.

> 🛑 **Sans priorité de routeur explicite, l'IHM est publique.** Traefik
> départage deux routeurs à la **longueur de leur règle**, et
> ``Host && PathPrefix(`/`)`` est plus longue que ``Host && Path(`/`)`` :
> l'Ingress public gagnerait la racine, donc la page de connexion de Cap. Les
> préfixes `/server` etc. gagneraient d'eux-mêmes, mais on ne laisse pas la
> racine au hasard d'un calcul de longueur. D'où
> `ingress.admin.priority: 100`, que la garde exige.

Le TLS n'est déclaré que sur l'Ingress public : même hôte, même certificat, un
seul Secret. Le redéclarer ferait deux demandes ACME concurrentes.

## Deux serrures sur l'IHM

`ADMIN_KEY` est le mot de passe de l'IHM, exigé par Cap (**12 caractères
minimum, sinon le processus refuse de démarrer** — le pod partirait en
CrashLoopBackOff sans autre indice). Il vient en plus de l'authentification de
l'Ingress d'administration, pas à sa place.

Aucune génération automatique, volontairement : `lookup` ne fonctionne pas sous
ArgoCD, qui rend avec `helm template` **sans accès au cluster**. Une clé
engendrée au rendu changerait à chaque synchronisation et invaliderait les
sessions en cours. Le chart exige donc une valeur explicite ou un
`existingSecret`.

## ⚠️ Dépendance de démarrage à jsdelivr

Avec `cap.assetsServer.enabled` (défaut), le serveur télécharge le widget et le
WebAssembly aux versions épinglées, les range dans Valkey et les sert lui-même.
Le visiteur ne joint donc aucun CDN — **sauf un cas** : le widget garde un repli
`pako` sur jsdelivr pour les navigateurs sans `DecompressionStream` (antérieurs
à 2023).

Les versions sont épinglées à dessein : `latest` ferait changer le code servi
aux visiteurs à chaque redémarrage du pod.

Côté consommateur, poser `window.CAP_CUSTOM_WASM_URL` pour que le WASM vienne
aussi de chez vous — la page captcha embarquée du chart `traefik-coraza` le fait
via `bouncer.captcha.widget.wasmURL`.

## Branchement sur traefik-coraza

Une fois la clé de site créée :

```yaml
bouncer:
  captcha:
    enabled: true
    provider: custom
    keys: { siteKey: "<publique>", secretKey: "<secrete>" }
    jsURL: https://captcha.example.com/assets/widget.js
    validateURL: http://cap.<namespace>.svc.cluster.local:3000/<siteKey>/siteverify
    widget:
      apiBaseURL: https://captcha.example.com
      wasmURL: https://captcha.example.com/assets/cap_wasm_bg.wasm
  attach:
    enabled: true
    middleware: captcha
    bypassHosts: ["captcha.example.com"]
```

Deux pièges à ce raccordement :

- **`bypassHosts` n'est pas optionnel.** Sans lui, l'hôte du captcha passe
  lui-même par le bouncer : un visiteur en remédiation ne peut jamais charger le
  widget qui le libérerait.
- **`validateURL` nomme la clé de site dans le chemin.** C'est ce qui fait
  refuser un jeton émis pour un autre site. Appel serveur à serveur : préférer
  l'URL interne au cluster.

Le chart `traefik-coraza` impose par ailleurs de déclarer le plugin bouncer et
ses volumes dans sa section `traefik:` — le subchart rend ces listes avec
`toYaml` sans `tpl`, rien ne peut les alimenter autrement. Voir son README.

## Gardes de rendu

Le chart **refuse de rendre** plutôt que de laisser partir un déploiement muet
mais cassé :

- aucun `ADMIN_KEY`, ou moins de 12 caractères, ou les deux sources à la fois ;
- `valkey.enabled: false` sans `valkey.externalUrl` ;
- `valkey.persistence.enabled: false` (perte des clés de site) ;
- `ingress.enabled` sans `ingress.host` ;
- IHM d'administration joignable sans authentification — Ingress admin
  désactivé, `middlewares` vide, ou `priority` nulle. Levable en connaissance de
  cause par `ingress.admin.allowUnauthenticated: true`.

## Limites connues

- **La remédiation « captcha » est décidée par les profils CrowdSec**
  (`profiles.yaml`), pas ici. Sans profil qui l'émette, le middleware se
  comporte comme un simple ban.
- **Un seul Valkey, sans réplication.** `strategy: Recreate` sur un volume RWO :
  toute mise à jour coupe brièvement la validation des captchas. Les défis en
  cours sont perdus, le visiteur rejoue.
- **Le serveur d'actifs a besoin d'un accès sortant** au démarrage. Sans lui le
  pod démarre, mais ne sert pas le widget.
