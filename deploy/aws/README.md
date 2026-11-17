# Deploying a2a-bridge on AWS (EC2)

One EC2 instance running the container, behind an Application Load Balancer with TLS and (optionally) WAF. SQLite on an encrypted EBS volume by default, or RDS Postgres with `use_rds = true`.

```
Internet / VPC ──► WAF ──► ALB (TLS 1.2+, 443) ──► EC2 :8000 (container, read-only, no capabilities)
                                                     │  ├── Secrets Manager   (env vars, DB password)
                                                     │  ├── SSM Parameter     (agents.yaml, image tag)
                                                     │  ├── CloudWatch Logs   (JSON, including the audit log)
                                                     │  └── EBS (SQLite)  or  RDS Postgres
                                                     └──► Boomi Execute API, your IdP, remote A2A agents (outbound 443 only)
```

## What has and has not been verified

| Verified | Not verified |
|---|---|
| `terraform validate` against the real AWS provider schema | A real `plan` or `apply` (needs your AWS account and credentials) |
| 8 offline `terraform test` cases on a mocked provider (locked-down defaults, internal vs public, RDS mode, certificate options, WAF toggles) | Instance boot on real Amazon Linux 2023 (EBS NVMe device lookup, `dnf` packages, systemd unit) |
| Boot script run in a container against stubbed AWS: env file layout and 0600 mode, password URL-encoding, refusal of bad secrets, fail-closed on missing parameters | WAF rule behaviour on your traffic |
| The production image, run against a real Postgres: readiness, auth, scopes, JSON audit logs, non-root | ALB behaviour end to end |

Run the first apply in a non-production account, and start with `waf_mode = "count"`.

## Before you apply

1. **A domain and certificate.** Either a Route 53 hosted zone (set `route53_zone_id`; the certificate is requested and validated for you) or an existing ACM certificate in the same region (`certificate_arn`).
2. **An image the instance can pull.** Tag a release (`git tag v0.2.0 && git push --tags`) and the `release` workflow publishes `ghcr.io/<owner>/agent-bridge:0.2.0` (the image tag has no `v`). The instance pulls anonymously, so the package must be public, or adapt `prestart.sh` for a private registry or ECR.
3. **A config file** based on [`agents.example.yaml`](agents.example.yaml). It is stored in an SSM parameter in plain text, so it holds no secrets: client keys are SHA-256 hashes (`a2a-bridge keygen`), and anything sensitive is a `${ENV}` reference.

## Deploy

```bash
cd deploy/aws
cp terraform.tfvars.example terraform.tfvars     # edit: domain, CIDRs, image tag, config path
terraform init
terraform plan
terraform apply

# Secrets go in out of band, so they never enter Terraform state (the output prints this command):
aws secretsmanager put-secret-value --secret-id a2a-bridge/env \
  --secret-string '{"PARTNER_OAUTH_SECRET":"..."}'
$(terraform output -raw restart)                 # pick the secrets up

curl https://a2a.example.com/readyz              # {"status":"ready"}
```

The container reads a flat JSON object of single-line values from `a2a-bridge/env` and exposes each key as an environment variable. The instance also sets `DATABASE_URL` and `A2A_PUBLIC_BASE_URL`; your config refers to them.

## Public or private?

| | `internal = false` (default) | `internal = true` |
|---|---|---|
| Reachable from | the internet, limited by `allowed_cidrs` | the VPC and anything peered or connected to it |
| Use when | Boomi cloud services or external A2A clients call you | only Boomi runtimes or apps inside your network call you |
| Do | restrict `allowed_cidrs` to known networks, use WAF, use per-client scoped keys or OIDC | `allowed_cidrs` defaults to the VPC CIDR |

## Operating it

| Task | How |
|---|---|
| Change agents or auth config | edit `agents.yaml`, `terraform apply`, then run the `restart` output |
| Roll out a new image | change `container_image`, `terraform apply`, restart. Pin a version or digest, not `latest`. |
| Add or rotate a client key | `a2a-bridge keygen`; add the new hash next to the old one, apply and restart, move the client, then remove the old hash |
| Shell on the instance | `terraform output -raw shell` (Session Manager; there is no SSH and no port 22) |
| Logs | CloudWatch log group `/a2a-bridge`. Every request is one JSON audit line. Insights example: `filter logger = "a2a_bridge.audit" and status >= 400 \| stats count() by principal, status` |
| Backups | EBS: snapshot the data volume (Data Lifecycle Manager). RDS: automated backups are on (7 days) and deletion protection is enabled. |
| Tear down a test stack | set `deletion_protection = false`, apply, then `terraform destroy` |

## The control plane in production

If you enable `server.admin`, the page and API are served by the same ALB listener as your agents. For an internet-facing ALB, add a WAF rule or listener rule that only allows `/admin*` from your office or VPN CIDRs, and keep the `admin:*` clients to a few people.

## Settings that matter

- **ALB idle timeout is 330 seconds.** `/invoke` can wait up to 300 seconds for an agent. The ALB default of 60 would cut those calls off. Keep it above `server.facade.max_wait_seconds`.
- **`forwarded_allow_ips: "*"`** in the config makes the proxy trust `X-Forwarded-For` from any peer. That is safe only because the instance security group admits nothing but the load balancer. Do not copy it to a setup where the app port is reachable directly.
- **The instance has a public IP for outbound traffic only** (image pulls, Boomi, your identity provider). No security group allows inbound from the internet to it. For a fully private instance, add NAT or VPC endpoints.
- **IMDSv2 only, hop limit 1,** so a process inside the container cannot reach the instance role's credentials.
- **WAF:** the managed rules run in `count` or `block` mode, and the 8 KB body-size rule is set to count because A2A messages can be larger. The proxy enforces its own `max_request_bytes`.

## Limits

- **One instance is not highly available.** If it fails, calls fail until it is replaced. The EBS volume survives, since it is a separate resource. Move to RDS and two instances behind the same ALB when you need availability, after adding a shared rate limit at the WAF, because the proxy's limiter is per process.
- **SQLite on one volume** is fine for a pilot. Use `use_rds = true` for anything you cannot afford to lose.
- **Cost** is roughly an ALB plus a small instance plus WAF, and more with RDS. Check the AWS pricing calculator for your region.
