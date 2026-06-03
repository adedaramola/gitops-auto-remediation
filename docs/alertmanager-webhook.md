# Alertmanager Webhook Setup

Point Alertmanager to the Terraform output `webhook_url`.

Example (values.yaml for kube-prometheus-stack):
```yaml
alertmanager:
  config:
    receivers:
      - name: sentinel-webhook
        webhook_configs:
          - url: "<WEBHOOK_URL_FROM_TERRAFORM>"
            send_resolved: true
    route:
      receiver: sentinel-webhook
```

The webhook sends JSON to the Signal Collector Lambda. It deduplicates, enriches with Prometheus/k8s context, writes a signal bundle to S3, and emits a `SignalBundled` EventBridge event that triggers the Decision Engine.

## MVP demo trigger

If you want to demo the system without waiting for a real Alertmanager event, use the sample payload in [docs/demo-alert.json](demo-alert.json):

```bash
make demo-alert \
  WEBHOOK_URL="<webhook_url_from_terraform>" \
  WEBHOOK_SECRET="<webhook_secret_if_configured>"
```
