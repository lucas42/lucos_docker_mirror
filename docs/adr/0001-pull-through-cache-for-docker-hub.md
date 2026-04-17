# ADR-0001: Pull-through cache for Docker Hub base images

**Status:** Accepted
**Date:** 2026-04-17

## Context

### The rate-limit problem

Docker Hub enforces a 200-pull-per-6-hours rate limit on authenticated free-tier accounts. The lucos estate CI pipeline (CircleCI) authenticates to Docker Hub as the `lucas42` account and pulls base images on every build. Two structural factors have pushed the estate chronically over that budget since early 2026:

1. **Multi-platform builds via QEMU.** Since the retirement of the `pici` ARM builder on 2026-03-17 (tracked in the lucos repo), ARM-deployed services build via buildx + QEMU on CircleCI's amd64 runners. Every base image referenced in a multi-arch Dockerfile is pulled once per target platform — doubling the pull count for services deployed to both amd64 and ARM hosts.
2. **Agent-driven estate automation.** Agents routinely land ~30 PRs in a ~30-minute window (e.g. estate-wide convention rollouts). Each PR triggers a CI build that pulls 2–4 base images. A single agent-driven sweep can consume the entire 6-hour budget in minutes.

Neither factor existed when the estate was sized against the Docker Hub free-tier limit. Both are now permanent features of how the estate operates.

### First attempt: GHCR static mirror (superseded)

The originally-agreed fix was a curated mirror on GitHub Container Registry. Decided in lucas42/lucos_deploy_orb#105 and completed on 2026-04-17. A scheduled GitHub Actions workflow in `lucas42/.github` pulled a hand-inventoried list of base images from Docker Hub weekly (`node:25-alpine`, `python:3.15*-alpine`, `nginx:*`, `typesense/typesense:*`, and a handful of others), re-tagged them under `ghcr.io/lucas42/mirror/`, and pushed them to GHCR. The intent was to have CI consume from GHCR instead of Docker Hub, removing Docker Hub from the hot path of estate builds. The weekly refresh used ~20 Docker Hub pulls against the `lucas42` authenticated budget — comfortably under the limit.

The question of *how* CI would consume the mirror was tracked in lucas42/lucos_deploy_orb#106. Two consumption variants were considered:

- **Per-Dockerfile `ARG BASE_REGISTRY`** — explicit in-Dockerfile convention; required an estate-wide rollout touching every Dockerfile in ~30+ repos.
- **Orb-level `docker buildx build --build-context` injection** — rewrites `FROM` references transparently at build time by parsing each Dockerfile's FROM lines in the orb; no per-repo changes.

### The problem with the GHCR approach

Both consumption variants turned out to share a fatal defect that only surfaced once real PRs started failing against the GHCR mirror: **Dependabot**.

Dependabot is configured estate-wide to open PRs bumping base image tags to new upstream versions. When it proposes `node:25.2-alpine` and the mirror has only seeded `node:25.1-alpine`, GHCR returns 404 for the unseeded tag. The Dockerfile-ARG variant and the orb `--build-context` variant both redirect to `ghcr.io/lucas42/mirror/node:25.2-alpine`, which does not exist — every Dependabot PR becomes a broken build.

Fixing this within the GHCR approach would require either:

- **Exhaustive pre-seeding** — mirror every recent tag of every tracked base image, permanently. Tag space for images like `node` is in the hundreds. Impractical.
- **Reactive sync** — a workflow that watches Dependabot PRs and extends the mirror on demand. Additional coupling between Dependabot and the mirror, more ops surface to maintain, and a race between Dependabot firing and the mirror catching up.

The problem is intrinsic to using GHCR as a **curated store**: GHCR does not proxy Docker Hub on cache miss, it only serves what has been explicitly pushed. In a Dependabot-active estate, a curated store is a tag-chasing exercise that scales with estate activity.

## Decision

Replace the GHCR static mirror with a **self-hosted pull-through cache** running Docker's official `registry:2` image in proxy mode, hosted in the lucos estate as the new service `lucos_docker_mirror`.

