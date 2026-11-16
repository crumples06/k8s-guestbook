# Guestbook

A small guestbook REST API (Flask + MySQL) built to practice core Kubernetes concepts on a local cluster. Every response includes the name of the Pod that handled the request, so you can watch load balancing across replicas happen.

This is a learning project, not production software. See [Known limitations](#known-limitations).

## Architecture

```
curl / browser
      |
      v
Ingress (nginx, host: localhost)
      |
      v
Service guestbook-api :80
      |
      v
3 x API Pods (Flask + gunicorn, :3000)
      |
      v
Service mysql :3306
      |
      v
StatefulSet pod mysql-0  -->  PVC data-mysql-0 (1Gi)
```

## Repository layout

| Path | Purpose |
|---|---|
| `app/app.py` | The whole application |
| `app/requirements.txt` | Pinned Python dependencies |
| `app/dockerfile` | Image build |
| `app/.dockerignore` | Keeps `.venv` and `__pycache__` out of the build context |
| `k8s/config.yaml` | ConfigMap (DB settings) and Secret (passwords) |
| `k8s/mysql.yaml` | MySQL Service and StatefulSet with persistent volume |
| `k8s/api.yaml` | API Deployment (3 replicas) and Service |
| `k8s/ingress.yaml` | Ingress routing external HTTP to the API |

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/entries` | Body: `{"name": "...", "message": "..."}`. Returns `201 {"id": ..., "pod": "..."}`. Returns `400` if a field is missing. |
| `GET` | `/entries` | Returns the 50 newest entries and the serving pod: `{"pod": "...", "entries": [...]}` |
| `GET` | `/healthz` | Returns `ok`. Used by the Kubernetes probes. |

Each entry has `id`, `name` (max 100 chars), `message` (max 500 chars), `served_by` and `created_at`.

- The top-level `pod` in a GET response is the Pod that served the **read**.
- `served_by` on each entry is the Pod that handled the original **write**.

## Prerequisites

- Docker
- `kubectl`
- A local Kubernetes cluster (Docker Desktop, kind or minikube)
- The [ingress-nginx](https://kubernetes.github.io/ingress-nginx/deploy/) controller installed in the cluster. The Ingress object is only a set of rules, and does nothing without a controller.

## Setup

### 1. Build the image

```bash
docker build -t guestbook-api:1.0 -f app/dockerfile app/
```

The manifests use `imagePullPolicy: IfNotPresent` with an image that isn't in any registry, so the image must exist on the cluster's nodes:

```bash
# Docker Desktop: nothing to do, the cluster shares your local Docker daemon
# kind:
kind load docker-image guestbook-api:1.0
# minikube:
minikube image load guestbook-api:1.0
```

### 2. Deploy

Apply the config first, then the database, then the rest:

```bash
kubectl apply -f k8s/config.yaml
kubectl apply -f k8s/mysql.yaml
kubectl rollout status statefulset/mysql
kubectl apply -f k8s/api.yaml -f k8s/ingress.yaml
kubectl rollout status deployment/guestbook-api
```

`kubectl apply -f k8s/` also works, but it applies files alphabetically, so the API Deployment is created before its ConfigMap and Secret. Pods briefly sit in `CreateContainerConfigError` and then recover on their own.

### 3. Try it

```bash
curl -X POST localhost/entries \
     -H 'Content-Type: application/json' \
     -d '{"name":"Ana","message":"hello"}'

curl localhost/entries
```

Repeat the GET a few times. The `pod` field changes as the Service spreads requests across the three replicas.

If `localhost` doesn't reach the Ingress (common with kind unless ports 80/443 are mapped, or minikube without a tunnel), bypass it with a port-forward:

```bash
kubectl port-forward svc/guestbook-api 8080:80
curl localhost:8080/entries
```

## How it works

### Application

- Configuration comes entirely from environment variables (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`), so the same image runs anywhere.
- A new database connection is opened per request, with no pooling. This is simple and survives a MySQL restart without stale connections, at the cost of connection overhead.
- Queries are parameterized, so they are not open to SQL injection.
- On startup, `init_db()` retries for about 60 seconds until MySQL is reachable, then runs `CREATE TABLE IF NOT EXISTS`. The `IF NOT EXISTS` makes it safe for every gunicorn worker in every Pod to run it concurrently.
- The Pod name comes from `socket.gethostname()`, since a Pod's hostname defaults to its name.

### Container image

