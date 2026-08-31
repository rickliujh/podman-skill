# `podman machine` — the macOS / Windows VM

On Linux, Podman runs containers directly. On macOS and Windows it runs a Fedora
CoreOS VM and the `podman` CLI is a remote client talking to it. Every path, port
and CPU instruction crosses that boundary.

```
macOS / Windows host              podman machine VM (Fedora CoreOS, linux)
  podman CLI  ──socket/npipe──▶   podman service ──▶ containers
  $HOME       ──virtiofs/9p──▶    $HOME                (nothing else shared)
  localhost:8080 ◀──gvproxy───    published port
```

## Providers

| OS | Default | Alternative |
|---|---|---|
| macOS | libkrun | applehv |
| Windows | WSL | Hyper-V |
| Linux | QEMU (rarely needed) | — |

libkrun supports GPU passthrough; applehv is the plain Apple Virtualization
framework. Switching providers requires recreating the machine.

## Lifecycle

```bash
podman machine init [--cpus 4 --memory 8192 --disk-size 100] [-v src:dst] [--import-native-ca] [--now]
podman machine start | stop | restart
podman machine list                    # NAME, VM TYPE, LAST UP, CPUS, MEMORY, DISK
podman machine inspect                 # mounts, socket path, resources
podman machine ssh [command]           # run inside the VM
podman machine cp local.txt machine:/path
podman machine set --cpus 6            # machine must be stopped
podman machine rm [name]               # destroys the VM and everything in it
podman machine reset                   # removes all machines
```

Machine config lives under `$XDG_CONFIG_HOME/containers/podman/machine/`.
The default machine is named `podman-machine-default`.

Machines here are **always rootless**. Do not create one with `--rootful` or
switch an existing one — rootful uses a separate image and volume store, so
everything silently vanishes from your working set, and it hands containers real
root inside the VM. If something seems to need it, the answer is almost always a
high port or a `keep-id` user namespace instead.

## 1. File sharing — the number one issue

**The default mount is `$HOME:$HOME`, and nothing else.** It comes from
`containers.conf`. Anything outside your home directory does not exist inside the
VM.

The failure is silent. Podman resolves the bind mount *inside the VM*, where the
path is missing, so it creates an empty directory and starts the container. You see
an app with no config, an empty database, or a web root serving nothing — and no
error anywhere.

```yaml
# macOS: works — under $HOME
volumes:
  - ./src:/app/src:z

# macOS: silently empty — outside $HOME
volumes:
  - /Volumes/external/data:/data:z
  - /opt/shared/config:/config:z
```

Diagnose by looking from the VM's side, not the host's:

```bash
podman machine inspect --format '{{range .Mounts}}{{.Source}} -> {{.Target}}{{"\n"}}{{end}}'
podman machine ssh ls -la /Volumes/external/data     # empty or missing = found it
```

Fix by recreating the machine with the extra mount — `-v` cannot be added to an
existing machine:

```bash
podman machine stop && podman machine rm
podman machine init -v "$HOME:$HOME" -v /Volumes/work:/Volumes/work --now
```

`podman machine rm` destroys images, containers and volumes in that VM. Confirm
before running it, and re-pull afterwards.

**The simplest rule: keep every project and its bind-mount sources under `$HOME`.**

Named volumes have no such problem — they live inside the VM and are always fine.
Prefer them for databases and caches, which also avoids slow cross-boundary I/O.

### Windows path specifics

Use Linux-style paths in Compose files. Under the WSL backend the Windows drive
appears as `/mnt/c/...`, and `podman-compose` has historically mangled such paths
by prefixing the drive again — another reason to use `podman compose`. Files on
`/mnt/c` also present all-permissive permissions and case-insensitive names, so a
container that depends on strict modes or case will behave differently than in CI.

### Performance

Cross-boundary file I/O is slow for many small files (`node_modules`, vendored
Ruby/PHP dependencies, build caches). Keep dependency directories in named volumes
rather than bind mounts:

```yaml
services:
  app:
    volumes:
      - ./:/app:z
      - node_modules:/app/node_modules   # stays inside the VM — fast
volumes:
  node_modules:
```

## 2. CPU architecture

The VM's kernel matches the host CPU. On Apple Silicon that is **arm64**, so an
image published only for `linux/amd64` fails at exec time:

```
exec /usr/local/bin/docker-entrypoint.sh: exec format error
```

This is not a corrupt image or a broken entrypoint. Check what you actually have:

```bash
podman image inspect <image> --format '{{.Architecture}}'
podman manifest inspect docker.io/library/postgres:16 | grep architecture
```

Options, best first:

1. Use a multi-arch tag — most official images publish `arm64`.
2. Pin the platform and accept emulation: `platform: linux/amd64` on the service.
   Expect a large slowdown; databases and JIT runtimes suffer most.
3. Build your own arm64 image.

When *building* on Apple Silicon for an amd64 production target, be explicit:

```bash
podman build --platform linux/amd64 -t myapp:amd64 .
podman build --platform linux/amd64,linux/arm64 --manifest myapp:latest .
```

Podman has also been reported to select the amd64 variant of a multi-arch image on
arm64 hosts in some versions; if a container is unexpectedly slow, check
`podman image inspect ... --format '{{.Architecture}}'` before assuming otherwise.

## 3. Networking

**Outbound to the host.** `host.containers.internal` resolves to the macOS/Windows
host from inside a container (Podman ≥ 5.3; `host.docker.internal` is also
provided). Use it to reach a database or API running natively on the laptop.

