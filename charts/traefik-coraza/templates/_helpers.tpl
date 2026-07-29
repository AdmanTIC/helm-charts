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
Selector par défaut des pods Traefik du subchart.
Le chart officiel pose `app.kubernetes.io/instance: <release>-<namespace>`.
Surchargeable via `coraza.service.selector`.
*/}}
{{- define "traefik-coraza.traefikSelector" -}}
{{- if .Values.coraza.service.selector -}}
{{- toYaml .Values.coraza.service.selector -}}
{{- else -}}
app.kubernetes.io/name: traefik
app.kubernetes.io/instance: {{ printf "%s-%s" .Release.Name .Release.Namespace }}
{{- end -}}
{{- end -}}
