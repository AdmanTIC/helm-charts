{{/*
Expand the name of the chart.
*/}}
{{- define "k8s-workload-rollout-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "k8s-workload-rollout-api.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
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
{{- define "k8s-workload-rollout-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "k8s-workload-rollout-api.labels" -}}
helm.sh/chart: {{ include "k8s-workload-rollout-api.chart" . }}
{{ include "k8s-workload-rollout-api.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "k8s-workload-rollout-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "k8s-workload-rollout-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "k8s-workload-rollout-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "k8s-workload-rollout-api.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "k8s-workload-rollout-api.credentials" -}}
{{- with .Values.credentials }}
{{- range . }}
{{ .token }} {{ .type }} {{ .namespace }} {{ .target }}
{{- end }}
{{- end }}
{{- end }}

{{- define "k8s-workload-rollout-api.repositories" -}}
{{- with .Values.helm.repositories }}
{{- range . }}
{{ .name }} {{ .url }}
{{- end }}
{{- end }}
{{- end }}