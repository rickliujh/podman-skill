---
name: podman
description: Use Podman correctly, especially for running docker-compose / Compose files on macOS and Windows, where containers live inside a `podman machine` VM. Load this whenever the task involves podman, `podman machine`, `podman compose`, a docker-compose.yml or compose.yaml on a machine without Docker Desktop, bind mounts that appear empty, `exec format error` on Apple Silicon, published ports not reachable from the host, container DNS between compose services, or registry TLS failures behind a corporate proxy. Also load when a `docker` or `docker compose` command fails and Podman is what is actually installed.
---

# Podman

Three facts explain nearly every surprise:

1. **Daemonless** — no background engine owns your containers.
2. **Always rootless** — the container's UID 0 is not the host's root. Never set
   up or suggest a rootful machine; every recipe here assumes rootless.
3. **On macOS and Windows, everything runs inside a Linux VM.** Your files, your
   ports and your CPU architecture are not automatically the container's.

Fact 3 dominates on Mac/Windows. Diagnose at the VM layer first — a bind mount
that "doesn't work" is usually a path the VM cannot see, not a Compose bug.

## 0. Orient before writing commands

```bash
podman --version
podman machine list                       # macOS/Windows: is there a running VM?
podman info --format '{{.Host.OS}}/{{.Host.Arch}} rootless={{.Host.Security.Rootless}}'
```

Or run the bundled diagnostic, which checks the VM, its mounts, the socket, the
architecture and the compose provider in one pass:

```bash
bash ~/.claude/skills/podman/scripts/podman-doctor.sh
```

`docker` on the box may be real Docker, the `podman-docker` shim, or absent.
Never infer which from the command existing.

## 1. Always use `podman compose`

**Use `podman compose` for every Compose operation.** Do not reach for
`docker compose`, `docker-compose`, or `podman-compose` directly, and do not set
`DOCKER_HOST` by hand.

```bash
podman compose up -d
podman compose ps
podman compose logs -f web
podman compose exec web sh
podman compose down
```

It takes the same flags and subcommands as Docker Compose, because underneath it
execs the real `docker-compose` binary — it starts the Podman socket and points the
provider at it for you. That is the whole reason to prefer it: correct wiring, no
environment to maintain, and behaviour that matches the Compose spec.

One prerequisite: a provider must be installed. If `podman compose` reports it
cannot find one, install Docker Compose (the plugin binary alone, not Docker
Engine) — Podman Desktop's *Compose* extension does this on macOS and Windows.

```bash
podman compose version        # confirms the provider it found
```

Silence the wrapper's "executing external compose provider" notice with
`PODMAN_COMPOSE_WARNING_LOGS=false`, or `compose_warning_logs = false` under
`[engine]` in `containers.conf`.

## 2. The VM layer (macOS / Windows)

`podman machine` runs a Fedora CoreOS VM. Providers: **libkrun** (macOS default)
or applehv; **WSL** (Windows default) or Hyper-V.

```bash
podman machine init          # create (see sizing and -v below)
podman machine start
podman machine ssh           # shell inside the VM — where containers actually run
podman machine inspect
```

Four things bite, in order of frequency. Full detail in `references/podman-machine.md`.

**Only `$HOME` is shared into the VM.** The default mount is `$HOME:$HOME` and
nothing else. A Compose bind mount pointing outside your home directory silently
resolves to a path that does not exist in the VM — you get an empty directory, not
an error. Mounts are fixed at creation, so adding one means recreating the machine:

```bash
podman machine rm
podman machine init -v "$HOME:$HOME" -v /Volumes/work:/Volumes/work
```

Keep projects under `$HOME` and this never comes up.

**Apple Silicon runs an arm64 kernel.** An image with only a `linux/amd64` binary
dies with `exec format error`. Prefer multi-arch images; otherwise declare the
platform and accept emulation:

```yaml
services:
  legacy:
    image: docker.io/library/some-amd64-only:1.0
    platform: linux/amd64
```

**Published ports reach the host, but only localhost.** gvproxy forwards
`ports:` from the VM out to the Mac/Windows host automatically. Ports below 1024
are blocked because Podman is rootless inside the VM. Publish high and put a proxy
in front — `"8080:80"`, not `"80:80"`. If a low port is unavoidable, raise the
limit inside the VM rather than making the machine rootful:

