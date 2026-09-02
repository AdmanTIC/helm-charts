{{/*
Calico bouncer — labels propres à ce composant (indépendants du helper
`crowdsec-all-in-one.labels` qui force `app.kubernetes.io/name: webui`).
*/}}

{{- define "calicoBouncer.name" -}}
crowdsec-calico-bouncer
{{- end }}

{{- define "calicoBouncer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "calicoBouncer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: calico-bouncer
{{- end }}

{{- define "calicoBouncer.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "calicoBouncer.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: crowdsec-all-in-one
{{- end }}
