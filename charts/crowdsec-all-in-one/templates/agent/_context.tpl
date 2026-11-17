{{/*
Contexte « sous-chart crowdsec » pour les copies de templates upstream (templates/agent/).
Les copies référencent .Values.agent, .Values.config, .Values.image… exactement comme
upstream : on leur passe un contexte dont .Values est .Values.crowdsec, afin de garder
un diff minimal avec https://github.com/crowdsecurity/helm-charts/tree/main/charts/crowdsec/templates
(à re-synchroniser à chaque montée de version du sous-chart, cf. update-deps.sh).
*/}}
{{- define "crowdsec-all-in-one.agent.context" -}}
{{- if and .Values.agentPersistentCredentials.enabled .Values.crowdsec.agent.enabled }}
{{- fail "agentPersistentCredentials.enabled exige crowdsec.agent.enabled=false (sinon deux DaemonSets crowdsec-agent)" }}
{{- end }}
{{- if and .Values.agentPersistentCredentials.enabled .Values.crowdsec.tls.enabled .Values.crowdsec.tls.agent.tlsClientAuth }}
{{- fail "agentPersistentCredentials est inutile avec tls.agent.tlsClientAuth (pas d'enregistrement par mot de passe) : laissez l'agent upstream" }}
{{- end }}
{{- if not .Values.crowdsec.agent.persistentVolume.config.enabled }}
{{- fail "agentPersistentCredentials.enabled exige crowdsec.agent.persistentVolume.config.enabled=true" }}
{{- end }}
{{- if .Values.agentKubernetesAcquisition.enabled }}
{{- if .Values.agentPersistentCredentials.acquisition }}
{{- fail "agentKubernetesAcquisition remplace les sources fichiers : vider agentPersistentCredentials.acquisition" }}
{{- end }}
{{- if not .Values.agentKubernetesAcquisition.sources }}
{{- fail "agentKubernetesAcquisition.enabled exige au moins une entrée dans agentKubernetesAcquisition.sources" }}
{{- end }}
{{- range .Values.agentKubernetesAcquisition.sources }}
{{- if not (and .namespace .selector .program) }}
{{- fail "agentKubernetesAcquisition.sources : chaque entrée exige namespace, selector et program" }}
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
Values du sous-chart crowdsec vues par la copie locale : identiques à .Values.crowdsec, sauf
agent.acquisition qui est remplacé par agentPersistentCredentials.acquisition si non vide,
et les réglages imposés par agentKubernetesAcquisition quand il est activé
(même format + `containerName`, que le schéma JSON du sous-chart interdit dans
crowdsec.agent.acquisition). Usage : {{ $v := fromYaml (include "crowdsec-all-in-one.agent.values" .) }}
*/}}
{{- define "crowdsec-all-in-one.agent.values" -}}
{{- $v := deepCopy .Values.crowdsec }}
{{- with .Values.agentPersistentCredentials.acquisition }}
{{- $_ := set $v.agent "acquisition" . }}
{{- end }}
{{- if .Values.agentKubernetesAcquisition.enabled }}
{{- /* Acquisition par l'API Kubernetes : plus aucune source fichier ni hostPath, les sources
       `kubernetes` sont ajoutées à additionalAcquisition (rendu tel quel par acquis-configmap). */}}
{{- $extra := $v.agent.additionalAcquisition | default list }}
{{- range .Values.agentKubernetesAcquisition.sources }}
{{- $extra = append $extra (dict "source" "kubernetes" "namespace" .namespace "selector" .selector "labels" (dict "type" .program)) }}
{{- end }}
{{- $_ := set $v.agent "acquisition" list }}
{{- $_ := set $v.agent "additionalAcquisition" $extra }}
{{- $_ := set $v.agent "hostVarLog" false }}
{{- $_ := set $v.agent "serviceAccountName" (include "crowdsec-all-in-one.agent.kubernetesServiceAccount" .) }}
{{- $_ := set $v "container_runtime" "containerd" }}
{{- end }}
{{- toYaml $v }}
{{- end -}}


{{/*
ServiceAccount de l'agent quand il lit les logs par l'API Kubernetes (agentKubernetesAcquisition).
*/}}
{{- define "crowdsec-all-in-one.agent.kubernetesServiceAccount" -}}
{{- .Values.agentKubernetesAcquisition.serviceAccountName | default (printf "%s-agent" .Release.Name) }}
{{- end -}}