**Inbound published ports.** gvproxy forwards published ports from the VM to the
host automatically, so `ports: ["8080:80"]` is reachable at `localhost:8080` on the
host with no extra step. It binds localhost only.

**Ports below 1024** fail — Podman is rootless inside the VM. Map high and put a
proxy in front: `"8080:80"`, never `"80:80"`. When a low port genuinely cannot be
avoided, lower the threshold inside the VM — this keeps the engine rootless:

```bash
podman machine ssh 'echo "net.ipv4.ip_unprivileged_port_start=80" | sudo tee /etc/sysctl.d/99-ports.conf && sudo sysctl -p /etc/sysctl.d/99-ports.conf'
```

The setting persists across machine restarts but is lost if the machine is
recreated.

**Between services**, Compose creates a user-defined network where aardvark-dns
resolves service names. This normally just works; it breaks if containers were
attached to the built-in `podman` network, which has DNS disabled. After changing
networks under running containers, `podman network reload --all`.

**Corporate VPNs** (AnyConnect and similar) frequently break VM connectivity on
both macOS and Windows by capturing routes. Symptom: image pulls hang or DNS fails
inside containers while the host is online. Restart the machine after connecting.

## 4. Corporate proxy and TLS interception

The VM has its own trust store and its own network stack. Neither inherits from the
host, so a laptop that browses fine will still fail to pull images.

### Always create the machine with `--import-native-ca`

A TLS-intercepting proxy re-signs every certificate with a corporate CA. The host
trusts it; a fresh Podman machine does not:

```
Error: initializing source docker://alpine:latest: pinging container registry
registry-1.docker.io: tls: failed to verify certificate:
x509: certificate signed by unknown authority
```

`--import-native-ca` copies the host's trusted CAs into the VM's
`/etc/pki/ca-trust/source/anchors` and runs `update-ca-trust`. It is accepted by
both `init` and `set`, and re-imports **on every `podman machine start`**, so CAs
added to the laptop later are picked up after a restart.

```bash
# new machine — the default for this environment
podman machine init --import-native-ca -v "$HOME:$HOME" --now

# existing machine
podman machine set --import-native-ca
podman machine stop && podman machine start
```

It defaults to off, and a failed import only warns rather than blocking boot — so a
machine can start cleanly and still fail every pull. If certificate errors appear,
confirm the flag was actually used before debugging anything else.

Manual fallback, for a CA that is not in the host trust store:

```bash
podman machine ssh
sudo cp corporate-ca.crt /etc/pki/ca-trust/source/anchors/
sudo update-ca-trust
```

That copy is lost when the machine is recreated; `--import-native-ca` is not.

### Proxy environment variables

Three separate layers need the proxy, and they fail differently:

| Layer | What breaks without it | Where to set it |
|---|---|---|
| Podman engine in the VM | `podman pull`, `podman compose up` image fetches | `[engine] env` in the VM's `containers.conf` |
| Running containers | app can't reach the internet at runtime | `environment:` in the Compose service |
| Builds | `apt-get`/`npm install` hangs in a `RUN` step | `args:` under `build:`, consumed by `ARG` |

Podman machine picks up `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` from the host
environment at **init** time, so export them before creating the machine. To set
them on an existing machine, edit `containers.conf` inside the VM:

```bash
podman machine ssh
sudo mkdir -p /etc/containers
sudo tee -a /etc/containers/containers.conf <<'CONF'
[engine]
env = ["HTTP_PROXY=http://proxy.corp:8080", "HTTPS_PROXY=http://proxy.corp:8080", "NO_PROXY=localhost,127.0.0.1,.corp"]
CONF
```

Always include the Compose network and `host.containers.internal` in `NO_PROXY`,
or service-to-service traffic gets routed out to the proxy and fails.

In the Compose file, keep proxy values out of the committed YAML — reference the
host's variables so they stay per-developer:

```yaml
services:
  app:
    environment:
      - HTTP_PROXY
      - HTTPS_PROXY
      - NO_PROXY
    build:
      context: .
      args:
        - HTTP_PROXY
        - HTTPS_PROXY
```

A bare name with no `=` passes the value through from the invoking shell.

## 5. Resources

The VM is sized at creation and does not grow.

```bash
podman machine inspect --format '{{.Resources.CPUs}} cpu {{.Resources.Memory}}MiB {{.Resources.DiskSize}}GiB'
podman machine stop
podman machine set --cpus 6 --memory 12288 --disk-size 150
podman machine start
```

Disk can usually only grow, not shrink. A build failing with `no space left on
device` is almost always the VM's virtual disk; reclaim before resizing:

```bash
podman system df
podman system prune -a --volumes        # destructive — confirm first
```

If image pulls start failing with TLS or certificate-validity errors after the
laptop has been asleep, the VM's clock has drifted — `podman machine restart`.

## 6. Docker-tool compatibility (only if a third-party tool needs it)

`podman compose` needs none of this. It matters when some other tool speaks the
Docker API — Testcontainers, an IDE plugin, a CI runner.

- **macOS:** `sudo podman-mac-helper install` creates `/var/run/docker.sock`
  pointing at the machine socket, so Docker tooling works unmodified. Podman
  Desktop's *Docker Compatibility* setting does the same via GUI.
- **Windows:** no helper. Set `DOCKER_HOST=npipe:////./pipe/docker_engine`.
- Socket path for scripting:
  `podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}'`
