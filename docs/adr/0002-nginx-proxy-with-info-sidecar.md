# ADR-0002: Replace Flask/gunicorn `web` container with nginx proxy + `info` sidecar

**Status:** Accepted
**Date:** 2026-04-17

## Context

`lucos_docker_mirror` as initially implemented (see ADR-0001) consists of two containers:

- `registry` — the upstream Docker registry in proxy mode.
- `web` — a Flask application running under sync gunicorn (`-w 2`), responsible for three things:
  1. Terminating client-facing HTTP basic auth against `REGISTRY_CLIENT_USERNAME` / `REGISTRY_CLIENT_PASSWORD`.
  2. Transparently reverse-proxying every other request through to the `registry` container on port 5000.
  3. Synthesising a `/_info` endpoint with three live checks (registry reachable, Docker Hub reachable, cache disk usage).

The proxy handler in `app.py` reads each incoming request body into memory via `request.get_data()`, hands it to the `requests` library with `stream=True` on the response, and iterates the upstream response back to the client in 8 KB chunks.

### The 2026-04-17 incident

Shortly after rollout, sync gunicorn workers were observed being SIGABRT-killed mid-stream on `docker pull` blob transfers in the 46–91 MB range. The kill signal came from gunicorn's arbiter against its default `--timeout 30` (sync-worker semantics: the wall-clock interval a request is permitted to hold a worker before the worker is declared hung and reaped). At the slower end of our client bandwidth distribution, a 60 MB blob transfer hits the 30-second threshold well before completion.

Raising `--timeout` would suppress the immediate symptom but leaves the architectural mismatch — and the broader concurrency cap — unaddressed. The incident is the surface expression of a deeper fit problem.

### Why Python/Flask/sync-gunicorn is the wrong shape

The `web` container's job description reduces to "stream bytes between a client and an upstream, with a basic-auth gate". That is the canonical reverse-proxy workload. Flask is a request/response framework, not a streaming proxy, and the current implementation exhibits several framework-level mismatches:

1. **`request.get_data()` fully buffers request bodies in memory before proxying.** For `docker push`, this means an entire image layer — tens to hundreds of MB — is held in a worker's address space before the first byte leaves for upstream. There is no framework-idiomatic way in Flask to stream the request body through `requests` without substantial work fighting WSGI's `environ['wsgi.input']` semantics.
2. **Sync gunicorn workers block end-to-end on a single request.** `-w 2` means exactly two concurrent streaming transfers; a third concurrent `docker pull` waits. Under agent-driven estate automation (which routinely runs ~30 CI builds in a short window, per lucos observations in 2026-03 and 2026-04), this is a realistic bottleneck, not a theoretical one.
3. **Every byte traverses WSGI + Flask + `requests` + gunicorn userspace copies.** nginx's `proxy_pass` uses kernel-level `sendfile`/`splice` in the common case, moving bytes between sockets without ever entering the Python address space.
4. **Dependency surface is disproportionate to responsibility.** Shipping a Python runtime, Flask, `requests`, urllib3, gunicorn, and their transitive trees — to reproduce what stock nginx does in its default config in five lines of configuration — is a larger ongoing tax (Dependabot noise, CVE surface, image size) than the responsibility justifies.

None of the above is "Python is slow". Python is fine. It is the wrong *shape* for this job.

### The `/_info` wrinkle

Replacing `web` with pure stock nginx is complicated by the `/_info` endpoint, which performs three live checks:

- Registry reachability (a single HTTP call to the internal `registry` service).
- Docker Hub reachability (a single HTTP call to `registry-1.docker.io/v2/`).
- Cache filesystem disk usage (a `statvfs` on `/var/lib/registry`).

Stock nginx has no clean way to execute those checks and assemble the lucos-conventional `/_info` JSON response. Options for addressing the `/_info` requirement were evaluated as part of this decision (see Alternatives).

## Decision

Replace the `web` container with a two-container split:

- **`web`**: stock `nginx:alpine`. Terminates client basic auth (`auth_basic`), routes `/_info` to the new `info` sidecar, and `proxy_pass`es everything else to the `registry` container on port 5000. Proxy buffering disabled on the registry upstream so client↔upstream streaming runs end-to-end.
- **`info`**: a minimal Python/Flask+gunicorn sidecar whose sole responsibility is responding to `/_info`. It is not on the data-plane hot path — it handles sub-second health checks, nothing else. It inherits the existing `_check_registry`, `_check_upstream`, and `_check_disk` implementations from the current `app.py`, with the proxy handler deleted.

