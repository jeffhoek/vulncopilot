# Container Hardening — Before/After Pen Test

A record of the container/pod-hardening pass on `vulncopilot`: what the unhardened pod let you do, what changed (`9c26e99`, `d2710e0`), and how each change was verified live rather than assumed. Covers both the non-root Dockerfile change (shared by the EKS and Azure deployments) and the K8s-specific `securityContext` enforcement (EKS only). Kept here so the exercise doesn't have to be redone from scratch to answer "why do we have this `securityContext`?"

## Goal

Deploy the app in its default (unhardened) state, demonstrate concretely what that exposes via a lightweight pen test, then apply `securityContext` hardening and verify both that the exposure is closed and the app still works.

## Before: what the unhardened pod allowed

[k8s/deployment.yaml](../k8s/deployment.yaml) originally shipped with **no `securityContext` at all**. Against the live pod:

```bash
POD=$(kubectl get pod -n rag -l app=vulncopilot -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n rag "$POD" -- id
# uid=0(root) gid=0(root) groups=0(root)

kubectl exec -n rag "$POD" -- sh -c 'echo pwned > /app/PWNED_DEMO.txt && cat /app/PWNED_DEMO.txt && rm /app/PWNED_DEMO.txt'
# succeeds — root filesystem writable, i.e. the app's own source could be tampered with in place

kubectl exec -n rag "$POD" -- sh -c 'grep -E "NoNewPrivs|CapEff" /proc/1/status'
# CapEff: 00000000a80425fb   NoNewPrivs: 0
```

`0xa80425fb` decodes to Docker's full default 14-capability set, none dropped: `CHOWN, DAC_OVERRIDE, FOWNER, FSETID, KILL, SETGID, SETUID, SETPCAP, NET_BIND_SERVICE, NET_RAW, SYS_CHROOT, MKNOD, AUDIT_WRITE, SETFCAP`. `NoNewPrivs: 0` means `allowPrivilegeEscalation` wasn't blocked either.

**Summary of exposure:** root user, writable rootfs (in-place tamper/persistence), full capability set, privilege escalation unblocked, plus a projected ServiceAccount API token the pod never needed (see below).

## Changes made

