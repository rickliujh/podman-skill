#!/usr/bin/env bash
# Read-only diagnostic for Podman + Compose. Makes no changes.

ok()   { printf '  \033[32mok\033[0m    %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
tip()  { printf '        %s\n' "$*"; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

hdr "podman"
command -v podman >/dev/null 2>&1 || { bad "podman not installed"; exit 1; }
ok "$(podman --version)"

host_os=$(uname -s)
case "$host_os" in
  Darwin) vm=1; host_label="macOS $(uname -m)" ;;
  MINGW*|MSYS*|CYGWIN*) vm=1; host_label="Windows" ;;
  *) vm=0; host_label="Linux" ;;
esac
ok "host: $host_label"

# ---------------------------------------------------------------- machine (VM)
if [ "$vm" -eq 1 ]; then
  hdr "podman machine"
  if ! ml=$(podman machine list --format '{{.Name}}|{{.Running}}|{{.VMType}}|{{.CPUs}}|{{.Memory}}|{{.DiskSize}}|{{.Rootful}}' 2>&1); then
    bad "podman machine list failed: $ml"; exit 1
  fi
  if [ -z "$ml" ]; then
    bad "no machine exists — containers cannot run"
    tip "podman machine init --now"
    exit 1
  fi
  running=0
  while IFS='|' read -r name isrun vmtype cpus mem disk isroot; do
    [ -z "$name" ] && continue
    if [ "$isrun" = "true" ]; then
      running=1
      ok "$name ($vmtype) running — ${cpus} cpu, ${mem} mem, ${disk} disk"
    else
      warn "$name ($vmtype) stopped — podman machine start $name"
    fi
    if [ "$isroot" = "true" ]; then
      bad "$name is ROOTFUL — this environment requires rootless"
      tip "podman machine set --rootful=false   (image store differs; re-pull after)"
    fi
  done <<<"$ml"
  [ "$running" -eq 0 ] && { bad "no machine running"; tip "podman machine start"; exit 1; }

  hdr "vm file sharing"
  mounts=$(podman machine inspect --format '{{range .Mounts}}{{.Source}}|{{.Target}}{{"\n"}}{{end}}' 2>/dev/null)
  if [ -z "$mounts" ]; then
    warn "could not read mounts from podman machine inspect"
  else
    while IFS='|' read -r src tgt; do
      [ -z "$src" ] && continue
      ok "shared: $src -> $tgt"
    done <<<"$mounts"
  fi
  cwd=$(pwd -P)
  case "$cwd" in
    "$HOME"/*|"$HOME") ok "cwd is under \$HOME — bind mounts here are visible to the VM" ;;
    *) warn "cwd ($cwd) is outside \$HOME"
       tip "bind mounts from here resolve to an EMPTY dir inside the VM, with no error"
       tip "move the project under \$HOME, or recreate the machine with -v $cwd:$cwd" ;;
  esac
fi

  hdr "proxy / tls"
  ca=$(podman machine inspect 2>/dev/null | grep -io '"[a-z]*native[a-z]*ca[a-z]*"[[:space:]]*:[[:space:]]*[a-z]*' | head -1)
  case "$ca" in
    *true)  ok "--import-native-ca enabled" ;;
    *false) bad "--import-native-ca is OFF — required behind the corporate proxy"
            tip "podman machine set --import-native-ca && podman machine stop && podman machine start" ;;
    *)      warn "could not read native-CA setting from podman machine inspect"
            tip "behind a TLS-intercepting proxy the machine must be created with --import-native-ca" ;;
  esac
  if [ -n "${HTTPS_PROXY:-${https_proxy:-}}" ]; then
    ok "host proxy: ${HTTPS_PROXY:-$https_proxy}"
    [ -n "${NO_PROXY:-${no_proxy:-}}" ] \
      && ok "NO_PROXY=${NO_PROXY:-$no_proxy}" \
      || warn "NO_PROXY unset — service-to-service traffic may be routed to the proxy"
  else
    warn "no HTTPS_PROXY in this shell; export it before 'podman machine init' so the VM inherits it"
  fi

# ---------------------------------------------------------------------- engine
hdr "engine"
if ! info=$(podman info --format \
    '{{.Host.Arch}}|{{.Host.Security.Rootless}}|{{.Store.GraphDriverName}}|{{.Store.GraphRoot}}|{{.Host.RemoteSocket.Path}}|{{.Host.RemoteSocket.Exists}}|{{.Host.NetworkBackend}}' 2>&1); then
  bad "podman info failed — nothing else will work:"
  tip "$info"
  case "$info" in
    *"not supported over"*|*"overlay"*)
      tip ""
      tip "rootless overlay needs fuse-overlayfs on this filesystem:"
      tip "  install fuse-overlayfs, then set in ~/.config/containers/storage.conf:"
      tip "  [storage.options.overlay] mount_program = \"/usr/bin/fuse-overlayfs\"" ;;
  esac
  exit 1
fi
IFS='|' read -r arch rootless driver graphroot sockpath sockexists netbackend <<<"$info"
ok "container arch=$arch  rootless=$rootless  storage=$driver  network=$netbackend"
[ "$rootless" = "true" ] || bad "engine is running ROOTFUL — this environment requires rootless"

if [ "$vm" -eq 1 ] && [ "$(uname -m)" = "arm64" ] && [ "$arch" != "arm64" ]; then
  warn "host is arm64 but engine reports $arch — amd64 images will be emulated"
fi
if [ "$vm" -eq 1 ] && [ "$(uname -m)" = "arm64" ]; then
  tip "amd64-only images fail with 'exec format error'; add platform: linux/amd64"
fi

# --------------------------------------------------------------------- compose
hdr "compose"
if podman compose version >/dev/null 2>&1; then
  ok "podman compose -> $(podman compose version 2>/dev/null | head -1)"
else
  bad "'podman compose' unavailable or no provider installed"
  tip "needs Podman >= 4.7 and a docker-compose binary"
  tip "macOS/Windows: install the Compose extension in Podman Desktop"
fi
if command -v docker >/dev/null 2>&1; then
  if docker --version 2>/dev/null | grep -qi podman; then
    warn "'docker' is the podman-docker shim, not Docker"
  else
    warn "real 'docker' present — use 'podman compose', not 'docker compose'"
  fi
fi
[ "${COMPOSE_BAKE:-}" = "false" ] && ok "COMPOSE_BAKE=false" \
  || tip "set COMPOSE_BAKE=false DOCKER_BUILDKIT=0 before 'compose build' (Podman has no BuildKit)"

# ------------------------------------------------------------------- registries
hdr "registries"
reg_found=0
for f in "${XDG_CONFIG_HOME:-$HOME/.config}/containers/registries.conf" /etc/containers/registries.conf; do
  [ -f "$f" ] || continue
  reg_found=1
  if grep -q '^unqualified-search-registries' "$f" 2>/dev/null; then
    ok "$(grep '^unqualified-search-registries' "$f")"
  else
    warn "$f sets no unqualified-search-registries"
  fi
done
[ "$reg_found" -eq 0 ] && warn "no registries.conf — fully qualify images (docker.io/library/nginx)"

# ---------------------------------------------------------------- linux extras
if [ "$vm" -eq 0 ]; then
  hdr "linux host"
  if [ -d "$graphroot" ] && fstype=$(stat -f -c %T "$graphroot" 2>/dev/null); then
    case "$driver:$fstype" in
      overlay:btrfs|overlay:zfs|overlay:nfs*|overlay:tmpfs)
        command -v fuse-overlayfs >/dev/null 2>&1 \
          && ok "overlay on $fstype via fuse-overlayfs" \
          || warn "overlay on $fstype without fuse-overlayfs — install it if podman fails" ;;
      vfs:*) warn "vfs driver: correct but slow and disk-hungry" ;;
      *) ok "storage $driver on $fstype" ;;
    esac
  fi
  if [ "$rootless" = "true" ]; then
    grep -q "^$(id -un):" /etc/subuid 2>/dev/null \
      && ok "subuid: $(grep "^$(id -un):" /etc/subuid)" \
      || bad "no /etc/subuid entry — rootless podman cannot map users"
    port_start=$(sysctl -n net.ipv4.ip_unprivileged_port_start 2>/dev/null)
    [ "${port_start:-1024}" -le 80 ] 2>/dev/null \
      && ok "privileged ports allowed" \
      || warn "cannot publish ports <${port_start:-1024}; map high ports instead"
    loginctl show-user "$(id -un)" -p Linger 2>/dev/null | grep -q 'Linger=yes' \
      && ok "linger enabled" \
      || warn "linger off; containers stop at logout: loginctl enable-linger $(id -un)"
  fi
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    warn "SELinux Enforcing — bind mounts need :z or :Z"
  fi
else
  hdr "notes"
  ok "VM runs SELinux — keep :z on bind mounts"
  ok "published ports auto-forward to host localhost; publish high, <1024 is blocked"
fi

printf '\nGuide: ~/.claude/skills/podman-skill/SKILL.md\n'
