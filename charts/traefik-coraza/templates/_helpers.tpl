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

{{/*
=============================================================================
 Normalisation des directives Sec* à valeur On/Off.

 YAML 1.1 — celui que parse Helm — résout `On`, `Off`, `Yes`, `No` en
 BOOLÉENS. Un `ruleEngine: On` non quoté dans les values arrive donc ici en
 `true`, ce qui rendait `SecRuleEngine true` dans la ConfigMap (refusé par
 Coraza) et faisait échouer les `eq …  "DetectionOnly"` de NOTES.txt sur une
 comparaison bool vs string — erreur de template visible dans ArgoCD.

 Ce helper reconvertit : true → On, false → Off, tout le reste inchangé.
 Toute valeur On/Off issue des values DOIT passer par ici.
=============================================================================
*/}}
{{- define "traefik-coraza.secFlag" -}}
{{- if kindIs "bool" . -}}
{{- ternary "On" "Off" . -}}
{{- else -}}
{{- toString . -}}
{{- end -}}
{{- end -}}

{{/*
Idem, mais pour les directives à trois états (SecRuleEngine, SecAuditEngine) :
normalise puis valide, plutôt que de laisser Coraza refuser sa configuration
au démarrage du sidecar — donc après le déploiement.
Argument : (dict "value" <valeur> "directive" <nom> "allowed" (list …))
*/}}
{{- define "traefik-coraza.secEnum" -}}
{{- $v := include "traefik-coraza.secFlag" .value -}}
{{- if not (has $v .allowed) -}}
{{- fail (printf "\n\n%s : valeur invalide %q (normalisée en %q).\nAttendu : %s.\n\nRappel : YAML 1.1 résout On/Off/Yes/No en booléens ; le chart les\nreconvertit en On/Off, mais toute autre valeur doit être écrite\nexactement, entre guillemets de préférence.\n" .directive .value $v (join ", " .allowed)) -}}
{{- end -}}
{{- $v -}}
{{- end -}}