A pull-through cache, unlike a curated store, fetches on demand. On cache miss it pulls the requested tag from Docker Hub using authenticated upstream credentials, caches the result, and serves subsequent pulls from the cache. Dependabot-driven tag bumps are handled transparently — any tag the build requests is fetched and cached the first time it is referenced.

### Consumption

CI clients consume the mirror via BuildKit's `registry-mirrors` configuration, which accepts a custom mirror host for `docker.io`. `registry:2` preserves Docker Hub's path layout (`/v2/library/node/manifests/25-alpine`), so clients can point at `mirror.l42.eu` without changes to image references anywhere.

This means:

- **No Dockerfile changes across the estate.** `FROM node:25-alpine` continues to work as-is; BuildKit routes the pull through the mirror.
- **No per-image logic in the orb.** A single `registry-mirrors` line in the `buildkitd.toml` config used by the orb's buildx setup covers every image in every Dockerfile.
- **No convention rollout.** There is nothing for `lucos_repos` to audit on a per-repo basis; the orb version is the single point of compliance.

### Hosting and routing

- **Service repo**: `lucas42/lucos_docker_mirror` (this repo) — follows the one-service-per-repo convention.
- **Host**: avalon.
- **Domain**: `mirror.l42.eu`, routed via `lucos_router` using the existing TLS/routing pattern. TLS certs via the shared `lucos_router_letsencrypt` volume.
- **Client auth**: htpasswd-based. A token stored in `lucos_creds` is presented by CI and by any other authorised client. The mirror is on the public internet (there is no internal trusted network in the lucos topology — see `references/network-topology.md`) so requiring auth is load-bearing, not defence-in-depth.
- **Upstream auth**: a Docker Hub personal access token for the `lucas42` account, stored in `lucos_creds`, used by `registry:2` to make authenticated pulls upstream. This is the token against which the 200/6h budget is counted — but because the mirror caches, the budget is only consumed on cache miss (i.e. once per new distinct tag), not on every build.
- **Storage**: a named Docker volume `lucos_docker_mirror_cache`, declared in this repo's `docker-compose.yml` and registered in `lucos_configy/config/volumes.yaml`. Backup policy TBD during implementation — a cache volume is rebuildable from Docker Hub, so backups may be unnecessary.
- **Refresh semantics**: `registry:2`'s `proxy.ttl` is set to a week. When the TTL elapses, the registry re-checks the manifest upstream on the next request for that tag. For moving tags (e.g. `node:25-alpine`) this may return a new manifest pointing at new layer digests, and the new layers are fetched on demand. For pinned tags (e.g. `node:25.1.0-alpine`) the manifest is identical and — because blobs are content-addressed — no layers are re-downloaded, so the upstream budget impact is a single manifest lookup per TTL window per pinned tag.

### Rollout

The GHCR mirror from lucas42/lucos_deploy_orb#105 **stays in place** until the pull-through cache is verified working in production. Specifically:

