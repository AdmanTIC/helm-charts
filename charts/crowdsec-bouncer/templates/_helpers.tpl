{{- define "crowdsec-bouncer.labels" -}}
app.kubernetes.io/name: crowdsec-bouncer
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/*
L'HÔTE PUBLIC DU SERVEUR CAP, composé une fois pour tout le chart.
`captcha.host` est un GABARIT Go rendu ici par `tpl` : il porte `{{ "{{ .Values.domain }}" }}`,
et c'est ce qui fait qu'on n'écrit le domaine qu'à un seul endroit (values.yaml, § « Le
domaine »). Un hôte écrit en dur dans une surcharge de values passe aussi : `tpl` d'une
chaîne sans accolades rend cette chaîne.
Trois consommateurs, tous ici : le routeur de contournement (ingressroute.yaml), les deux
origines de files/captcha.html (via `captchaPage` ci-dessous), et les URL du plugin que
l'exploitant recopie depuis les exemples de values.yaml.
*/}}
{{- define "crowdsec-bouncer.captchaHost" -}}
{{- tpl .Values.captcha.host . -}}
{{- end -}}

{{/*
LES DEUX PAGES, JETONS SUBSTITUÉS. 🛑 PASSER PAR CES HELPERS, JAMAIS PAR `.Files.Get`.

🛑 CE N'EST PAS `tpl` — et ça ne peut pas l'être : ces fichiers sont des templates du
PLUGIN, ils portent `{{ "{{ .SiteKey }}" }}`, `{{ "{{ .FrontendJS }}" }}`, `{{ "{{ .ClientIP }}" }}`, que Helm
ne saurait pas résoudre (voir l'en-tête de configmap-page.yaml). Ce sont des SUBSTITUTIONS
DE CHAÎNE, `replace`, sur des jetons `__…__` choisis pour n'apparaître nulle part ailleurs :
les doubles accolades du plugin traversent intactes.

Les jetons, et ce qu'ils portent :
  __ORGANIZATION__      le nom de l'organisation (`organization`) — titre, `alt` du logo,
                        et la phrase qui dit au visiteur que le contrôle est chez nous ;
  __CAPTCHA_ORIGIN__    l'origine du serveur Cap (`https://<captcha.host>`), page captcha
                        seulement : le widget et son WebAssembly.
⚠️ `CAP_CUSTOM_WASM_URL`, dans la page captcha, n'est PAS un jeton à nous : c'est le nom de
   la variable que le widget Cap lit. Le mot « CUSTOM » qu'il contient ne se substitue pas.
*/}}
{{- define "crowdsec-bouncer.captchaPage" -}}
{{- .Files.Get "files/captcha.html"
      | replace "__CAPTCHA_ORIGIN__" (printf "https://%s" (include "crowdsec-bouncer.captchaHost" .))
      | replace "__ORGANIZATION__" .Values.organization -}}
{{- end -}}

{{- define "crowdsec-bouncer.banPage" -}}
{{- .Files.Get "files/ban.html" | replace "__ORGANIZATION__" .Values.organization -}}
{{- end -}}

{{/*
Nom du Middleware, SUFFIXÉ de l'empreinte des DEUX pages (captcha.html + ban.html). 🛑 C'EST CE QUI FAIT RELIRE LA PAGE.
Le plugin ne lit `captchaFilePath` qu'à la création du middleware (`GetTemplate` dans
`captcha.New()`) ; et Traefik ne reconstruit un middleware que si son SPEC change — une
annotation n'y suffit pas (essayé le 2026-09-03 : « configuration inchangée », ancienne
page servie). Un nom nouveau = un objet nouveau = une instance neuve du plugin qui lit le
fichier ; Argo CD supprime l'ancien (prune). L'IngressRoute référence le même nom, donc
bascule dans la même synchronisation.
🛑 L'EMPREINTE PORTE SUR LES PAGES RENDUES, pas sur les fichiers du dépôt : changer
`organization`, `domain` ou `captcha.host` change le texte servi, donc doit recréer le
middleware. Hacher `.Files.Get` laisserait le plugin sur son ancienne copie — le visiteur
verrait l'ancien nom, l'ancienne origine, et rien ne le dirait.
⚠️ Cela ne vide PAS le cache des décisions ni des captchas résolus : il est GLOBAL au plugin
(`var cache` dans pkg/cache), partagé par toutes les instances du processus Traefik. Une IP
qui a résolu le captcha reste laissée passer jusqu'à la fin de sa grâce, middleware recréé
ou non. Seul un redémarrage du pod le vide.
*/}}
{{- define "crowdsec-bouncer.middlewareName" -}}
{{- printf "%s-%s" .Values.middleware.name (printf "%s%s" (include "crowdsec-bouncer.captchaPage" .) (include "crowdsec-bouncer.banPage" .) | sha256sum | trunc 8) -}}
{{- end -}}
