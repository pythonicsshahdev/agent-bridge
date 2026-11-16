#!/bin/sh
# Exercises files/prestart.sh with stubbed `aws` and `docker`, inside a throwaway container.
#   deploy/aws/tests/prestart_test.sh        (needs Docker)
set -eu
HERE=$(cd "$(dirname "$0")/.." && pwd)

docker run --rm -v "$HERE/files:/files:ro" alpine:3.20 sh -c '
set -eu
apk add --no-cache bash jq >/dev/null
mkdir -p /stub/bin /stub/params /stub/secrets /etc/a2a-bridge
cat >/stub/bin/aws <<STUB
#!/bin/bash
# aws ssm get-parameter --name X ... | aws secretsmanager get-secret-value --secret-id X ...
while [ \$# -gt 0 ]; do case "\$1" in --name|--secret-id) key=\$2; shift;; esac; shift; done
f=/stub/params/\$(basename "\$key"); [ -e "\$f" ] || f=/stub/secrets/\$(echo "\$key" | tr "/!:" "___")
[ -e "\$f" ] || { echo "stub: no such \$key" >&2; exit 254; }
cat "\$f"
STUB
printf "#!/bin/sh\necho docker \$*  >> /stub/docker.log\n" >/stub/bin/docker
chmod +x /stub/bin/aws /stub/bin/docker
export PATH=/stub/bin:$PATH

printf "server: {}\n" >/stub/params/agents-yaml
printf "ghcr.io/x/agent-bridge:v1" >/stub/params/image
printf "{\"BOOMI_API_TOKEN\":\"abc\",\"PARTNER\":\"1\"}" >/stub/secrets/a2a-bridge_env

write_boot() { printf "REGION=us-east-1\nSECRET_ID=a2a-bridge/env\nCONFIG_PARAM=/a2a-bridge/agents-yaml\nIMAGE_PARAM=/a2a-bridge/image\nPUBLIC_URL=https://a2a.example.com\nDB_SECRET_ARN=%s\nDB_HOST=%s\n" "$1" "$2" >/etc/a2a-bridge/boot.env; }
fail() { echo "FAIL: $1" >&2; exit 1; }

echo "1) sqlite mode"
write_boot "" ""
bash /files/prestart.sh
env=/run/a2a-bridge/env
grep -qx "A2A_PUBLIC_BASE_URL=https://a2a.example.com" $env || fail "public url"
grep -qx "DATABASE_URL=sqlite+aiosqlite:////data/a2a_bridge.db" $env || fail "sqlite url"
grep -qx "BOOMI_API_TOKEN=abc" $env || fail "secret passed through"
[ "$(tail -n 2 $env | head -n 1)" = "BOOMI_API_TOKEN=abc" ] || fail "secrets must come last so they can override defaults"
[ "$(stat -c %a $env)" = "600" ] || fail "env file must be 0600, got $(stat -c %a $env)"
[ "$(cat /run/a2a-bridge/image)" = "ghcr.io/x/agent-bridge:v1" ] || fail "image"
grep -q "server" /etc/a2a-bridge/agents.yaml || fail "config written"
grep -q "pull --quiet ghcr.io/x/agent-bridge:v1" /stub/docker.log || fail "image pulled"

echo "2) rds mode encodes the password"
printf "{\"username\":\"a2a\",\"password\":\"p@ss/w:rd?#\"}" >/stub/secrets/rds_db-1
write_boot "rds!db-1" "db.internal"
bash /files/prestart.sh
grep -qx "DATABASE_URL=postgresql+asyncpg://a2a:p%40ss%2Fw%3Ard%3F%23@db.internal:5432/a2a?ssl=require" $env || fail "rds url: $(grep DATABASE_URL $env)"
grep -q "sqlite" $env && fail "sqlite url must not appear in rds mode"

echo "3) a multi-line secret value is refused and nothing is written"
rm -f $env
printf "{\"K\":\"line1\\\\nline2\"}" >/stub/secrets/a2a-bridge_env
write_boot "" ""
if bash /files/prestart.sh 2>/stub/err; then fail "should have exited non-zero"; fi
grep -q "single-line" /stub/err || fail "error should explain"
[ ! -e $env ] || fail "env file must not exist after refusal"

echo "4) a non-object secret is refused"
printf "[1,2]" >/stub/secrets/a2a-bridge_env
if bash /files/prestart.sh 2>/dev/null; then fail "array secret accepted"; fi

echo "5) a missing parameter fails closed"
printf "{}" >/stub/secrets/a2a-bridge_env
rm /stub/params/image
if bash /files/prestart.sh 2>/dev/null; then fail "missing image parameter accepted"; fi
echo ALL PASSED
'