- Based on `python:3.12-slim`.
- `requirements.txt` is copied and installed before `app.py`, so code changes don't reinstall dependencies.
- Runs as `nobody`, not root.
- Served by gunicorn with 2 workers, not Flask's development server.
- `cryptography` is in `requirements.txt` but never imported. PyMySQL needs it for MySQL 8's default `caching_sha2_password` authentication.

### Kubernetes objects

**ConfigMap `guestbook-config`** holds non-sensitive settings. `DB_HOST: mysql` works because cluster DNS resolves the Service name. The API loads the whole ConfigMap with `envFrom`; MySQL reads only `DB_NAME` and `DB_USER`.

**Secret `mysql-secret`** holds the root and application database passwords. Secrets are base64-encoded, not encrypted.

**StatefulSet `mysql`** runs the database. A StatefulSet is used instead of a Deployment because the database needs a stable identity (`mysql-0`) and storage that follows it. `volumeClaimTemplates` creates a 1Gi PVC mounted at `/var/lib/mysql`, which survives Pod deletion. The official image creates the database and user from the `MYSQL_*` variables on first initialization. The readiness probe uses `mysqladmin ping -h 127.0.0.1`: forcing TCP matters because during first-run initialization MySQL runs a temporary server listening only on a socket, which a default ping would mistake for ready.

**Deployment `guestbook-api`** keeps 3 stateless replicas running.
- An init container (`busybox`, `nc -z`) waits until `mysql:3306` accepts connections before the app starts.
- The readiness probe decides whether a Pod receives traffic. The liveness probe decides whether it gets restarted (20s initial delay).
- Resources: requests of 50m CPU and 64Mi memory, and a 128Mi memory limit (exceeding it gets the container `OOMKilled`). There is no CPU limit, which avoids throttling.

**Services** give stable names and virtual IPs. `guestbook-api` maps port 80 to container port 3000 and selects Pods by the `app: guestbook-api` label. Only Ready Pods are listed as endpoints.

**Ingress `guestbook`** routes every path (`Prefix` match on `/`) for host `localhost` to the API Service. The ingress-nginx controller routes by the HTTP `Host` header, so the host in the Ingress must match what the client sends. To use a custom name such as `guestbook.local`, change `host:` in `k8s/ingress.yaml` and send `-H "Host: guestbook.local"` with curl or add a hosts-file entry.

## Useful commands

```bash
kubectl get pods -o wide                      # see all Pods and their IPs
kubectl get endpointslices                    # which Pod IPs each Service targets
kubectl logs deploy/guestbook-api             # API logs
kubectl describe pod <name>                   # events, probe failures, OOMKilled
kubectl delete pod <api-pod>                  # a replacement appears within seconds
kubectl scale deployment guestbook-api --replicas=5
```

Experiments worth running:
- Delete an API Pod and watch the ReplicaSet recreate it.
- Relabel a Pod (`kubectl label pod <name> app=stranger --overwrite`) and watch it leave the Service endpoints and the ReplicaSet.
- Delete `mysql-0`, wait for it to return, and confirm your entries are still there.

## Cleanup

```bash
kubectl delete -f k8s/ingress.yaml -f k8s/api.yaml -f k8s/mysql.yaml -f k8s/config.yaml
kubectl delete pvc data-mysql-0     # deletes the database data
```

Deleting the StatefulSet does **not** delete its PVC. The data survives until you delete the PVC explicitly.

## Known limitations

These are deliberate simplifications for a local learning project:

- **Secrets are committed to git in plain text** (`root`, `devpass`). Fine for local use, not beyond that.
- **Changing the Secret later does not change the real database password.** MySQL creates its user only when the data directory is empty. To re-initialize you must delete the PVC, which destroys the data.
- **`/healthz` doesn't check the database.** If MySQL goes down, API Pods stay Ready and return 500s instead of being removed from the Service.
- **MySQL is a single instance** with no replication or backups. Raising `replicas` would create independent, unsynchronized databases, not a cluster.
- **The `mysql` Service is not headless**, so per-Pod DNS names like `mysql-0.mysql` don't exist. The app connects through the plain Service name.
- **No input length validation.** A name over 100 characters or a message over 500 makes the insert fail with a 500 error instead of a 400. A JSON body that isn't an object also causes a 500.
- **No authentication, rate limiting or pagination.**
- **The 128Mi memory limit may be tight** for two gunicorn workers. If Pods restart, check `kubectl describe pod` for `OOMKilled`.
- **The image file is named `dockerfile` (lowercase)**, hence `-f app/dockerfile` in the build command. Renaming it to `Dockerfile` removes the need for the flag.