| File | Change |
|---|---|
| [Dockerfile](../Dockerfile) | Added a dedicated UID/GID `10001` (`appuser`), `chown`'d `/app` (both the copied contents and the directory itself — see gotcha below), and switched to `USER 10001`. |
| [k8s/deployment.yaml](../k8s/deployment.yaml) | Pod-level `securityContext` (`runAsNonRoot`, `runAsUser`/`runAsGroup`/`fsGroup: 10001`, `seccompProfile: RuntimeDefault`) and container-level `securityContext` (`allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`), plus two `emptyDir` volumes mounted at `/app/.files` (Chainlit's upload spool — required, since `features.spontaneous_file_upload` is enabled) and `/tmp` (general safety net). |
| [k8s/serviceaccount.yaml](../k8s/serviceaccount.yaml) | `automountServiceAccountToken: false` — the pod has no AWS data-plane access (Postgres is reached via `PG_DATABASE_URL`, not IAM), so the projected token was unused attack surface. |

### Dockerfile ownership gotcha found along the way

`WORKDIR /app` creates `/app` as `root:root` *before* the later `COPY --chown=10001:10001` runs. Docker's `--chown` re-owns the copied *contents*, but not the pre-existing destination directory itself — so without an explicit follow-up `RUN chown 10001:10001 /app`, the directory stays root-owned even though everything inside it is correctly owned by `appuser`. This surfaced only because a stray local `.files/` directory (an artifact of an unrelated local `import chainlit` test) got baked into an early build and masked the bug — `mkdir(exist_ok=True)` on an already-existing path short-circuits on `EEXIST` before the kernel ever checks write permission, so Chainlit's own `.files` creation appeared to succeed while a fresh file write to `/app` did not. Fixed by adding `.files/` to `.gitignore`/`.dockerignore` and the explicit `chown`.

## Test methodology: one variable at a time

Rather than applying the Dockerfile and K8s manifest changes together, they were rolled out and verified in two stages, since the two are orthogonal:

1. **Image only** (`kubectl set image` to the new non-root image, old pod spec / no `securityContext` still in place) — isolates what the non-root Dockerfile user alone buys you.
2. **Full manifest** (`kubectl apply -f k8s/deployment.yaml`) — layers in the K8s-enforced protections.

Stage 1 results were instructive on their own: `id` showed `uid=10001`, and `CapEff` was already `0` (Linux drops effective capabilities on a non-root exec unless the ambient set grants them back — this is independent of any `securityContext`), but `NoNewPrivs` was still `0` and `/app` was still writable (until the ownership gotcha above was fixed) — confirming that a non-root image user alone does **not** get you `readOnlyRootFilesystem` or `allowPrivilegeEscalation: false`; those require the explicit K8s `securityContext`.

## After: verification

Once the full manifest was applied and the image pointed at the hardened build:

```bash
kubectl exec -n rag "$POD" -- id
# uid=10001(appuser) gid=10001(appuser) groups=10001(appuser)

kubectl exec -n rag "$POD" -- sh -c 'echo pwned > /app/PWNED_DEMO.txt'
# sh: /app/PWNED_DEMO.txt: Read-only file system

kubectl exec -n rag "$POD" -- sh -c 'echo test > /app/.files/test.txt && cat /app/.files/test.txt && rm /app/.files/test.txt'
# succeeds — the emptyDir mount keeps file uploads working despite the read-only rootfs

kubectl exec -n rag "$POD" -- sh -c 'touch /etc/PWNED_DEMO.txt'
# Read-only file system

kubectl exec -n rag "$POD" -- sh -c 'grep -E "NoNewPrivs|CapEff" /proc/1/status'
# CapEff: 0000000000000000   NoNewPrivs: 1

ls /var/run/secrets/kubernetes.io/serviceaccount/ 2>&1
# No such file or directory — confirms automountServiceAccountToken: false took effect
```

Functional check: `curl https://rag.manheok.com/healthz` → `200`, GitHub OAuth login still worked, file upload still worked (writing through the `.files` `emptyDir` mount).

## Cross-deployment check: Azure App Service

The Dockerfile (not the K8s manifests — those are Kubernetes-only) is also built by [azure-pipelines.yml](../azure-pipelines.yml) for the Azure App Service deployment, which is the "always on" production instance. Its pipeline triggers on push to `main` with **no PR-time check** (`pr: none`), and its deploy stage restarts the live app *before* polling `/healthz` — i.e. merging is the moment it would have deployed untested.

Validated out-of-band before merging, same pattern as the EKS manual tests: built and pushed a `:hardened-test` tag to ACR, pointed the App Service at it directly (`az webapp config container set` + `az webapp restart`), and confirmed `/healthz` returned `200` while genuinely running the new tag (not a cached response). Rollback value captured beforehand: `az webapp config container show` showed the previously-running tag (`298bbe6`) for a fast revert if needed.

## Rollback

- **EKS**: `kubectl rollout undo deployment/vulncopilot -n rag` — works regardless of how the bad rollout got there (CI or manual).
- **Azure**: `az webapp config container set --docker-custom-image-name <previous-tag>` followed by `az webapp restart`.

## Known gap found later: `.chainlit/translations` under `readOnlyRootFilesystem`

`.chainlit/translations/` is gitignored, so it's never in the build context — the Docker image never bakes it in. Chainlit's own `init_config()` re-creates it from its bundled defaults at every startup via `os.makedirs()`, which fails under `readOnlyRootFilesystem: true` and crashloops the pod (`OSError: [Errno 30] Read-only file system`).

This didn't surface during the pen test above because that test's image was built locally, where `.chainlit/translations/` already existed on disk from an earlier `chainlit run` and got copied in despite being gitignored (`.dockerignore` doesn't exclude it — only `.gitignore` does). A clean CI checkout never has the directory. Fixed by mounting an `emptyDir` at `/app/.chainlit/translations`, same pattern as `/app/.files` and `/tmp`.
