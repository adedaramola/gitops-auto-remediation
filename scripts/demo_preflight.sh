#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"
TFVARS_FILE="${TFVARS_FILE:-$TF_DIR/terraform.tfvars}"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

tfvars_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 ~ "^[[:space:]]*" key "[[:space:]]*$" {
      value = $2
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      gsub(/^"/, "", value)
      gsub(/"$/, "", value)
      print value
    }
  ' "$TFVARS_FILE" | tail -n1
}

pass() {
  printf "PASS  %s\n" "$1"
}

fail() {
  printf "FAIL  %s\n" "$1" >&2
  exit 1
}

note() {
  printf "INFO  %s\n" "$1"
}

require_cmd terraform
require_cmd aws
require_cmd kubectl

[ -f "$TFVARS_FILE" ] || fail "Expected tfvars file at $TFVARS_FILE"

AWS_REGION="$(tfvars_value aws_region)"
AWS_REGION="${AWS_REGION:-us-east-1}"
ENABLE_MULTI_AGENT="$(tfvars_value enable_multi_agent)"
ENABLE_MULTI_AGENT="${ENABLE_MULTI_AGENT:-false}"
MODEL_PROVIDER="$(tfvars_value model_provider)"
MODEL_PROVIDER="${MODEL_PROVIDER:-bedrock}"
BEDROCK_MODEL_ID="$(tfvars_value bedrock_model_id)"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-anthropic.claude-3-haiku-20240307-v1:0}"
OPENAI_SECRET_ARN="$(tfvars_value openai_secret_arn)"

CLUSTER_NAME="$(terraform -chdir="$TF_DIR" output -raw cluster_name 2>/dev/null || true)"
WEBHOOK_URL="$(terraform -chdir="$TF_DIR" output -raw webhook_url 2>/dev/null || true)"

[ -n "$CLUSTER_NAME" ] || fail "Terraform output cluster_name is empty. Deploy the stack first."
[ -n "$WEBHOOK_URL" ] || fail "Terraform output webhook_url is empty. API Gateway is not ready."

KUBECONFIG_PATH="$(mktemp /tmp/gitops-sentinel-demo-kubeconfig.XXXXXX)"
MODEL_OUTPUT_PATH="$(mktemp /tmp/gitops-sentinel-model-check.XXXXXX)"
cleanup() {
  rm -f "$KUBECONFIG_PATH" "$MODEL_OUTPUT_PATH"
}
trap cleanup EXIT

aws eks update-kubeconfig \
  --region "$AWS_REGION" \
  --name "$CLUSTER_NAME" \
  --kubeconfig "$KUBECONFIG_PATH" >/dev/null
pass "EKS kubeconfig refreshed for $CLUSTER_NAME"

APP_SYNC="$(kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd get application demo-staging -o jsonpath='{.status.sync.status}' 2>/dev/null || true)"
APP_HEALTH="$(kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd get application demo-staging -o jsonpath='{.status.health.status}' 2>/dev/null || true)"
[ -n "$APP_SYNC" ] || fail "Argo CD application demo-staging is missing."
[ "$APP_SYNC" = "Synced" ] || fail "Argo CD application demo-staging is not Synced (current: ${APP_SYNC:-unknown})."
[ "$APP_HEALTH" = "Healthy" ] || fail "Argo CD application demo-staging is not Healthy (current: ${APP_HEALTH:-unknown})."
pass "Argo CD application demo-staging is Synced and Healthy"

POLICY_APP_SYNC="$(kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd get application platform-policies -o jsonpath='{.status.sync.status}' 2>/dev/null || true)"
POLICY_APP_HEALTH="$(kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd get application platform-policies -o jsonpath='{.status.health.status}' 2>/dev/null || true)"
[ -n "$POLICY_APP_SYNC" ] || fail "Argo CD application platform-policies is missing."
[ "$POLICY_APP_SYNC" = "Synced" ] || fail "Argo CD application platform-policies is not Synced (current: ${POLICY_APP_SYNC:-unknown})."
[ "$POLICY_APP_HEALTH" = "Healthy" ] || fail "Argo CD application platform-policies is not Healthy (current: ${POLICY_APP_HEALTH:-unknown})."
pass "Argo CD application platform-policies is Synced and Healthy"

AVAILABLE_REPLICAS="$(kubectl --kubeconfig "$KUBECONFIG_PATH" -n demo-staging get deployment demo-service -o jsonpath='{.status.availableReplicas}' 2>/dev/null || true)"
[ -n "$AVAILABLE_REPLICAS" ] || fail "Deployment demo-service is missing from namespace demo-staging."
[ "${AVAILABLE_REPLICAS:-0}" -ge 1 ] || fail "Deployment demo-service exists but has no available replicas."
pass "Deployment demo-service is available in demo-staging"

if [ "$ENABLE_MULTI_AGENT" = "true" ]; then
  if [ "$MODEL_PROVIDER" = "bedrock" ]; then
    if aws bedrock-runtime invoke-model \
      --region "$AWS_REGION" \
      --model-id "$BEDROCK_MODEL_ID" \
      --content-type application/json \
      --accept application/json \
      --body '{"anthropic_version":"bedrock-2023-05-31","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}' \
      "$MODEL_OUTPUT_PATH" >/dev/null 2>&1; then
      pass "Bedrock model access check passed for $BEDROCK_MODEL_ID"
    else
      fail "Bedrock model access failed for $BEDROCK_MODEL_ID. For a safe demo, either finish Bedrock account setup or set enable_multi_agent=false and re-apply."
    fi
  elif [ "$MODEL_PROVIDER" = "openai" ]; then
    [ -n "$OPENAI_SECRET_ARN" ] || fail "model_provider=openai but openai_secret_arn is empty."
    if aws secretsmanager get-secret-value \
      --region "$AWS_REGION" \
      --secret-id "$OPENAI_SECRET_ARN" \
      --query SecretString \
      --output text | grep -q '"api_key"'; then
      pass "OpenAI secret contains an api_key field"
      note "OpenAI network reachability is not checked here; this confirms the secret is present."
    else
      fail "OpenAI secret is missing or does not contain an api_key field."
    fi
  else
    fail "Unsupported model_provider value: $MODEL_PROVIDER"
  fi
else
  note "enable_multi_agent=false, so the safer single-agent demo path is selected."
fi

pass "Webhook is ready at $WEBHOOK_URL"
note "Safe trigger: make demo-alert WEBHOOK_URL=\"$WEBHOOK_URL\" WEBHOOK_SECRET=\"<your-webhook-secret>\""
