# rebox

**rebox** (*research sandbox*) creates rootless sandbox environments for
testing untrusted software and running AI agents on Linux. Each sandbox is an
[Apptainer](https://apptainer.org) image built from **reusable recipes**,
combined with a **Pixi workspace** in which you can keep installing conda
packages after the sandbox starts.

The only requirement on the host is [Pixi](https://pixi.sh). rebox is a conda
package; its dependencies (Python, Apptainer, squashfs-tools, pixi-pack, git)
come from conda-forge.

## Installation

rebox is installed from the conda channel it is published on (see
[Building and publishing the package](#building-and-publishing-the-package)):

```
pixi global install -c <rebox-channel> -c conda-forge rebox
rebox --version
```

`pixi global` puts rebox and its tools in a private environment and only
exposes the `rebox` command. Then create a project, which holds your
environments and your own recipes:

```
rebox init my-sandboxes --examples     # --examples: add the example environments
cd my-sandboxes
```

`rebox init` writes a `pixi.toml` depending on rebox (from the channel rebox
was installed from; add others with `--channel URL`). Running `pixi install`
there pins the rebox version in `pixi.lock`: `pixi run rebox ...` then uses
exactly that version, and `rebox pack` ships that environment in offline
bundles. Both `rebox ...` and `pixi run rebox ...` work in a project.

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
pixi.toml, pixi.lock       host tools: rebox (and its dependencies)
defaults.toml              recipe sources, resources and recipes applied to every environment
recipes/                   the "local" recipe source: your recipes, <recipe>/<version>.toml
.sources/                  generated: local mirrors of git recipe sources
envs/<name>/
  sandbox.toml             recipes + resources of this environment
  sandbox.lock.toml        locked recipe versions (+ build info); commit it
  workspace/               mounted read-write at /sandbox inside the sandbox
    pixi.toml, pixi.lock   the Pixi workspace of the sandbox
  .state/                  generated: image.sif, build.def, persistent home
```

rebox finds the project by looking for `defaults.toml` in the current
directory and its parents (or `REBOX_ROOT`).

`sandbox.toml` lives outside the mounted workspace, so code running in the
sandbox cannot change its own configuration.

## Recipes

A recipe is a TOML file with a shell script run as (fake) root while the image
is built. Recipes declare their dependencies and can take arguments.

```toml
# recipes/nodejs/1.0.0.toml
description = "Node.js runtime from the official nodejs.org binaries"
requires = ["base"]                     # same syntax as in sandbox.toml (see below)

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

Sources are declared in `defaults.toml` (and can be added, overridden or
disabled with `enabled = false` in an environment's `sandbox.toml`):

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
`envs/NAME/sandbox.lock.toml`: source, version, sha256, git commit, path and
arguments of each recipe. From then on, builds use exactly these versions,
even when newer ones are published; editing `sandbox.toml` only re-resolves
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
`sandbox.toml` first.

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

Recipes shipped with rebox (source `builtin`, all at version 1.0.0; a
project's `local` source is searched first, so it can override them):

| recipe           | content                                                        |
|------------------|----------------------------------------------------------------|
| `base`           | Debian/Ubuntu essentials, socat, the `apt-install` helper      |
| `pixi`           | Pixi                                                           |
| `nodejs`         | Node.js (official binaries)                                    |
| `claude-code`    | Claude Code CLI                                                |
| `analysis-tools` | strace, ltrace, gdb, file, binutils, lsof…                     |
| `build-tools`    | gcc, make, pkg-config                                          |
| `gui-base`       | GTK/NSS/X11 libraries and fonts for GUI and Electron apps      |
| `vscode`         | Visual Studio Code (Microsoft build), command `code`           |
| `vscodium`       | VSCodium (no Microsoft telemetry, Open VSX extensions), command `codium` |

## Environments and resources

```toml
# envs/ai-agent/sandbox.toml
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

[resources.binds.gitconfig]               # bind inherited from defaults.toml…
enabled = false                           # …turned off here

[resources.allow.anthropic]               # domain group from defaults.toml…
enabled = true                            # …turned on here

[resources.allow.internal]                # a new group
domains = ["git.example.org", "*.pkg.example.org"]
ports = [443]

[resources.env]                           # only these variables reach the sandbox
ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"
```

Merging with `defaults.toml`: tables are merged key by key, values from the
environment win, lists are replaced. Set `inherit_defaults = false` to ignore
`defaults.toml` entirely. `rebox show NAME [--definition]` prints the
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
| `gpu`            | `false`        | NVIDIA GPU access (`--nv`)                                      |
| `hostname`       | none           | hostname inside the sandbox                                     |
| `binds.NAME`     |                | `source`, `target`, `mode`, `optional`, `enabled`               |
| `env.NAME`       |                | value, may use `${VAR}` / `${VAR:-default}` from the host       |
| `apptainer_args` | `[]`           | extra raw `apptainer exec` options                              |
| `allow.NAME`     |                | `domains`, `ports` (default `[80, 443]`), `allow_private`, `enabled` |

What is always applied: `--containall` (own PID/IPC namespaces, minimal
`/dev`, no host home, `/tmp` or working directory), `--cleanenv` (no host
environment variables, proxies included, except those in `resources.env`),
the workspace mounted at `/sandbox`, the sandbox user being your own
unprivileged user.

Limits: with `auto`, limits are enforced through `systemd-run --user --scope`
when a systemd user session exists. Otherwise the tool prints a note and runs
without limits; set `limits_backend = "systemd-run"` or `"apptainer"` to make
that an error instead.

## Allowlist network mode

With `network = "allowlist"` the sandbox has no network interface except
loopback, exactly like `none`. Its only way out is a filtering HTTP proxy that
the tool runs on the host for the duration of the session:

```
sandbox (no network)                              host
  curl, pip, pixi, git, claude ...
     │ HTTP(S)_PROXY=http://127.0.0.1:3128
     ▼
  socat relay 127.0.0.1:3128 ── Unix socket ──► allowlist proxy ──► Internet
                                (bind mount)     checks host:port
```

- HTTPS goes through `CONNECT host:port`: the proxy checks the host name and
  relays the encrypted stream untouched (no TLS interception, no certificate
  to install). Plain HTTP requests are checked the same way.
- Domains come from the enabled `[resources.allow.*]` groups. `example.com`
  matches only that name; `*.example.com` matches any subdomain (not the apex).
  `defaults.toml` defines the groups `conda` (enabled), `pypi`, `npm`,
  `github` and `anthropic` (disabled; enable them per environment).
- DNS is resolved on the host: the sandbox has no resolver, which also
  prevents DNS-based exfiltration. Allowed names that resolve to loopback,
  private or link-local addresses are refused unless the group sets
  `allow_private = true` (to reach a local service).
- Every decision is appended to `envs/NAME/.state/network.log`, and a summary
  of refused destinations is printed when the session ends: the easiest way to
  build an allowlist is to run once and read the log.
- If the host itself must use an HTTP proxy (`https_proxy` in the environment
  of the tool), allowed connections are chained through it.
- The relay needs `socat` in the image (installed by the `base` recipe).
- `--network allowlist` on `shell` / `run` switches any environment to this
  mode for one session.

Limits: only programs honouring `HTTP(S)_PROXY` can reach the network (curl,
git over HTTPS, pip, npm, pixi, Claude Code…; Node.js `fetch` needs
`NODE_USE_ENV_PROXY=1`, which the tool sets). Others simply fail, they cannot
bypass the filter. SSH, UDP and QUIC do not work. Filtering is per domain, so
an allowed domain can still be used to send data out (e.g. pushing to
github.com); restrict groups to what each environment really needs.

## GUI applications (VS Code)

Graphical programs need access to your display, given with ordinary binds and
environment variables (see `envs/vscode/sandbox.toml`):

```toml
[build]
recipes = ["vscode"]                      # or "vscodium"

[resources.binds.x11]
source = "/tmp/.X11-unix"
target = "/tmp/.X11-unix"
mode = "ro"

[resources.binds.xauthority]
source = "${XAUTHORITY:-~/.Xauthority}"
target = "/run/sandbox-x11/Xauthority"
mode = "ro"
optional = true

[resources.env]
DISPLAY = "${DISPLAY}"
XAUTHORITY = "/run/sandbox-x11/Xauthority"
```

Then `rebox run vscode --no-pixi -- code /work`, or open a
`rebox shell vscode` and run `code` from there (the Pixi environment is then
visible to VS Code's terminals and Python extension). On Wayland desktops this
works through XWayland.

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
- **Security:** any program connected to your X server can read keystrokes and
  screenshot other windows. Never give a display to untrusted software. For
  a stronger separation, run a nested X server (e.g. `Xephyr :2`) and give
  the sandbox `DISPLAY=:2` only.

## Inside the sandbox

`rebox shell` opens `pixi shell` on `/sandbox/pixi.toml`: the software from
the recipes is on the `PATH`, together with the Pixi environment. Packages added
with `pixi add` are stored in `envs/NAME/workspace` and persist across runs.
Installing packages needs network: use `--network allowlist` (the `conda`
group is enabled by default) or `--network host` (then pass your proxy through
`resources.env` if you have one).

`rebox run NAME -- CMD` runs `CMD` through `pixi run` in the workspace;
add `--no-pixi` to run it directly.

## Reproducing an environment on another machine

`envs/NAME/sandbox.lock.toml` pins every recipe (source, version, sha256, git
commit) and records build information (hash of the generated definition,
image and `pixi.lock` hashes, Apptainer version). With the project, the lock
and access to the sources, `rebox build NAME` rebuilds the same
definition on another machine.

`rebox export NAME -o DIR` writes a standalone project in `DIR`:
the fully resolved `sandbox.toml` (defaults merged in, every recipe pinned with
`@==version`), a copy of the exact recipe versions it uses as local sources
(`recipes/<source>/<recipe>/<version>.toml`, so no git access is needed),
the workspace `pixi.toml` / `pixi.lock`, the generated `build.def`, and the
project's `pixi.toml` / `pixi.lock` (so the same rebox version is used). On the
other machine:

```
cd DIR && pixi run rebox build NAME
```

For identical images, pin everything that is fetched at build time:

- the base image by digest (`docker://debian@sha256:…`),
- Debian packages with `recipe_args.base.APT_SNAPSHOT` (snapshot.debian.org),
- versions in recipe arguments (`PIXI_VERSION`, `NODE_VERSION`, …).

The Pixi workspace is pinned by its `pixi.lock`; rebox and the host tools by
the project's `pixi.lock`. Recipes from the `builtin` source are locked like the
others (version and sha256) and also record the rebox version they came from.

## Offline bundle

```
rebox pack NAME -o NAME-bundle.tar
```

The bundle contains the exported configuration, the built image, the installed
workspace (`.pixi` included; conda prefixes stay valid since the workspace is
always mounted at `/sandbox`), and the project's Pixi environment (rebox,
Apptainer, Python...) packed by `pixi-pack` as a self-extracting executable.
The project's `pixi.toml` must therefore depend on rebox (as created by
`rebox init`) and be installed (`pixi.lock` present). The persistent home is left out unless you pass
`--include-home` (it may contain credentials).

On the target machine (no internet, no Pixi, no rebox needed):

```
tar xf NAME-bundle.tar && cd NAME-bundle
./install.sh
./rebox shell NAME
```

## Security notes

Apptainer isolates processes with Linux namespaces; it is not a virtual
machine. Untrusted code shares the host kernel, and runs as your user (with
your user's limits) inside the sandbox. For hostile malware, prefer
`network = "none"` (or a minimal `allowlist`), `home = "ephemeral"`, no read-write binds, and run the
whole thing in a dedicated user account or VM. Never bind directories with
secrets (SSH keys, cloud credentials) into a sandbox.

## Building and publishing the package

The repository is a Pixi workspace that also defines the rebox conda package
(`[package]` in `pixi.toml`, built with the `pixi-build-python` backend from
`pyproject.toml`). The package is `noarch: python`: one build serves every
Linux architecture.

```
pixi run build-package                       # -> dist/rebox-<version>-<build>.conda
pixi publish --to file:///srv/conda-channel  # build + add to a local channel (indexed)
pixi publish --to https://prefix.dev/<channel>
pixi publish --to https://anaconda.org/<owner>/<label>
```

Remote channels need credentials (`pixi auth login ...`). A local or shared
directory channel is enough for a team or an air-gapped site:
`pixi global install -c file:///srv/conda-channel -c conda-forge rebox`.

To release a version, bump `version` in both `pyproject.toml` and the
`[package]` table of `pixi.toml`. The recipes shipped with rebox are package
data (`src/rebox/data/recipes`): add new versions there, never modify a
published one.

## Development

```
pixi run rebox --help        # run rebox from the sources
pixi run -e test test        # run the tests
```

Sources: `src/rebox/` (CLI, configuration, recipe resolution, image build,
allowlist proxy), `src/rebox/data/` (built-in recipes, `defaults.toml`
template, example environments), `tests/`.
