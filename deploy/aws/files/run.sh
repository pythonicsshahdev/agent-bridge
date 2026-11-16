#!/bin/bash
# Starts the container with a read-only filesystem, no capabilities and no privilege escalation.
set -euo pipefail
# shellcheck source=/dev/null
. /etc/a2a-bridge/boot.env
exec docker run --rm --name a2a-bridge \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 \
  --env-file /run/a2a-bridge/env \
  -v /etc/a2a-bridge:/etc/a2a-bridge:ro -v /data:/data \
  -p 8000:8000 \
  --log-driver awslogs --log-opt "awslogs-region=$REGION" --log-opt "awslogs-group=$LOG_GROUP" \
  "$(cat /run/a2a-bridge/image)"