```bash
podman machine ssh 'echo "net.ipv4.ip_unprivileged_port_start=80" | sudo tee /etc/sysctl.d/99-ports.conf && sudo sysctl -p /etc/sysctl.d/99-ports.conf'
```

**Behind a corporate proxy, always create the machine with `--import-native-ca`.**
TLS-intercepting proxies re-sign certificates with a corporate CA that the VM does
not trust, so pulls fail with `x509: certificate signed by unknown authority`.
The flag imports the host's trust store into the VM on every start:

```bash
podman machine init --import-native-ca --now
podman machine set --import-native-ca      # existing machine; stop/start to apply
```

Proxy details in `references/podman-machine.md`.

**The VM has fixed CPU/memory/disk.** Builds that OOM or hit `no space left on
device` are usually VM limits, not container limits:

```bash
podman machine stop
podman machine set --cpus 4 --memory 8192 --disk-size 100
podman machine start
```

On Linux there is no VM and none of this section applies.

## 3. Compose file rules for Podman

The edits that make a Docker-authored file run unmodified. Per-key coverage in
`references/compose-interop.md`.

**Fully qualify every image.** Podman has no implicit `docker.io`. Without
`unqualified-search-registries`, `image: nginx` fails outright when there is no TTY
to prompt.

```yaml
image: docker.io/library/nginx:1.27      # not: nginx:1.27
```

**Label bind mounts `:z`.** The Mac/Windows VM is Fedora CoreOS with SELinux
enforcing, so this matters on every platform, not just RHEL hosts.

```yaml
volumes:
  - ./data:/var/lib/app:z    # z = shared, Z = private to one container
```

**Keep bind mount sources under `$HOME`** (macOS/Windows) — see §2.

**Fix file ownership with `keep-id`, not `chmod 777`.** Rootless containers run in
a user namespace, so files written to a bind mount land owned by a high subuid.

```yaml
userns_mode: "keep-id"                    # or "keep-id:uid=1000,gid=1000"
```

Repair existing ownership inside the namespace:
`podman unshare chown -R 1000:1000 ./data`.

**Disable Bake/BuildKit before building.** Podman's API implements the classic
build endpoint only.

```bash
export COMPOSE_BAKE=false DOCKER_BUILDKIT=0
```

Dockerfiles needing `RUN --mount=type=cache|secret` cannot be built through
Compose over Podman — build with `podman build` and reference the tag as `image:`.

**Reach the host** with `host.containers.internal` (Podman ≥ 5.3 also provides
`host.docker.internal`).

**Address services by service name**, never by generated container name —
`podman compose exec web sh`, not `podman exec myproj-web-1 sh`.

## 4. Errors whose text points the wrong way

Most Podman errors say what they mean. These few do not:

| Error says | Actually means |
|---|---|
| `exec format error` | Image has no build for the VM's architecture (arm64 on Apple Silicon). Not a corrupt binary or entrypoint bug. |
| Bind mount is empty, no error at all | Source path is outside `$HOME` and invisible to the VM (macOS/Windows). |
| `Docker Compose is configured to build using Bake, but buildx isn't installed` | Installing buildx will not help. Podman has no BuildKit; set `COMPOSE_BAKE=false`. |
| `Cannot connect to the Docker daemon at unix:///var/run/docker.sock` | Something invoked `docker` directly instead of `podman compose`. |
| `permission denied` on a bind mount | Two unrelated causes — missing `:z` (SELinux) *or* user-namespace UID shift. Check both. |
| `no space left on device` during build | Usually the VM's virtual disk, not the host disk. `podman machine set --disk-size`. |
| `x509: certificate signed by unknown authority` | Corporate TLS interception. The machine was created without `--import-native-ca`. |
| `: not found` / `^M` from an entrypoint script (Windows) | CRLF line endings. `git config --global core.autocrlf input`. |

Read Podman's *first* error, not the wrapper's — Compose stacks its own failure on
top, and the underlying message is the actionable one.

## Reference files

- `references/podman-machine.md` — the macOS/Windows VM: mounts, arch, networking, sizing, lifecycle
- `references/compose-interop.md` — per-key Compose support and migration checklist
- `scripts/podman-doctor.sh` — one-shot environment diagnostic
