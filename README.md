# rebox

**rebox** (*research sandbox*) creates rootless sandbox environments for
testing untrusted software and running AI agents on Linux. Each sandbox is an
[Apptainer](https://apptainer.org) image built from **reusable recipes**,
combined with a **Pixi workspace** in which you can keep installing conda
packages after the sandbox starts.

The only requirement on the host is [Pixi](https://pixi.sh). rebox is a conda
package; its dependencies (Python, Apptainer, squashfs-tools, git)
come from conda-forge.

## Installation

rebox is installed from the conda channel it is published on (see
[Building and publishing the package](#building-and-publishing-the-package)):

```
pixi global install -c <rebox-channel> -c conda-forge rebox
rebox --version
```

`pixi global` puts rebox and its tools in a private environment and only
exposes the `rebox` command. rebox can also be a dependency of any Pixi
environment (`pixi add rebox` in a workspace that lists its channel), then run
with `pixi run rebox`.

Then create a project, which holds your environments and your own recipes:

```
rebox init my-sandboxes --examples     # --examples: add the example environments
cd my-sandboxes
```

```
rebox list                             # recipes (all versions) and environments
rebox sources --fetch                  # recipe sources (local dirs, git repos)
rebox new my-env nodejs@1              # scaffold envs/my-env
rebox build my-env                     # lock versions, build image, install the Pixi workspace
rebox outdated my-env                  # newer recipe versions available?
rebox update my-env                    # move to them and rebuild the image
rebox shell my-env                     # interactive Pixi shell in the sandbox
rebox run my-env -- python x.py
rebox export my-env -o ../my-env-spec  # reproducible configuration
rebox pack my-env                      # offline bundle
rebox unpack my-env-bundle.tar         # redeploy it (on another machine)
```

## Host requirements

- Linux with unprivileged user namespaces enabled
  (`cat /proc/sys/user/max_user_namespaces` must be greater than 0).
- `/dev/fuse` usable by your user and `fusermount3` installed (standard on
  desktop distributions): images are mounted with squashfuse.
- Optional, for CPU/memory/PID limits: a systemd user session with cgroups v2
  (default on recent distributions).
- Pixi (to install rebox; everything else is installed by Pixi).

No root access is needed, neither to build nor to run sandboxes. If your user
is listed in `/etc/subuid` but `newuidmap` is not installed, builds
automatically fall back to a root-mapped user namespace.

## Project layout

A project, as created by `rebox init`:

```
rebox-defaults.toml              recipe sources, resources and recipes applied to every environment
recipes/                   the "local" recipe source: your recipes, <recipe>/<version>.toml
.sources/                  generated: local mirrors of git recipe sources
envs/<name>/
  rebox.toml             recipes + resources of this environment
  rebox.lock.toml        locked recipe versions (+ build info); commit it
  workspace/               mounted read-write at /rebox inside the sandbox
    pixi.toml, pixi.lock   the Pixi workspace of the sandbox
  .state/                  generated: image.sif, build.def, persistent home
```

rebox finds the project by looking for `rebox-defaults.toml` in the current
directory and its parents (or `REBOX_ROOT`).

`rebox.toml` lives outside the mounted workspace, so code running in the
sandbox cannot change its own configuration.

## Recipes

A recipe is a TOML file with a shell script run as (fake) root while the image
is built. Recipes declare their dependencies and can take arguments.

```toml
# recipes/nodejs/1.0.0.toml
description = "Node.js runtime from the official nodejs.org binaries"
requires = ["base"]                     # same syntax as in rebox.toml (see below)

post = '''
curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    | tar -xJ -C /usr/local --strip-components=1 --no-same-owner
'''

[args]                 # defaults, overridable per environment
NODE_VERSION = "24.20.0"

[environment]          # exported at runtime in the sandbox
NODE_OPTIONS = "--max-old-space-size=4096"
```

Keys must come before the `[args]` / `[environment]` tables (TOML rule).
Optional `runscript` sets the image's default command.

### Recipe sources

Recipes come from one or more **sources**: local directories or git
repositories, all with the same layout:

```
<source root>/                 a directory, or a git repo (optionally a subdir of it)
  source.toml                  optional: description = "..."
  <recipe>/
    <version>.toml             one file per published version
```

```
recipes/                       example: a local source
  source.toml
  base/1.0.0.toml
  nodejs/1.0.0.toml
  nodejs/1.1.0.toml
  vscode/1.0.0.toml
```

Sources are declared in `rebox-defaults.toml` (and can be added, overridden or
disabled with `enabled = false` in an environment's `rebox.toml`):

```toml
[sources.local]
path = "recipes"                 # relative to the project root, ~ and ${VAR} allowed

[sources.builtin]
builtin = true                   # the recipes shipped with rebox

[sources.team]
git = "https://git.example.org/team/sandbox-recipes.git"   # any URL git understands
ref = "main"                     # branch, tag or commit (default "main")
subdir = "recipes"               # optional: where the layout starts in the repo
```

Git sources are cloned once into `.sources/` (a bare mirror, using your usual
git credentials) and only fetched again by `sources --fetch`, `outdated` and
`update`, or when a locked commit is missing. `rebox sources` shows
each source and its current commit; `rebox list` shows every recipe
with its versions.

### Versions

Versions are dot-separated numbers, optionally with a suffix (`1.0.0`, `2.1`,
`1.0.0-rc1` < `1.0.0`). A recipe version is the version of the recipe file:
bump it whenever its script, arguments or dependencies change (pinned software
versions, like `NODE_VERSION`, are usually arguments). **A published version
must never be modified**: environments lock its sha256 and refuse to build if
it changes.

Recipes are referenced as `[source:]name[@constraint]`, in `build.recipes`,
`build.default_recipes` and in a recipe's `requires`:

| reference                    | meaning                                              |
|------------------------------|------------------------------------------------------|
| `nodejs`                     | newest version, from the first source that has it    |
| `team:nodejs`                | from source `team` only                              |
| `nodejs@1` / `nodejs@1.2.*`  | any 1.x / any 1.2.x                                  |
| `nodejs@==1.2.0`             | exactly 1.2.0                                        |
| `nodejs@>=1.2,<2`            | comma-separated clauses: `==`, `!=`, `>=`, `>`, `<=`, `<` |

An unqualified reference in `requires` is first looked up in the source of
the requiring recipe. A recipe name can only come from one source in a given
environment; constraints from several places on the same recipe are combined.

### Locking and updating

The first `build` (or `lock`) resolves every recipe and writes
`envs/NAME/rebox.lock.toml`: source, version, sha256, git commit, path and
arguments of each recipe. From then on, builds use exactly these versions,
even when newer ones are published; editing `rebox.toml` only re-resolves
what the change requires (e.g. a new recipe, a constraint the locked version
no longer satisfies). Commit the lock file with the environment.

```
rebox outdated NAME           # fetch sources, compare locked / allowed / newest
rebox update NAME             # newest allowed versions of all recipes, rebuild
rebox update NAME vscode      # only this recipe
rebox update NAME --dry-run   # show the changes only
rebox update NAME --no-build  # update the lock, rebuild later with `build`
```

`update` rebuilds the image when versions changed (the lock is only written
once the build succeeds) and does nothing otherwise. To move to a version
excluded by a constraint (`outdated` marks those), change the constraint in
`rebox.toml` first.

Rules for writing recipes:

- Scripts run with `/bin/sh` (`set -eu`), each in its own subshell, in
  dependency order. Every recipe is inlined in the generated `build.def`.
- Builds run in a rootless user namespace: files cannot be owned by other
  users. Use `tar --no-same-owner`, avoid `chown` to system users, and install
  packages with `apt-install PKG...` (provided by `base`) rather than
  `apt-get install`: it runs apt under `fakeroot` when the build has no
  `/etc/subuid` mapping, so that package scripts calling `chown` work.
- HTTP(S) proxy variables of the host are available during the build.
- Pin versions with `[args]` to keep images reproducible.

Recipes shipped with rebox (source `builtin`; a project's `local` source is
searched first, so it can override them):

| recipe           | content                                                        |
|------------------|----------------------------------------------------------------|
| `base`           | Debian/Ubuntu essentials, socat, the `apt-install` helper      |
| `pixi`           | Pixi (1.1.0; 1.0.0 is kept for locked environments)            |
| `nodejs`         | Node.js (official binaries)                                    |
| `claude-code`    | Claude Code CLI                                                |
| `analysis-tools` | strace, ltrace, gdb, file, binutils, lsof…                     |
| `build-tools`    | gcc, make, pkg-config                                          |
| `gui-base`       | GTK/NSS/X11 libraries and fonts for GUI and Electron apps      |
| `vscode`         | Visual Studio Code (Microsoft build), command `code`           |
| `vscodium`       | VSCodium (no Microsoft telemetry, Open VSX extensions), command `codium` |
| `rebox`          | rebox itself, `pixi global install` from neuro-forge (`REBOX_CHANNEL`, `REBOX_VERSION` args) |

## Environments and resources

```toml
# envs/ai-agent/rebox.toml
description = "Claude Code agent working on a single project directory"

[build]
recipes = ["claude-code"]                 # added after build.default_recipes

[build.recipe_args.claude-code]
CLAUDE_CODE_VERSION = "2.0.30"

[resources]
network = "allowlist"                     # "none", "allowlist" or "host"
cpus = 4
memory = "8G"
pids = 1024
home = "persistent"                       # or "ephemeral"

[resources.binds.project]                 # extra host paths
source = "~/code/my-project"
target = "/work"
mode = "rw"                               # "ro" (default) or "rw"
optional = false                          # true: skip if the source is missing

[resources.binds.gitconfig]               # bind inherited from rebox-defaults.toml…
enabled = false                           # …turned off here

[resources.allow.anthropic]               # domain group from rebox-defaults.toml…
enabled = true                            # …turned on here

[resources.allow.internal]                # a new group
domains = ["git.example.org", "*.pkg.example.org"]
ports = [443]

[resources.env]                           # only these variables reach the sandbox
ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"
```

Merging with `rebox-defaults.toml`: tables are merged key by key, values from the
environment win, lists are replaced. Set `inherit_defaults = false` to ignore
`rebox-defaults.toml` entirely. `rebox show NAME [--definition]` prints the
resolved result.

All resource keys:

| key              | default        | meaning                                                         |
|------------------|----------------|-----------------------------------------------------------------|
| `network`        | `"none"`       | `none`: loopback only; `allowlist`: HTTP(S) to allowed domains only (see below); `host`: host network |
| `cpus`           | none           | CPU quota (e.g. `2`, `0.5`)                                     |
| `memory`         | none           | memory limit, swap disabled (`"512M"`, `"4G"`)                  |
| `pids`           | none           | maximum number of processes                                     |
| `limits_backend` | `"auto"`       | `auto`, `systemd-run`, `apptainer` or `none` (see below)        |
| `home`           | `"persistent"` | `persistent`: `envs/NAME/.state/home`; `ephemeral`: new empty home per run |
| `writable_tmpfs` | `true`         | writable container filesystem, discarded on exit                |
| `display`        | `false`*       | access to the graphical server (X11, Wayland), see below        |
| `gpu`            | `false`*       | GPU access: `true` (auto), `"nvidia"`, `"amd"`, `"dri"`, `false` |
| `nested`         | `false`        | expose `/dev/fuse` so rebox can run sandboxes inside (see below) |
| `hostname`       | none           | hostname inside the sandbox                                     |
| `binds.NAME`     |                | `source`, `target`, `mode`, `optional`, `enabled`               |
| `env.NAME`       |                | value, may use `${VAR}` / `${VAR:-default}` from the host       |
| `apptainer_args` | `[]`           | extra raw `apptainer exec` options                              |
| `allow.NAME`     |                | `domains`, `ports` (default `[80, 443]`), `allow_private`, `enabled` |

\* `rebox init` sets `display` / `gpu` to `true` in `rebox-defaults.toml`
when it finds a display / a GPU on the host (override with `--display`,
`--no-display`, `--gpu`, `--no-gpu`).

What is always applied: user and mount namespaces, `--containall` (own
PID/IPC namespaces, minimal `/dev`, no host home, `/tmp` or working
directory), `--cleanenv` (no host
environment variables, proxies included, except those in `resources.env`),
the workspace mounted at `/rebox`, the sandbox user being your own
unprivileged user.

Limits: with `auto`, limits are enforced through `systemd-run --user --scope`
when a systemd user session exists. Otherwise the tool prints a note and runs
without limits; set `limits_backend = "systemd-run"` or `"apptainer"` to make
that an error instead.

## Network isolation

### The three modes

| `network`   | network namespace           | the sandbox can reach                                        |
|-------------|-----------------------------|--------------------------------------------------------------|
| `none`      | its own, loopback only      | nothing outside the sandbox                                  |
| `allowlist` | its own, loopback only      | HTTP(S) to the allowed domains, through rebox's proxy        |
| `host`      | the host's (shared)         | everything the host can reach, host loopback services included |

`--network MODE` on `shell` / `run` overrides the mode for one session.

### How it works: Linux namespaces, no root, no firewall rules

Every sandbox is started with `apptainer exec` as your own, unprivileged user.
Apptainer creates a **user namespace** (in which your user can create the
other namespaces without privileges) and rebox asks for:

- a **mount** namespace (always): the sandbox only sees its image and the
  paths rebox binds into it;
- **PID** and **IPC** namespaces (`--containall`): host processes and their
  shared memory are invisible;
- a **UTS** namespace (`hostname`);
- with `none` and `allowlist`, a **network** namespace
  (`--net --network none`).

The **cgroup** namespace is shared with the host (the sandbox can read the
names of the host's cgroups, not change them).

A new network namespace has its own network stack, which only holds the
loopback interface `lo`: no other interface, no route, no DNS server (the
`/etc/resolv.conf` copied from the host points to addresses that are
unreachable). So:

- there is no path to the outside at all, whatever the program does: raw
  sockets, UDP, ICMP or a hand-written TCP client fail with "network
  unreachable";
- the host's loopback services (`127.0.0.1` of the host: databases, Jupyter,
  development servers, local model servers...) are **not** reachable: the
  sandbox's `127.0.0.1` is its own;
- abstract Unix sockets (names starting with `@`, e.g. `@/tmp/.X11-unix/X0`)
  belong to the host network namespace and are not reachable either;
- filesystem Unix sockets do not depend on the network namespace: they are
  reachable only if their file is visible, and rebox binds only the ones it
  needs (the allowlist proxy socket, and the display sockets with
  `display = true`).

There are no virtual interfaces, no `iptables`/`nftables` rules and nothing
to clean up: the namespace disappears with the sandbox. Even code that gains
"root" inside the sandbox's user namespace can only configure its own, empty
network namespace.

rebox deliberately does not give the sandbox a user-mode network stack
(slirp4netns, pasta) filtered by IP address: allowlisting IP addresses does
not work with CDNs and changing DNS records, and it leaves DNS as a channel to
send data out. Filtering by domain name needs a proxy anyway.

### Allowlist mode: the filtering proxy

With `network = "allowlist"` the sandbox is isolated exactly like with
`none`, plus one way out: a Unix socket leading to a filtering HTTP proxy that
rebox runs on the host, in the host network namespace, for the duration of
the session.

```
sandbox (own network namespace, lo only)                     host
  curl, pip, pixi, git, claude ...
     │ HTTP(S)_PROXY=http://127.0.0.1:3128
     ▼
  socat relay 127.0.0.1:3128 ── Unix socket ──► allowlist proxy ──► Internet
                                (bind mount)     checks host:port
                                                 resolves DNS
                                                 logs every decision
```

1. For each session, rebox starts the proxy inside the rebox process. It
   listens on a Unix socket (mode `0600`) in a new private temporary
   directory, bind-mounted in the sandbox at `/run/rebox-net/proxy.sock`. The
   proxy stops, and the directory is removed, when the session ends.
2. Before your command, a `socat` relay starts in the sandbox: it listens on
   the sandbox's own `127.0.0.1:3128` and forwards each connection to the
   socket.
3. `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` (and their lowercase forms)
   point to `http://127.0.0.1:3128`; `NO_PROXY` covers the sandbox loopback;
   `NODE_USE_ENV_PROXY=1` makes Node.js `fetch` use the proxy too. Proxy
   variables of the host are never passed.
4. For each connection, the proxy reads the request:
   - HTTPS (and any TLS protocol) arrives as `CONNECT host:port`: the proxy
     checks the host name and port, then relays the encrypted stream
     untouched (no TLS interception, no certificate to install);
   - plain HTTP arrives as `GET http://host/path`, checked the same way;
   - anything else is refused (`400`), unlisted destinations get `403`.
5. Names are resolved by the proxy, on the host: the sandbox never talks to a
   DNS server, which also prevents DNS-based exfiltration. Allowed names that
   resolve to loopback, private or link-local addresses are refused unless
   the group sets `allow_private = true` (to reach a local service on
   purpose).

Domains come from the enabled `[resources.allow.*]` groups. `example.com`
matches only that name; `*.example.com` matches any subdomain (not the apex).
`ports` defaults to `[80, 443]`. `rebox-defaults.toml` defines the groups
`conda` (enabled), `pypi`, `npm`, `github`, `anthropic`, `neuro-forge`,
`vscode-marketplace` and `open-vsx` (disabled; enable them per environment).

Every decision (`ALLOW`, `DENY`, `ERROR` with host and port) is appended to
`envs/NAME/.state/network.log`, and a summary of refused destinations is
printed when the session ends: the easiest way to build an allowlist is to
run once and read the log.

If the host itself must use an HTTP proxy (`https_proxy` in the environment of
rebox), allowed connections are chained through it. The upstream proxy then
resolves names, so the private-address check does not apply.

The relay needs `socat` in the image (installed by the `base` recipe).

### Checking it yourself

```
rebox run NAME -- cat /proc/net/dev              # interfaces: only lo
rebox run NAME -- readlink /proc/self/ns/net     # differs from the host's
readlink /proc/self/ns/net                       # (on the host)
rebox run NAME --network allowlist -- curl -sS https://example.org
                                                 # CONNECT tunnel failed, response 403
tail envs/NAME/.state/network.log                # DENY example.org:443 ...
```

### Limits

- In `allowlist` mode, only programs honouring `HTTP(S)_PROXY` reach the
  network (curl, git over HTTPS, pip, npm, pixi, Claude Code...). Others
  fail: they cannot bypass the filter. SSH, UDP and QUIC (HTTP/3) do not work.
- Filtering is per domain: an allowed domain can still be used to send data
  out (e.g. pushing to github.com, or to any site hosted under an allowed
  wildcard). Keep each environment's groups to what it really needs.
- The proxy sees host names and ports, not the content of HTTPS traffic.
- `network = "host"` shares the host network namespace: the sandbox reaches
  everything the host can, including services listening on the host's
  `127.0.0.1` and abstract Unix sockets. Use it only for trusted software.
- Namespaces share the host kernel: a kernel vulnerability can break out of
  any namespace-based sandbox (see [Security notes](#security-notes)).

## Display and GPU

### Display (`display = true`)

Gives the sandbox the graphical server of the session that runs rebox, in
every network mode (`none`, `allowlist`, `host`):

- **X11**: the local socket directory `/tmp/.X11-unix` (read-only) and your
  Xauthority file are mounted, `DISPLAY` and `XAUTHORITY` are set. On Wayland
  desktops this is XWayland, used by most toolkits and by Electron apps.
  X11 over TCP (e.g. `ssh -X`, `DISPLAY=localhost:10.0`) only works with
  `network = "host"`.
- **Wayland**: only the compositor socket is exposed, as `wayland-0` in a
  private, empty `XDG_RUNTIME_DIR` (`/run/rebox-runtime`). The rest of your
  runtime directory (D-Bus, keyring, ssh/gpg agents...) stays hidden.

Displays are looked up when the sandbox starts (`DISPLAY`, `WAYLAND_DISPLAY`,
`XDG_RUNTIME_DIR`, `XAUTHORITY` of the shell running rebox); `rebox show NAME`
tells what was found. `rebox init` enables `display` in `rebox-defaults.toml`
when it finds one, so turn it off (`display = false`) in environments running
untrusted code, as the `malware-lab` example does.

**Security:** any program connected to your display can read keystrokes and
take screenshots of your other windows. Never give a display to untrusted
software. For a stronger separation, run a nested X server (e.g.
`Xephyr :2`) and start rebox with `DISPLAY=:2`.

### GPU (`gpu = ...`)

| value      | effect                                                               |
|------------|----------------------------------------------------------------------|
| `true`     | use the GPUs found when the sandbox starts (any of the below)        |
| `"nvidia"` | `--nv`: host driver libraries, `nvidia-smi`, `/dev/nvidia*`          |
| `"amd"`    | `--rocm`: host ROCm libraries, `/dev/kfd`, `/dev/dri`                |
| `"dri"`    | `/dev/dri` render nodes (Intel/AMD/Mesa acceleration, e.g. OpenGL, VA-API) |
| `false`    | no GPU                                                               |

NVIDIA and AMD support rely on Apptainer's `--nv` / `--rocm`, which take the
driver libraries from the host. `rebox init` enables `gpu = true` when it
finds a GPU (`/dev/nvidiactl`, `/dev/kfd` or `/dev/dri/renderD*`). A GPU that
is requested but not found produces a note, not an error, so environments
stay portable.

## rebox inside a sandbox

An environment can contain rebox itself (`rebox` recipe), e.g. to unpack and
run bundles from inside a sandbox. Apptainer mounts images with FUSE, and
`/dev/fuse` is not part of the minimal `/dev` of a sandbox, so:

- set `nested = true` in the outer environment: `/dev/fuse` is exposed and
  nested sandboxes start as fast as on the host (all network modes work,
  `allowlist` included, nested in each other);
- without it, the inner rebox still works: it extracts the image to a
  temporary directory (`envs/NAME/.state/tmp`) at every start, which is slower
  and needs disk space, and says so.

Building images (`rebox build`) inside a sandbox is not supported. FUSE
slightly widens the kernel interface reachable from the sandbox: keep
`nested = false` for untrusted software.

## GUI applications (VS Code)

The `vscode` / `vscodium` recipes only need display access
(see `envs/vscode/rebox.toml`):

```toml
[build]
recipes = ["vscode"]                      # or "vscodium"

[resources]
display = true
```

Then `rebox run vscode --no-pixi -- code /work`, or open a
`rebox shell vscode` and run `code` from there (the Pixi environment is then
visible to VS Code's terminals and Python extension).

- `code` / `codium` are wrappers that start the editor with `--no-sandbox`
  (Chromium's own sandbox cannot work inside an unprivileged container; the
  sandbox provides the isolation) and create default settings disabling
  automatic updates and telemetry. Update by changing `VSCODE_VERSION` /
  `VSCODIUM_VERSION` and rebuilding.
- Extensions: enable the `vscode-marketplace` allow group (VS Code) or
  `open-vsx` (VSCodium); check `network.log` if an extension needs more.
  Extensions are stored in the sandbox home (`envs/NAME/.state/home`).
- Settings Sync, GitHub sign-in and Copilot need more domains (e.g. the
  `github` group); add them only if you want them.

## Inside the sandbox

`rebox shell` opens `pixi shell` on `/rebox/pixi.toml`: the software from
the recipes is on the `PATH`, together with the Pixi environment. Packages added
with `pixi add` are stored in `envs/NAME/workspace` and persist across runs.
Installing packages needs network: use `--network allowlist` (the `conda`
group is enabled by default) or `--network host` (then pass your proxy through
`resources.env` if you have one).

`rebox run NAME -- CMD` runs `CMD` through `pixi run` in the workspace;
add `--no-pixi` to run it directly.

## Reproducing an environment on another machine

`envs/NAME/rebox.lock.toml` pins every recipe (source, version, sha256, git
commit) and records build information (hash of the generated definition,
image and `pixi.lock` hashes, Apptainer version). With the project, the lock
and access to the sources, `rebox build NAME` rebuilds the same
definition on another machine.

`rebox export NAME -o DIR` writes a standalone project in `DIR`:
the fully resolved `rebox.toml` (defaults merged in, every recipe pinned with
`@==version`), a copy of the exact recipe versions it uses as local sources
(`recipes/<source>/<recipe>/<version>.toml`, so no git access is needed),
the workspace `pixi.toml` / `pixi.lock` and the generated `build.def`. The
lock file records the rebox version used. On the other machine, with rebox
installed:

```
cd DIR && rebox build NAME
```

For identical images, pin everything that is fetched at build time:

- the base image by digest (`docker://debian@sha256:…`),
- Debian packages with `recipe_args.base.APT_SNAPSHOT` (snapshot.debian.org),
- versions in recipe arguments (`PIXI_VERSION`, `NODE_VERSION`, …).

The Pixi workspace is pinned by its `pixi.lock`. Recipes from the `builtin` source are locked like the
others (version and sha256) and also record the rebox version they came from.

## Offline bundle

```
rebox pack NAME -o NAME-bundle.tar          # on a machine where NAME is built
rebox unpack NAME-bundle.tar [DIRECTORY]    # on the target machine, offline
cd NAME-bundle && rebox shell NAME
```

The bundle contains only the environment: the exported configuration (with
the exact recipe versions), the built image, the installed workspace (`.pixi`
included; conda prefixes stay valid since the workspace is always mounted at
`/rebox`) and a `rebox-bundle.toml` manifest (environment name, rebox version,
image sha256). The persistent home is left out unless you pass
`--include-home` (it may contain credentials).

rebox itself is not in the bundle: it must be installed on both machines.
For an offline target, install it from a local copy of the channels (e.g. a
directory channel made with `pixi publish --to file:///path`, plus the
conda-forge packages it needs). `rebox unpack` needs no network: it checks the
manifest and the image checksum, extracts the bundle into a new directory
(`./<bundle name>` by default) which is a normal rebox project, and prints a
note if the bundle was made with a different rebox version.

## Security notes

Apptainer isolates processes with Linux namespaces; it is not a virtual
machine. Untrusted code shares the host kernel, and runs as your user (with
your user's limits) inside the sandbox. For hostile malware, prefer
`network = "none"` (or a minimal `allowlist`), `home = "ephemeral"`,
`display = false`, `gpu = false`, `nested = false`, no read-write binds, never
`network = "host"`, and run the whole thing in a dedicated user account or VM.
See [Network isolation](#network-isolation) for what each network mode
blocks. Never bind directories with
secrets (SSH keys, cloud credentials) into a sandbox.

## Building and publishing the package

`pyproject.toml` is the only manifest of the repository: `[project]` holds the
package metadata (name, version, `rebox` entry point) and `[tool.pixi.*]` the
Pixi development workspace and the conda package build (`pixi-build-python`
backend, conda run-dependencies in `[tool.pixi.package.run-dependencies]`).
The package is `noarch: python`: one build serves every Linux architecture.

```
pixi run build-package                       # pixi publish --target-dir dist -> dist/rebox-<version>-<build>.conda
pixi publish --to file:///srv/conda-channel  # build + add to a local channel (indexed)
pixi publish --to https://prefix.dev/<channel>
pixi publish --to https://anaconda.org/<owner>/<label>
```

Remote channels need credentials (`pixi auth login ...`). A local or shared
directory channel is enough for a team or an air-gapped site:
`pixi global install -c file:///srv/conda-channel -c conda-forge rebox`.

To release a version, bump `version` in `[project]`. Runtime dependencies
are conda packages only (`[project].dependencies` stays empty, so that Pixi
never has to resolve anything from PyPI). The recipes shipped with rebox are package
data (`src/rebox/data/recipes`): add new versions there, never modify a
published one.

## Development

```
pixi run rebox --help        # run rebox from the sources
pixi run -e test test        # run the tests
```

Sources: `src/rebox/` (CLI, configuration, recipe resolution, image build,
allowlist proxy), `src/rebox/data/` (built-in recipes, `rebox-defaults.toml`
template, example environments), `tests/`.
