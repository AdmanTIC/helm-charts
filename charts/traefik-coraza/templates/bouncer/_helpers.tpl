{{/*
Bouncers CrowdSec — helpers propres à ce composant.
Le bouncer est un PLUGIN de Traefik (maxlerebourg/crowdsec-bouncer-traefik-plugin),
du code exécuté DANS le processus Traefik : ce chart pose les objets qu'il
consomme (Middlewares, ConfigMap de pages, Secrets de clés), provisionne les
clés d'API auprès de la LAPI CrowdSec (copie du Secret + job d'enregistrement
posés dans le namespace CrowdSec), et vérifie au rendu que le plugin et les
montages sont bien déclarés côté subchart Traefik (bouncer/guard.yaml).
*/}}

{{- define "bouncer.name" -}}
crowdsec-bouncer
{{- end }}

{{- define "bouncer.anyEnabled" -}}
{{- or .Values.bouncer.ban.enabled .Values.bouncer.captcha.enabled -}}
{{- end }}

{{- define "bouncer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "bouncer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: crowdsec-bouncer
{{- end }}

{{- define "bouncer.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "bouncer.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Rendu d'une page depuis files/bouncer/, avec substitution des marqueurs
%%...%% depuis bouncer.branding. 🛑 Ne JAMAIS passer ces fichiers par `tpl` :
ils contiennent des doubles accolades ({{ .ClientIP }}, {{ .SiteKey }},
{{ .FrontendJS }}) destinées au PLUGIN, pas à Helm — .Files.Get les laisse
traverser intactes, c'est voulu.
Appel : include "bouncer.renderPage" (list $ "ban.html" $overrideHtml)
*/}}
{{- define "bouncer.renderPage" -}}
{{- $root := index . 0 -}}
{{- $file := index . 1 -}}
{{- $override := index . 2 -}}
{{- $bn := $root.Values.bouncer -}}
{{- $b := $bn.branding -}}
{{- $html := $override | default ($root.Files.Get (printf "files/bouncer/%s" $file)) -}}
{{- $logoBlock := "" -}}
{{- if $b.logo -}}
{{- $logoBlock = printf "<div class=\"logo\"><img src=\"%s\" alt=\"%s\"></div>" $b.logo ($b.siteName | default "logo") -}}
{{- end -}}
{{- $titlePrefix := "" -}}
{{- if $b.siteName -}}
{{- $titlePrefix = printf "%s - " $b.siteName -}}
{{- end -}}
{{- $html = $html
      | replace "%%LOGO_BLOCK%%" $logoBlock
      | replace "%%TITLE_PREFIX%%" $titlePrefix
      | replace "%%SITE_NAME%%" ($b.siteName | default "")
      | replace "%%PRIMARY_COLOR%%" $b.primaryColor
      | replace "%%BACKGROUND_COLOR%%" $b.backgroundColor
      | replace "%%SUPPORT_TEXT%%" $b.supportContact
      | replace "%%LANG%%" $b.lang
      | replace "%%CAP_API_BASE%%" (($bn.captcha.widget).apiBaseURL | default "" | trimSuffix "/")
      | replace "%%CAP_WASM_URL%%" (($bn.captcha.widget).wasmURL | default "")
-}}
{{- $html -}}
{{- end }}

{{- define "bouncer.banPage" -}}
{{- include "bouncer.renderPage" (list . "ban.html" .Values.bouncer.ban.overrideHtml) -}}
{{- end }}

{{- define "bouncer.captchaPage" -}}
{{- include "bouncer.renderPage" (list . "captcha.html" .Values.bouncer.captcha.overrideHtml) -}}
{{- end }}

{{/*
Suffixe optionnel du nom des Middlewares : les 8 premiers caractères de
l'empreinte des pages rendues. Le plugin ne lit banFilePath/captchaFilePath
qu'à la CRÉATION de l'instance du middleware, et Traefik ne reconstruit une
instance que si le SPEC de l'objet change : un nom nouveau = un objet nouveau
= une page relue. Contrepartie : le nom change à chaque modification de page,
donc ne référencer le middleware par son nom que depuis des objets de CE chart
(attach) — jamais en dur ailleurs. Désactivé par défaut (noms stables).
*/}}
{{- define "bouncer.pagesHashSuffix" -}}
{{- printf "-%s" (printf "%s%s" (include "bouncer.banPage" .) (include "bouncer.captchaPage" .) | sha256sum | trunc 8) -}}
{{- end }}

{{- define "bouncer.banMiddlewareName" -}}
{{- .Values.bouncer.ban.middlewareName }}{{ if .Values.bouncer.hashSuffix }}{{ include "bouncer.pagesHashSuffix" . }}{{ end -}}
{{- end }}

{{- define "bouncer.captchaMiddlewareName" -}}
{{- .Values.bouncer.captcha.middlewareName }}{{ if .Values.bouncer.hashSuffix }}{{ include "bouncer.pagesHashSuffix" . }}{{ end -}}
{{- end }}

{{/*
Options communes du plugin, partagées par les deux Middlewares (ban et captcha).
Prend (list $root <fichier de clé LAPI>).
*/}}
{{- define "bouncer.pluginCommon" -}}
{{- $root := index . 0 -}}
{{- $keyFile := index . 1 -}}
{{- $bn := $root.Values.bouncer -}}
enabled: "true"
crowdsecMode: {{ $bn.crowdsecMode | quote }}
updateMaxFailure: {{ $bn.updateMaxFailure | quote }}
crowdsecLapiScheme: {{ $bn.crowdsec.lapiScheme | quote }}
crowdsecLapiHost: {{ $bn.crowdsec.lapiHost | quote }}
crowdsecLapiKeyFile: {{ printf "%s/%s" $bn.secrets.mountPath $keyFile | quote }}
remediationStatusCode: {{ $bn.ban.statusCode | quote }}
banFilePath: {{ printf "%s/ban.html" $bn.pages.mountPath | quote }}
logLevel: {{ $bn.logLevel | quote }}
{{- if $bn.forwardedHeaders.trustedIPs }}
{{- if $bn.forwardedHeaders.headerName }}
forwardedHeadersCustomName: {{ $bn.forwardedHeaders.headerName | quote }}
{{- end }}
forwardedHeadersTrustedIPs:
  {{- range $bn.forwardedHeaders.trustedIPs }}
  - {{ . | quote }}
  {{- end }}
{{- end }}
{{- end }}
