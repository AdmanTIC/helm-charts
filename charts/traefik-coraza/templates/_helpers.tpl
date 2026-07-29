{{/*
Labels communs aux objets créés par ce chart.
Volontairement distincts de ceux du subchart Traefik : en mode
`coraza.service.mode: clusterIP`, le selector du Service doit viser les pods
Traefik, jamais ces labels-ci.
*/}}
{{- define "traefik-coraza.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: waf
{{- end -}}

{{/*
=============================================================================
 Réplique des helpers de nommage du subchart Traefik (41.0.2), pour pouvoir
 raisonner depuis le parent sur les noms et labels qu'il va produire.
 Les values d'un subchart ne passant pas par le moteur de template, c'est le
 seul moyen de vérifier la cohérence au rendu.
 À revérifier contre traefik/templates/_helpers.tpl à chaque bump du subchart.
=============================================================================
*/}}

{{/* traefik.namespace */}}
{{- define "traefik-coraza.tfNamespace" -}}
{{- .Values.traefik.namespaceOverride | default .Release.Namespace -}}
{{- end -}}

{{/* traefik.name → app.kubernetes.io/name, qui fait partie du SÉLECTEUR */}}
{{- define "traefik-coraza.tfNameLabel" -}}
{{- .Values.traefik.nameOverride | default "traefik" -}}
{{- end -}}

{{/* traefik.instance-name → app.kubernetes.io/instance, aussi dans le SÉLECTEUR */}}
{{- define "traefik-coraza.tfInstanceLabel" -}}
{{- .Values.traefik.instanceLabelOverride | default (printf "%s-%s" .Release.Name (include "traefik-coraza.tfNamespace" .)) -}}
{{- end -}}

{{/* traefik.fullname → nom des objets (DaemonSet, Service, ServiceAccount, …) */}}
{{- define "traefik-coraza.tfFullname" -}}
{{- if .Values.traefik.fullnameOverride -}}
{{- .Values.traefik.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "traefik-coraza.tfNameLabel" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Ce que le chart Traefik produirait en standalone sous le release
`standaloneNaming.expectedRelease`, dans le même namespace.
*/}}
{{- define "traefik-coraza.expectedInstanceLabel" -}}
{{- printf "%s-%s" .Values.standaloneNaming.expectedRelease (include "traefik-coraza.tfNamespace" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "traefik-coraza.expectedFullname" -}}
{{- $release := .Values.standaloneNaming.expectedRelease -}}
{{- $name := include "traefik-coraza.tfNameLabel" . -}}
{{- if contains $name $release -}}
{{- $release | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" $release $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Selector des pods Traefik, pour le Service coraza en mode clusterIP.
Dérivé des mêmes helpers que ci-dessus : il suit automatiquement
nameOverride / instanceLabelOverride. Surchargeable via `coraza.service.selector`.
*/}}
{{- define "traefik-coraza.traefikSelector" -}}
{{- if .Values.coraza.service.selector -}}
{{- toYaml .Values.coraza.service.selector -}}
{{- else -}}
app.kubernetes.io/name: {{ include "traefik-coraza.tfNameLabel" . }}
app.kubernetes.io/instance: {{ include "traefik-coraza.tfInstanceLabel" . }}
{{- end -}}
{{- end -}}
