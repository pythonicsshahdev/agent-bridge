#!/bin/bash
# Runs before every container start. Pulls config, secrets and the image; fails closed on any error.
set -euo pipefail
# shellcheck source=/dev/null
. /etc/a2a-bridge/boot.env

install -d -m 700 /run/a2a-bridge

aws ssm get-parameter --region "$REGION" --name "$CONFIG_PARAM" --query Parameter.Value --output text >/etc/a2a-bridge/agents.yaml.new
mv /etc/a2a-bridge/agents.yaml.new /etc/a2a-bridge/agents.yaml
chmod 644 /etc/a2a-bridge/agents.yaml

IMAGE=$(aws ssm get-parameter --region "$REGION" --name "$IMAGE_PARAM" --query Parameter.Value --output text)
printf '%s' "$IMAGE" >/run/a2a-bridge/image

app_env=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" --query SecretString --output text)
if ! jq -e 'type == "object" and all(.[]; (tostring | contains("\n")) | not)' <<<"$app_env" >/dev/null; then
  echo "secret $SECRET_ID must be a flat JSON object with single-line values" >&2
  exit 1
fi

umask 077
{
  echo "A2A_PUBLIC_BASE_URL=$PUBLIC_URL"
  echo "A2A_BRIDGE_LOG_FORMAT=json"
  if [ -n "${DB_SECRET_ARN:-}" ]; then
    creds=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$DB_SECRET_ARN" --query SecretString --output text)
    echo "DATABASE_URL=postgresql+asyncpg://$(jq -r '.username|@uri' <<<"$creds"):$(jq -r '.password|@uri' <<<"$creds")@${DB_HOST}:5432/a2a?ssl=require"
  else
    echo "DATABASE_URL=sqlite+aiosqlite:////data/a2a_bridge.db"
  fi
  # Secrets last, so they can override the defaults above.
  jq -r 'to_entries[] | "\(.key)=\(.value|tostring)"' <<<"$app_env"
} >/run/a2a-bridge/env

docker pull --quiet "$IMAGE"
