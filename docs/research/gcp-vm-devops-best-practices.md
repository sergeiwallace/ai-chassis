---
title: GCP VM DevOps Best Practices — Rootless Podman, CI/CD, Secrets, Zero-Downtime
category: research
tags: [gcp, devops, podman, systemd, cicd, secrets, github-actions]
status: draft
source: "Genericized from prior personal research; external claims cited inline."
template_version: "research-1.1.0"
---
<!-- doc:region name="context" kind="immutable" -->

## Context

(none — context not recorded in original doc)

<!-- /doc:region name="context" -->

<!-- doc:region name="body" kind="replaceable" -->


# GCP VM DevOps Best Practices

Best-practice guidance for running a containerized web app on a GCP Compute Engine
VM (Ubuntu 24.04, rootless Podman) behind a load balancer. Covers moving the app off
a personal user account into a dedicated service user, CI/CD, GCP Secret Manager
integration, and zero-downtime deploys. Written for a small deployment (a handful to a
few dozen users on a single VM); scale-out notes are called out where relevant.

---

## Table of Contents

- [1. Dedicated Service User for Rootless Podman](#1-dedicated-service-user-for-rootless-podman)
- [2. CI/CD: GitHub Actions → GCP VM Deploy](#2-cicd-github-actions--gcp-vm-deploy)
  - [Why WIF over service account JSON key](#why-wif-over-service-account-json-key)
  - [Setup](#setup)
  - [Alternative: Cloud Build + Artifact Registry](#alternative-cloud-build--artifact-registry)
- [3. Secret Management: GCP Secret Manager](#3-secret-management-gcp-secret-manager)
  - [GCP setup](#gcp-setup)
  - [Injection pattern: ExecStartPre + EnvironmentFile on tmpfs](#injection-pattern-execstartpre--environmentfile-on-tmpfs)
- [4. Systemd: User Service vs System Service](#4-systemd-user-service-vs-system-service)
  - [Quadlet (preferred over podman generate systemd)](#quadlet-preferred-over-podman-generate-systemd)
- [5. Zero-Downtime Deploys with Podman](#5-zero-downtime-deploys-with-podman)
  - [Architecture](#architecture)
  - [Deploy script](#deploy-script)
- [Summary](#summary)

## 1. Dedicated Service User for Rootless Podman

**Common anti-pattern:** the app runs under a personal user account (e.g. a named
developer's uid). If that account is offboarded, renamed, or has its home directory
changed, the app breaks.

**Recommendation:** run under a dedicated non-login system user (e.g. `myapp`) with a
home directory in `/var/lib/`.

```bash
# Create non-login system user
sudo useradd -r -m -d /var/lib/myapp -s /usr/sbin/nologin myapp

# Assign subuid/subgid ranges — required for rootless Podman user namespace mapping
sudo usermod --add-subuids 200000-265535 --add-subgids 200000-265535 myapp

# Enable linger — keeps user's systemd scope alive through reboots and after SSH logout
sudo loginctl enable-linger myapp

# Verify
loginctl show-user myapp --property=Linger  # → Linger=yes
```

**Managing the service user from your SSH session:**
```bash
# Run commands as service user
sudo -u myapp XDG_RUNTIME_DIR=/run/user/$(id -u myapp) systemctl --user status

# Or with machinectl (sets up proper environment automatically)
sudo machinectl shell myapp@ /usr/bin/podman ps
```

**Why not `User=` in a system-level unit?** Systemd rejects `sd_notify` from rootless
containers launched via `User=` in system units, and the user session environment
(`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`) is not set up correctly. The Podman
project explicitly documents this. The correct path is always: dedicated user +
`loginctl enable-linger` + user-level systemd.

**Sources:**
- [Rootless Podman containers under system accounts](https://blog.christophersmart.com/2021/02/20/rootless-podman-containers-under-system-accounts-managed-and-enabled-at-boot-with-systemd/)
- [Podman GitHub Discussion #28320](https://github.com/containers/podman/discussions/28320)

---

## 2. CI/CD: GitHub Actions → GCP VM Deploy

**Recommendation:** GitHub Actions + Workload Identity Federation (keyless) +
`google-github-actions/ssh-compute` via IAP tunnel.

### Why WIF over service account JSON key
- No long-lived secrets to rotate or leak
- Short-lived tokens (1-hour expiry) issued per workflow run
- Scoped to specific repos/branches via attribute conditions
- Official Google recommendation since 2021, now mature

### Setup

**GCP side:**
```bash
# Create WIF pool and GitHub OIDC provider
gcloud iam workload-identity-pools create github-actions-pool \
  --location=global

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global \
  --workload-identity-pool=github-actions-pool \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='YOUR_ORG/YOUR_REPO'"

# Create service account for GitHub Actions
gcloud iam service-accounts create github-actions-sa

# Allow WIF pool to impersonate it (scoped to your repo)
gcloud iam service-accounts add-iam-policy-binding \
  github-actions-sa@PROJECT_ID.iam.gserviceaccount.com \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-actions-pool/attribute.repository/YOUR_ORG/YOUR_REPO"

# Grant IAP tunnel and OS login roles
gcloud projects add-iam-policy-binding PROJECT_ID \
  --role=roles/iap.tunnelResourceAccessor \
  --member="serviceAccount:github-actions-sa@PROJECT_ID.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding PROJECT_ID \
  --role=roles/compute.osLogin \
  --member="serviceAccount:github-actions-sa@PROJECT_ID.iam.gserviceaccount.com"

# Firewall rule for IAP TCP forwarding
gcloud compute firewall-rules create allow-iap-ssh \
  --allow=tcp:22 \
  --source-ranges=35.235.240.0/20
```

**GitHub Actions workflow:**
```yaml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write   # required for WIF OIDC token

    steps:
      - uses: actions/checkout@v4

      - id: auth
        uses: google-github-actions/auth@v3
        with:
          workload_identity_provider: 'projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-actions-pool/providers/github-provider'
          service_account: 'github-actions-sa@PROJECT_ID.iam.gserviceaccount.com'

      - id: deploy
        uses: google-github-actions/ssh-compute@v2
        with:
          instance_name: 'YOUR_VM_NAME'
          zone: 'YOUR_ZONE'
          ssh_private_key: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}
          command: |
            cd /var/lib/myapp/app
            git pull origin main
            just build
            systemctl --user restart myapp.service
```

**Note on SSH key:** WIF provides GCP API auth for the IAP tunnel. A separate Ed25519
deploy key pair is still needed for the actual SSH session — store the private key in
GitHub Secrets, add the public key to `~myapp/.ssh/authorized_keys`.

### Alternative: Cloud Build + Artifact Registry

A better fit if you want builds inside GCP (not on GitHub runners) and image scanning.
Flow:
1. Cloud Build trigger fires on push → builds image → pushes to
   `REGION-docker.pkg.dev/PROJECT/REPO/myapp:TAG`
2. VM pulls the new image via `podman pull` and restarts

**Verdict for a small deployment:** direct WIF + ssh-compute + git pull is simpler and
sufficient. Cloud Build earns its keep when you need multi-stage builds, vulnerability
scanning, or decoupled build infrastructure.

**Sources:**
- [google-github-actions/ssh-compute](https://github.com/google-github-actions/ssh-compute)
- [Enabling keyless authentication from GitHub Actions (Google Cloud Blog)](https://cloud.google.com/blog/products/identity-security/enabling-keyless-authentication-from-github-actions)
- [Using IAP for TCP forwarding](https://cloud.google.com/iap/docs/using-tcp-forwarding)

---

## 3. Secret Management: GCP Secret Manager

**Common anti-pattern:** env vars (API keys, session secrets, etc.) live in a
gitignored plaintext file on disk — outside version control, tied to no rotation
mechanism, and lost if the VM is recreated.

**Recommendation:** move all secrets to GCP Secret Manager. Fetch them at service
startup into a RAM-backed tmpfs file. Never write them to persistent disk.

### GCP setup
```bash
# Store secrets
echo -n "your-llm-api-key"   | gcloud secrets create llm-api-key   --data-file=-
echo -n "your-session-secret"| gcloud secrets create session-secret --data-file=-

# Grant the VM's attached service account read access
gcloud secrets add-iam-policy-binding llm-api-key \
  --member="serviceAccount:myapp-sa@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
# repeat for each secret
```

### Injection pattern: ExecStartPre + EnvironmentFile on tmpfs

`/run/user/UID/` is a RAM-backed tmpfs — never written to persistent disk. Fetch
secrets there at startup:

```ini
# ~/.config/containers/systemd/myapp.container  (Quadlet format)
[Service]
RuntimeDirectory=myapp
RuntimeDirectoryMode=0700
ExecStartPre=/bin/bash -c '\
  gcloud secrets versions access latest --secret=session-secret \
    | xargs -I{} printf "SESSION_SECRET=%s\n" {} > %t/myapp/secrets.env; \
  gcloud secrets versions access latest --secret=llm-api-key \
    | xargs -I{} printf "LLM_API_KEY=%s\n" {} >> %t/myapp/secrets.env'

[Container]
EnvironmentFile=%t/myapp/secrets.env
```

`%t` expands to `/run/user/UID`. `RuntimeDirectory=myapp` creates `/run/user/UID/myapp`
with mode 0700. The secrets file is ephemeral — it disappears on reboot and is
recreated fresh on each service start.

**Authentication:** the VM's attached service account + metadata server — no
credentials file needed. `gcloud` on the VM authenticates automatically.

**Sources:**
- [GCP Secret Manager + Compute Engine](https://hoop.dev/blog/how-to-configure-gcp-secret-manager-google-compute-engine-for-secure-repeatable-access)
- [Podman secrets with env vars](https://oneuptime.com/blog/post/2026-03-17-use-secret-environment-variable-podman-container/view)

---

## 4. Systemd: User Service vs System Service

**Recommendation: user service with Podman Quadlet + `loginctl enable-linger`**

| Approach | Verdict |
|---|---|
| System service (`/etc/systemd/system/`) with rootless Podman | Broken — `sd_notify` rejected, user session env unavailable |
| System service with rootful Podman | Works but unnecessary privilege escalation |
| User service + `enable-linger` | **Correct pattern for rootless** |
| **Quadlet `.container` files** | **Preferred** — `podman generate systemd` is deprecated in Podman 4.4+ |

### Quadlet (preferred over `podman generate systemd`)

Quadlet is declarative: unit files live in source control and are regenerated at boot.
It supports `ExecStartPre`, `EnvironmentFile`, `Secret=`, `AutoUpdate=registry`, health
checks, and inter-container dependencies.

```ini
# /var/lib/myapp/.config/containers/systemd/myapp.container
[Unit]
Description=My App
After=network-online.target

[Container]
Image=localhost/myapp:latest
PublishPort=8080:3000
EnvironmentFile=%t/myapp/secrets.env
Network=myapp-net
NetworkAlias=myapp

[Service]
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

**Managing:**
```bash
sudo -u myapp systemctl --user daemon-reload
sudo -u myapp systemctl --user enable --now myapp.service
sudo journalctl -M myapp@ --user -u myapp.service -f
```

**Sources:**
- [Podman Quadlet: A Better Way to Run Rootless Containers](https://dev.to/lyraalishaikh/podman-quadlet-a-better-way-to-run-rootless-containers-with-systemd-3i3l)
- [Painless services: rootless Podman and systemd (Red Hat)](https://www.redhat.com/en/blog/painless-services-implementing-serverless-rootless-podman-and-systemd)
- [Podman Quadlet documentation](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html)

---

## 5. Zero-Downtime Deploys with Podman

**Podman has no built-in rolling-update primitive for a single container.** For a small
deployment on a single VM, the recommended pattern is **blue-green with container
network aliases and a reverse proxy (e.g. Caddy) as the resolver**.

### Architecture
```
Load balancer :443
  → VM :8080 (Caddy, always running)
    → container network alias "myapp"
      → myapp-blue OR myapp-green (whichever is live)
      (both during the brief overlap window — Caddy round-robins)
```

### Deploy script
```bash
#!/bin/bash
set -euo pipefail
NEW_IMAGE="localhost/myapp:$1"
NETWORK="myapp-net"

# Determine active slot
if podman ps --format '{{.Names}}' | grep -q '^myapp-blue$'; then
  OLD=myapp-blue; NEW=myapp-green
else
  OLD=myapp-green; NEW=myapp-blue
fi

podman pull "$NEW_IMAGE"

# Start new container with same network alias
podman run -d --name "$NEW" \
  --network "$NETWORK" --network-alias myapp \
  --env-file /run/user/$(id -u)/myapp/secrets.env \
  "$NEW_IMAGE"

# Wait for health check
timeout 60 bash -c "until podman healthcheck run $NEW; do sleep 2; done"

# Drain old container
podman stop "$OLD" && podman rm "$OLD"
echo "Deployed: $NEW_IMAGE as $NEW"
```

**Reverse-proxy config:** resolve by alias, not port — no change needed if already
using something like:
```
reverse_proxy myapp:3000
```

**Health check in the Containerfile:**
```dockerfile
HEALTHCHECK --interval=5s --timeout=3s --retries=5 \
  CMD curl -f http://localhost:3000/api/health || exit 1
```

**For a small deployment:** the overlap window is 10-30s and costs one extra container
instance (a couple of GB of RAM). On a well-provisioned VM this is trivial.

**Sources:**
- [evolutics/zero-downtime-deployments-with-podman](https://github.com/evolutics/zero-downtime-deployments-with-podman-docker-or-docker-compose)
- [Zero-Downtime Node.js Blue-Green with Docker](https://medium.com/@vasanthancomrads/zero-downtime-node-js-deployments-with-blue-green-strategy-in-docker-917e01cbed80)

---

## Summary

| Area | Anti-pattern | Target |
|---|---|---|
| Service user | Personal user account | Dedicated non-login system user in `/var/lib/` |
| Systemd unit | `podman generate systemd` under a personal account | Podman Quadlet under the service user + linger |
| CI/CD | Manual SSH | GitHub Actions + WIF + `google-github-actions/ssh-compute` via IAP |
| Secrets | Gitignored env file on disk | GCP Secret Manager → ExecStartPre → `/run/user/UID/` tmpfs |
| Zero-downtime | None | Blue-green with reverse-proxy alias routing |

**Highest priority:** service-user migration. If the app runs under a personal account
that later gets offboarded or changed, the app goes down.

<!-- /doc:region name="body" -->

<!-- doc:region name="ambiguous_items" kind="replaceable" -->

## Ambiguous Items from Auto-Remediation (Post-Run Review)

(none yet — auto-populated by verify_and_remediate node)

<!-- /doc:region name="ambiguous_items" -->

<!-- doc:region name="appendix_research_prompt" kind="immutable" -->

## Appendix: Research Prompt

[Original prompt not recorded in this doc]

<!-- /doc:region name="appendix_research_prompt" -->

<!-- doc:region name="appendix_provenance" kind="replaceable" -->

## Appendix: Provenance Ledger

(none yet — auto-populated at commit from the validated provenance ledger)

<!-- /doc:region name="appendix_provenance" -->

<!-- doc:region name="run_history" kind="append_only" -->

## Run History

<!-- /doc:region name="run_history" -->
