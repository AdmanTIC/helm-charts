{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "short_name" -}}
{{- $tplDir := base (dir $.Template.Name) -}}
{{- if eq "templates" $tplDir -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $tplDir | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "chart_name_version" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "chart_labels" -}}
app: {{ include "short_name" $ }}
chart: {{ .Chart.Name }}
{{ include "chart_labels_for_network_policy" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "chart_labels_for_network_policy" -}}
release: {{ .Release.Name }}
{{- end -}}