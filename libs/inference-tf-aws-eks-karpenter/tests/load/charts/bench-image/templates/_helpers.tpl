{{/*
chart.image — the shared image-ref helper. Renders registry/repository:tag,
or repository@digest when the onboard override sets tag to "@sha256:...". Copy verbatim
into every consumer chart; a hardcoded ref bypasses the onboard rewrite and has no pull
path on the endpoints-only VPC.
*/}}
{{- define "chart.image" -}}
{{- $sep := ":" -}}
{{- if hasPrefix "@" .tag -}}{{- $sep = "" -}}{{- end -}}
{{- if .registry -}}
{{ .registry }}/{{ .repository }}{{ $sep }}{{ .tag }}
{{- else -}}
{{ .repository }}{{ $sep }}{{ .tag }}
{{- end -}}
{{- end -}}
