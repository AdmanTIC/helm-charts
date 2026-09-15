{{/*
Nom de base. `cap` par défaut, pour que l'URL de service reste
`cap.<namespace>.svc.cluster.local:3000` — celle que les values du bouncer
traefik-coraza nomment dans `bouncer.captcha.validateURL`.
*/}}
{{- define "cap.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cap.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "cap.valkeyFullname" -}}
{{- printf "%s-valkey" (include "cap.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Labels communs. `part-of: cap` relie les deux charges l'une à l'autre.
*/}}
{{- define "cap.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/part-of: {{ include "cap.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
{{- end -}}

{{/*
Sélecteurs. 🛑 IMMUABLES sur un Deployment : ne jamais y ajouter de label
porteur de version ou de release après un premier déploiement.
*/}}
{{- define "cap.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cap.fullname" . }}
{{- end -}}

{{- define "cap.valkeySelectorLabels" -}}
app.kubernetes.io/name: {{ include "cap.valkeyFullname" . }}
{{- end -}}

{{/*
Nom du Secret qui porte ADMIN_KEY, et la clé qu'on y lit.
*/}}
{{- define "cap.adminSecretName" -}}
{{- $a := .Values.cap.adminKey -}}
{{- if $a.existingSecret -}}{{ $a.existingSecret }}{{- else -}}{{ printf "%s-admin" (include "cap.fullname" .) }}{{- end -}}
{{- end -}}

{{- define "cap.adminSecretKey" -}}
{{- $a := .Values.cap.adminKey -}}
{{- if $a.existingSecret -}}{{ $a.existingSecretKey }}{{- else -}}adminKey{{- end -}}
{{- end -}}

{{/*
URL du Valkey consommée par Cap : l'instance du chart, ou celle fournie.
*/}}
{{- define "cap.redisUrl" -}}
{{- if .Values.valkey.enabled -}}
redis://{{ include "cap.valkeyFullname" . }}:6379
{{- else -}}
{{ .Values.valkey.externalUrl }}
{{- end -}}
{{- end -}}