The public contract at `docker.l42.eu` is unchanged: same domain, same auth scheme, same `/_info` shape, same registry API exposed at every other path. Only the internal topology and the runtime stack backing it change.

### htpasswd generation

nginx's `auth_basic` expects an on-disk htpasswd file. The lucos convention is environment variables from `lucos_creds`, not mounted files. The `web` container's entrypoint will therefore generate the htpasswd file at container start from `REGISTRY_CLIENT_USERNAME` and `REGISTRY_CLIENT_PASSWORD`, then exec nginx. This is a small one-shot shell step, not an ongoing concern.

### Compose topology

The stack grows from 2 containers to 3 (`registry` + `web` + `info`). `web` gains `depends_on` on both `registry` and `info`. The existing named cache volume is unchanged — `info` takes over the read-only mount for the disk-usage check, `web` does not need it.

### Internal routing shape

```
client -> web (nginx:alpine) ─┬─> /_info          -> info (python/flask)
                              └─> all other paths -> registry (registry:2)
```

## Alternatives considered

### Tactical fix within the existing Python stack

Switch sync gunicorn workers to gthread (`-k gthread --threads 32 -w 2`), raise `--timeout` substantially, and replace `request.get_data()` with streaming input via `request.stream`. This is genuinely a ~5-line change and would resolve the immediate SIGABRT incident.

Rejected as the end state (but explicitly kept available as a rollback position — see Reversibility). The tactical fix silences the symptom without addressing the mismatch: Python remains on the data-plane hot path, the Dependabot surface stays disproportionate to the responsibility, the two-worker concurrency cap remains, and the first future scaling pressure point rediscovers the same problem from a different angle. We already pay for the tactical fix once in this incident; paying for it again in six months is avoidable.

### Async Python (uvicorn + ASGI framework with `httpx` streaming)

Replace sync gunicorn workers with an ASGI server (uvicorn) and a framework supporting genuine async request/response streaming (FastAPI, Starlette, or aiohttp) alongside a streaming HTTP client (`httpx` in async mode). Distinct from the gthread tactical fix above: async Python would resolve both the concurrency cap *and* the request-body streaming problem while remaining in-language, and would not rely on thread-pool sizing to work around a framework that was never streaming-shaped to begin with.

