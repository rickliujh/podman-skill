# Compose ↔ Podman interop

Always drive Compose through `podman compose`. This file covers how individual
Compose keys behave once you do.

## What `podman compose` runs underneath

`podman compose` is a dispatcher, not an implementation. It starts the Podman
socket, sets `DOCKER_HOST` for the child process, and execs the `docker-compose`
binary. Consequences worth knowing when reading errors:

- Spec coverage is Docker Compose's, so `profiles`, `extends`, `depends_on`
  conditions and `watch` all work.
- Containers are named `project-service-1`, matching Docker.
- Error messages come from Docker Compose and may mention Docker concepts
  (`buildx`, the daemon socket) that do not exist here.
- If it cannot find a provider, install the Docker Compose binary — Podman
  Desktop's *Compose* extension does this on macOS and Windows.

`podman compose version` prints which provider was found. `PODMAN_COMPOSE_PROVIDER`
pins an explicit path if several are installed.

## Key-by-key notes

| Compose key | Podman behaviour |
|---|---|
| `image:` | **No implicit `docker.io`.** Fully qualify, or set `unqualified-search-registries` in `registries.conf`. |
| `platform:` | Needed on Apple Silicon for amd64-only images; costs emulation speed. |
| `build:` | Classic builder only. No BuildKit, so no `--mount=type=cache/secret`. Set `COMPOSE_BAKE=false DOCKER_BUILDKIT=0`. |
| `volumes:` (bind) | Add `:z`/`:Z` — the Mac/Windows VM runs SELinux. On macOS/Windows the source must be under `$HOME` or the VM cannot see it. |
| `volumes:` (named) | Always work; live inside the VM. Prefer for databases, caches and `node_modules`. |
| `ports:` | Auto-forwarded VM → host by gvproxy, localhost only. Publish high — `<1024` is blocked for rootless. |
| `user:` | Sets the in-container UID; does **not** change host-side ownership. Pair with `userns_mode`. |
| `userns_mode: "keep-id"` | Podman-specific, the main fix for bind-mount ownership. Ignored by Docker, so safe to leave in a shared file. |
| `network_mode: host` | Means the *VM's* network on macOS/Windows, not the laptop's. Rarely what you want there. |
| `networks:` + aliases | Resolved by aardvark-dns on the Compose-created network. Works by default. |
| `depends_on` + `condition` | Fully supported. |
| `healthcheck:` | Supported. `start_period`/`start_interval` need a recent Podman. |
| `restart:` | Enforced by the Podman service, which stops with the machine. Containers come back on `podman machine start` only if the service restarts them; do not rely on it for anything important. |
| `deploy.resources` | Honoured, but capped by the VM's own CPU/memory. |
| `privileged`, `cap_add` | Rootless can only add capabilities the user already has — `privileged: true` is not host root. |
| `secrets:` | File-based Compose secrets work; external Swarm secrets do not. |
| `extra_hosts` | Works; `host.containers.internal` is available without declaring it. |
| `tmpfs:` | Works; counts against VM memory. |

## Migration checklist: Docker Compose file → Podman

1. Fully qualify all `image:` references.
2. Append `:z` to every bind mount.
3. Confirm all bind-mount sources are under `$HOME` (macOS/Windows).
4. Add `userns_mode: "keep-id"` to services writing into a bind mount.
5. Move any port `<1024` to a high port.
6. Add `platform: linux/amd64` to services whose images lack an arm64 build, or
   switch to multi-arch tags (Apple Silicon).
7. Move `node_modules`-style dependency directories into named volumes.
8. Strip BuildKit-only Dockerfile syntax, or pre-build with `podman build`.
9. Replace container-name scripting with `podman compose exec <service>`.

## Keeping one file portable across Docker and Podman

Keep `compose.yaml` Docker-clean and put Podman-only keys in an override, so the
same repo works for teammates on either engine:

```bash
podman compose -f compose.yaml -f compose.podman.yaml up -d
# or: export COMPOSE_FILE=compose.yaml:compose.podman.yaml
```

```yaml
# compose.podman.yaml
services:
  app:
    userns_mode: "keep-id"
    volumes:
      - ./data:/data:z
```

## Useful non-Compose commands

| Intent | Command |
|---|---|
| Fix bind-mount ownership | `podman unshare chown -R 1000:1000 ./data` |
| Inspect UID mapping | `podman unshare cat /proc/self/uid_map` |
| Check an image's architecture | `podman image inspect X --format '{{.Architecture}}'` |
| Re-sync DNS after network changes | `podman network reload --all` |
| Reclaim space | `podman system df` then `podman system prune -a --volumes` |
| Nuke and start over | `podman system reset` (destroys everything — confirm first) |
