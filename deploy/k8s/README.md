# Kubernetes manifests

Apply all manifests at once with kustomize:

```
kubectl apply -k deploy/k8s
```

## Manifests

| File | Purpose |
|------|---------|
| `pvc.yaml` | 10 GiB PersistentVolumeClaim for SQLite data |
| `deployment.yaml` | Single-replica Deployment with liveness + readiness probes |
| `service.yaml` | ClusterIP Service on port 8000 |
| `kustomization.yaml` | Kustomize entry point |

Single replica only. Horizontal scaling is deferred per ADR-009 (SQLite
write serialisation; migrate to Postgres before enabling replicas > 1).

## Secrets

Create the secret from your `.env` file before the first deploy:

```
kubectl create secret generic lmchat-secrets --from-env-file=.env
```

Required keys (see `deploy/systemd/lmchat.env.example` for the full
template):

- `LM_CHAT_SECRET` — session signing + TOTP key-derivation secret (generate
  with `python -c "import secrets; print(secrets.token_urlsafe(48))"`)
- `LM_STUDIO_BASE_URL` — URL of the LM Studio API (e.g. `http://lmstudio-host:1234`)

## Image

The deployment references `lmchat:latest`. Build and push to your registry:

```
docker build -f deploy/Dockerfile -t <registry>/lmchat:1.0.0 .
docker push <registry>/lmchat:1.0.0
```

Then patch the image in your kustomize overlay:

```yaml
# overlays/prod/kustomization.yaml
images:
  - name: lmchat
    newName: <registry>/lmchat
    newTag: "1.0.0"
```

## Accessing the service

Expose via an Ingress or port-forward for local testing:

```
kubectl port-forward svc/lmchat 8000:8000
```

For SSE streaming through an ingress controller, ensure proxy buffering is
disabled on the `/api/chat/stream` path — see `deploy/nginx.conf.example`
for the nginx snippet.

## Readiness vs liveness

`/healthz` (liveness — always 200, drives restarts) and `/readyz`
(readiness — gates Service traffic routing) are separate endpoints for a
reason. `/readyz` gates its 200/503 status on DB + session-store
reachability only; LM Studio reachability is probed and reported in the
response body (`checks.lm_studio`, plus `degraded: ["lm_studio"]` when
down) for observability, but it never fails readiness. `deployment.yaml`
wires `readinessProbe` to `/readyz`, which means: with `replicas: 1`, a
pod is pulled from the Service's endpoints only on a real DB/session-store
outage — an admin closing LM Studio (routine for this local-first,
single-admin app) leaves the whole app reachable; only chat/model features
degrade until LM Studio comes back. Do not wire `livenessProbe` (restarts)
to `/readyz` — see the comments in `deployment.yaml`.

## LM Studio connectivity

`LM_STUDIO_BASE_URL` in `deployment.yaml` defaults to
`http://lmstudio-host:1234`. Update this to match your cluster topology
(ExternalName service, NodePort, or host IP).