Rejected: Python remains on the data-plane hot path, and the dependency tree stays disproportionate to the responsibility — an ASGI runtime, an HTTP framework, an async HTTP client, and their transitive deps, in service of a role nginx fulfils in its default config with zero application code. The rewrite effort would also be comparable to introducing nginx (the current `app.py` is not trivially asyncifiable — `requests` would be swapped for `httpx.AsyncClient`, the proxy handler re-expressed against ASGI's `receive`/`send`, and gunicorn replaced with uvicorn). At similar implementation cost we should take the architecturally correct answer, not a better Python.

### OpenResty (nginx + Lua) for `/_info` synthesis

Write the three checks in Lua and execute them in-nginx. Architecturally elegant — one container, one runtime, no Python on the hot path or anywhere else. Rejected because it introduces a non-stock nginx build to the estate for a single endpoint on a single service, and OpenResty is not currently used anywhere in lucos. The operational and review cost (a new runtime that no other team member has worked with) exceeds the benefit of eliminating the small sidecar.

### Static `/_info` JSON refreshed by a cron sidecar

nginx serves `/_info` as a static file; a sidecar regenerates the file every N seconds. Rejected: a health endpoint that is up to N seconds stale is a worse default than a live endpoint, and the monitoring semantics ("the system was healthy N seconds ago") are surprising. The engineering cost is about the same as the live sidecar, without the live-signal benefit.

### Registry-native htpasswd auth, drop `web` entirely

`registry:2` supports `REGISTRY_AUTH=htpasswd` natively. The `web` container could be removed altogether, with the registry exposed directly behind the estate's front-door TLS. Rejected because this eliminates the `/_info` endpoint entirely (the registry does not speak the lucos `/_info` convention), and because it forecloses the ability to add any other edge-level concerns in future (request logging, path rewrites, rate limits specific to this service) without reintroducing an edge container. The cost of keeping a thin edge container is small; the option value of having one is real.

### Caddy instead of nginx

Caddy is a plausible peer to nginx for this role and has a more concise config language. Rejected for consistency: nginx is already present in the estate (`lucos_arachne`, `lucos_router`). Introducing a second reverse-proxy technology to do a job nginx already does locally is a dilution without a payoff.

## Consequences

### Positive

- **End-to-end streaming.** Large blob transfers are bounded only by client and upstream bandwidth, not by any worker-level timeout or buffer.
- **Concurrency is no longer pinned to a small integer.** A single nginx worker handles thousands of concurrent streams; the `-w 2` hard cap is gone.
- **Right tool per responsibility.** The data plane runs on software built for byte-streaming at network line rate. The metadata plane runs on a small Python process that handles trivial health checks. Neither is contorted to do the other's job.
- **Smaller and stabler dependency tree on the hot path.** `nginx:alpine` ships no application dependencies — just nginx itself — so Dependabot noise on the critical `web` container drops to near zero. The remaining Python deps live on the `info` sidecar, where a failure would take out the health endpoint but not CI pulls.
- **Failure isolation.** A bug or OOM in `info` takes out `/_info` (so monitoring reports the service as unhealthy — correctly). It does not take out `docker pull`. This is a strictly better failure mode than the current design, where a bug in the same Python process that answers `/_info` also serves every blob transfer.

### Negative

- **Container count goes from 2 to 3.** A small increase in operational surface: one more image to build and patch, one more healthcheck to maintain, one more entry in `lucos_configy` if applicable. The incremental cost is genuinely small but it is non-zero and worth naming.
- **Nginx config is now a first-class artefact in this repo.** Previously the proxy logic lived in Python and was reviewable as code; it now lives in an nginx config file, which is a different review surface. This is a neutral-to-positive change on balance (nginx config is well-documented and lint-able) but it does shift what reviewers are looking at.
- **Htpasswd generation at container start is a new moving part.** A small shell entrypoint reads two env vars, writes a file, exec's nginx. Straightforward, but it is code that did not exist before.
- **The tactical fix is forgone.** The 2026-04-17 incident remains live until this re-architecture ships, rather than being fully closed in the hours it would take to ship the one-line gthread switch. This trade-off is made deliberately — see Alternatives — but it is a real near-term cost.

### Reversibility

High. The `web` container is the only thing replaced; the registry, cache volume, `lucos_creds` variables, CI, routing, and public contract at `docker.l42.eu` are all unchanged. Rolling back is either (a) revert the implementation PR, which restores the Flask `web` container, or (b) ship the tactical fix on top of the reverted state, which closes the incident without touching the architecture. No data migration, no client-visible change, no coordinated estate rollout.

## Implementation notes (for the follow-up issue)

These are orientation notes for whoever picks up the implementation; the authoritative scope lives in the linked issue.

- Replace the existing root `Dockerfile` with an `nginx:alpine`-based image: copy a new `nginx.conf`, copy a small entrypoint shell script, expose `${PORT}`.
- Move the current `app.py`, `requirements.txt` (minus `requests` — the sidecar still needs it for the upstream check, keep as appropriate) into a new `info/` subdirectory with its own `Dockerfile`. Strip the `proxy` handler; keep only `/_info` and its three checks.
- `nginx.conf` needs: `listen ${PORT}` (templated at start via `envsubst` or similar), `auth_basic` on the proxy location only (not on `/_info` — monitoring hits `/_info` unauthenticated today), `location = /_info { proxy_pass http://info:...; }`, `location / { auth_basic on; proxy_pass http://registry:5000; proxy_buffering off; proxy_request_buffering off; client_max_body_size 0; }`.
- Entrypoint (`/docker-entrypoint.d/...` or a top-level `entrypoint.sh`): `htpasswd -Bbn "$REGISTRY_CLIENT_USERNAME" "$REGISTRY_CLIENT_PASSWORD" > /etc/nginx/.htpasswd` (alpine needs `apache2-utils` or `bcrypt` alternatives — implementer to choose), then `envsubst` the nginx config template, then exec nginx.
- `docker-compose.yml`: add the `info` service; `web` gains `depends_on` both `registry` and `info` (with `condition: service_healthy` on `info` too); remove the cache volume mount from `web` and add it to `info` read-only.
- Healthchecks: `web` stays as `wget /_info` (which now flows through the full nginx→info path — a stronger check than before). `info` gains its own healthcheck hitting its own `/_info` on its internal port.

## References

- ADR-0001 (this repo) — the pull-through cache decision that introduced this service.
- lucas42/lucos_docker_mirror#22 — tracking issue for this ADR.
- nginx `proxy_pass` streaming behaviour: https://nginx.org/en/docs/http/ngx_http_proxy_module.html (in particular `proxy_buffering`, `proxy_request_buffering`, `client_max_body_size`).
- gunicorn worker types and `--timeout` semantics: https://docs.gunicorn.org/en/stable/settings.html#timeout (for the record of what the 30s sync-worker timeout actually means, given the confusion the incident caused).
