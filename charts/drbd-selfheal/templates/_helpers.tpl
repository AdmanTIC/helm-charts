{{- /*
Namespace de dépôt de TOUS les objets du chart, et namespace où le balayeur va chercher les
pods satellites — c'est nécessairement le même : le droit `pods/exec` est porté par un `Role`
namespacé, jamais par un `ClusterRole`.

Vide dans les values = namespace de la release.
*/}}
{{- define "drbd-selfheal.namespace" -}}
{{ .Values.namespace | default .Release.Namespace }}
{{- end -}}

{{- /*
Placement commun au balayeur et à son puits. Vide par défaut : le chart ne présume aucune
convention de labels ni de taints de nœuds. Les deux pods joignent les satellites par l'API
Kubernetes, ils n'ont donc aucune adhérence à un nœud de stockage.
*/}}
{{- define "drbd-selfheal.placement" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
