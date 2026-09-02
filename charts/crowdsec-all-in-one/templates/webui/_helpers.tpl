{{/*
Expand the name of the chart.
*/}}
{{- define "crowdsec-all-in-one.name" -}}
{{- default .Chart.Name .Values.webui.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "crowdsec-all-in-one.fullname" -}}
{{- if .Values.webui.fullnameOverride }}
{{- .Values.webui.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.webui.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "crowdsec-all-in-one.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "crowdsec-all-in-one.labels" -}}
helm.sh/chart: {{ include "crowdsec-all-in-one.chart" . }}
{{ include "crowdsec-all-in-one.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "crowdsec-all-in-one.selectorLabels" -}}
app.kubernetes.io/name: {{ include "crowdsec-all-in-one.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Name of the Secret holding the OIDC client secret for WebUI SSO.
Resolves to .Values.webui.sso.oidc.existingSecret if set, else
<fullname>-sso (chart-managed).
*/}}
{{- define "webui.sso.secretName" -}}
{{- if .Values.webui.sso.oidc.existingSecret -}}
{{- .Values.webui.sso.oidc.existingSecret -}}
{{- else -}}
{{- printf "%s-sso" (include "crowdsec-all-in-one.fullname" .) -}}
{{- end -}}
{{- end }}