1. Build, deploy, and verify `lucos_docker_mirror` (tracked in lucas42/lucos#91).
2. Open a separate issue against `lucas42/lucos_deploy_orb` to update the orb's buildx setup to inject the `registry-mirrors` config. Roll out by bumping the orb version in each service's CircleCI config through normal estate channels.
3. Observe one week of build activity on the new mirror to catch any Docker Hub pull failures, cache sizing issues, or client auth problems.
4. Decide the fate of the GHCR mirror (decommission vs retain narrowly as a DR backup for core base images against extended Docker Hub outages). Defer this decision to a follow-up issue once the observation window is complete.
5. Close lucas42/lucos_deploy_orb#106 as superseded by this ADR.

## Alternatives considered

### Paid Docker Hub subscription

A Docker Hub Pro or Team plan grants effectively unlimited authenticated pulls for ~$5–7/month per user. This would require no infrastructure, no Dockerfile changes, and no orb changes — a single `docker login` step already present would suddenly have no budget ceiling.

Rejected because: it establishes an ongoing billing relationship with Docker Inc. and a continuing dependency on Docker Hub specifically. The lucos estate deliberately minimises recurring vendor costs and keeps infrastructure on components it controls. If Docker Hub pricing, terms, or availability changed unfavourably, the estate would face an urgent migration rather than a routine change.

### GHCR static mirror (superseded)

Already built as lucas42/lucos_deploy_orb#105. Incompatible with Dependabot-driven tag churn as described above. The mirror itself is left in place during migration as a belt-and-braces measure, but it is not the strategic answer.

### GHCR mirror + orb `--build-context`

An earlier proposal in lucas42/lucos_deploy_orb#106. It would have solved the "estate-wide Dockerfile rollout" problem — `--build-context` can redirect `FROM` references without touching Dockerfiles. But it does not solve the version-tracking problem; the redirected target is still GHCR, which is still a curated store. Same Dependabot bug.

### Third-party managed proxy (e.g. Google Artifact Registry, AWS ECR)

Cloud-hosted registries can proxy Docker Hub on demand and would solve the version-tracking problem without hosting new infrastructure in lucos. Rejected because: it introduces a new cloud-provider dependency that lucos does not currently have, trading the self-hosting burden for a vendor-lock burden and a recurring cost.

## Consequences

### Positive

- **Dependabot just works.** Any tag the build requests is fetched on demand; there is nothing for the estate to keep in sync.
- **No per-repo changes.** `FROM` lines stay as they are. Local `docker build` still works against Docker Hub directly (no auth required for the usual anonymous-pull case, no mirror dependency for development).
- **Single point of CI compliance.** The orb's buildx config is the only place the mirror is referenced.
- **Budget shaped to cache misses only.** Distinct new tag → one upstream pull, ever. Repeat builds → zero budget consumption.
- **Decouples estate from a specific external registry.** If in future the mirror needs to point at a different upstream (another registry, a different account), that is one config line.

### Negative

- **New hosted service to run.** `lucos_docker_mirror` is a live component with its own monitoring, patching, and availability requirements. This did not previously exist.
- **New single point of failure.** If the mirror service is down — for any reason, not just its own fault — estate CI pulls break until it is back. Previously, CI pulled from Docker Hub directly, so the availability dependency was Docker Hub's (external) rather than avalon's (ours). The new failure mode is "CI is broken because our mirror is sick."
- **Staleness within the TTL window.** Moving tags are re-checked weekly, not on every pull. If a build expects to see a same-day upstream tag update, it will see the previous week's manifest until the TTL elapses or the cache is manually invalidated. This is acceptable for base images but documented here explicitly so it is not surprising.
- **Client auth adds a small operational surface.** The htpasswd file needs to be kept in sync with credentials in `lucos_creds`; a leaked client token means someone else can consume our Docker Hub pull budget through our mirror.
- **The GHCR mirror becomes redundant** once migration is complete. It was built in #105 and working; retaining it for DR value has ongoing cost, decommissioning it means throwing away working infrastructure. Either outcome is a small additional tax.

### Reversibility

The decision is highly reversible. The orb's `registry-mirrors` config is a single line that can be removed to send CI back to Docker Hub direct, at which point the rate-limit problem re-emerges but nothing is broken. The GHCR mirror from #105 remains a working (if incomplete) fallback during this reversibility window. The mirror service itself can be decommissioned by stopping the container and the routing rule — no data loss, no downstream dependencies to untangle.

## References

- lucas42/lucos_deploy_orb#105 — original GHCR mirror decision (completed 2026-04-17, now superseded in scope).
- lucas42/lucos_deploy_orb#106 — Dockerfile migration to consume GHCR mirror (to be closed as superseded).
- Design discussion leading to this decision: https://github.com/lucas42/lucos_deploy_orb/issues/106#issuecomment-4268669372
- lucas42/lucos#91 — tracking issue for implementing this ADR.
- Docker `registry:2` proxy mode documentation: https://distribution.github.io/distribution/about/configuration/
- BuildKit `registry-mirrors` configuration: https://github.com/moby/buildkit/blob/master/docs/buildkitd.toml.md
