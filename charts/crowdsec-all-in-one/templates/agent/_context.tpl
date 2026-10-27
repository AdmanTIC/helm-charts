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
{{- end -}}

{{/*
Values du sous-chart crowdsec vues par la copie locale : identiques à .Values.crowdsec, sauf
agent.acquisition qui est remplacé par agentPersistentCredentials.acquisition si non vide
(même format + `containerName`, que le schéma JSON du sous-chart interdit dans
crowdsec.agent.acquisition). Usage : {{ $v := fromYaml (include "crowdsec-all-in-one.agent.values" .) }}
*/}}
{{- define "crowdsec-all-in-one.agent.values" -}}
{{- $v := deepCopy .Values.crowdsec }}
{{- with .Values.agentPersistentCredentials.acquisition }}
{{- $_ := set $v.agent "acquisition" . }}
{{- end }}
{{- toYaml $v }}
{{- end -}}
