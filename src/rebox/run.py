"""Building and running the Apptainer command line for a sandbox."""

from __future__ import annotations

import functools
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import SANDBOX_HOME, WORKSPACE_TARGET, ConfigError, SandboxEnv
from .proxy import AllowlistProxy, Upstream

PROXY_PORT = 3128
PROXY_DIR = "/run/rebox-net"
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"

# Runs inside the sandbox before the user command when network = "allowlist":
# exposes the host proxy's Unix socket as 127.0.0.1:3128, then execs the command.
RELAY_SCRIPT = f"""\
if ! command -v socat >/dev/null 2>&1; then
    echo "sandbox: socat is missing from the image; rebuild it (base recipe) to use network = allowlist" >&2
    exit 125
fi
socat TCP-LISTEN:{PROXY_PORT},bind=127.0.0.1,reuseaddr,fork UNIX-CONNECT:{PROXY_DIR}/proxy.sock 2>/dev/null &
i=0
until socat -u /dev/null TCP:127.0.0.1:{PROXY_PORT} 2>/dev/null; do
    i=$((i + 1)); [ "$i" -gt 100 ] && break; sleep 0.05
done
exec "$@"
"""


def apptainer_bin() -> str:
    path = shutil.which("apptainer")
    if not path:
        raise ConfigError("apptainer not found on PATH. Run the tool through `rebox ...`.")
    return path


@functools.cache
def user_systemd_available() -> bool:
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def limits_backend(env: SandboxEnv) -> str:
    res = env.resources
    if not (res.cpus or res.memory_mb or res.pids):
        return "none"
    if res.limits_backend != "auto":
        return res.limits_backend
    # Rootless cgroup limits (systemd-run or apptainer --cpus/--memory) both need
    # a systemd user session with cgroups v2 delegation.
    return "systemd-run" if user_systemd_available() else "none"


def limits_requested(env: SandboxEnv) -> bool:
    res = env.resources
    return bool(res.cpus or res.memory_mb or res.pids)


@dataclass
class Command:
    argv: list[str]
    temp_dirs: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    proxy: AllowlistProxy | None = None


def build_command(
    env: SandboxEnv,
    inner: list[str],
    *,
    network_override: str | None = None,
    limits: bool = True,
    extra_binds: list[str] | None = None,
) -> Command:
    res = env.resources
    cmd = Command(argv=[])
    if not env.image.is_file():
        cmd.notes.append(f"Image {env.image} does not exist yet: run `rebox build {env.name}` first")

    backend = limits_backend(env) if limits else "none"
    if limits and backend == "none" and limits_requested(env) and env.resources.limits_backend == "auto":
        cmd.notes.append(
            "CPU/memory/PID limits are NOT enforced: no systemd user session with cgroups v2 "
            "was found (set limits_backend explicitly to fail instead)"
        )
    if backend == "systemd-run":
        cmd.argv += ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
        if res.memory_mb:
            cmd.argv += ["-p", f"MemoryMax={res.memory_mb}M", "-p", "MemorySwapMax=0"]
        if res.cpus:
            cmd.argv += ["-p", f"CPUQuota={int(res.cpus * 100)}%"]
        if res.pids:
            cmd.argv += ["-p", f"TasksMax={res.pids}"]
        cmd.argv += ["--"]

    cmd.argv += [apptainer_bin(), "--quiet", "exec", "--containall", "--cleanenv", "--no-eval"]

    # Home directory
    if res.home == "persistent":
        home = env.home_dir
        home.mkdir(parents=True, exist_ok=True)
    else:
        home = Path(tempfile.mkdtemp(prefix=f"sandbox-{env.name}-home-"))
        cmd.temp_dirs.append(home)
    cmd.argv += ["--home", f"{home}:{SANDBOX_HOME}"]

    # Workspace (inner Pixi project) and user binds
    env.workspace.mkdir(parents=True, exist_ok=True)
    cmd.argv += ["--bind", f"{env.workspace}:{WORKSPACE_TARGET}:rw", "--pwd", WORKSPACE_TARGET]
    for bind in res.binds:
        cmd.argv += ["--bind", bind.spec()]
    for spec in extra_binds or []:
        cmd.argv += ["--bind", spec]

    if res.writable_tmpfs:
        cmd.argv.append("--writable-tmpfs")
    network = network_override or res.network
    if network in ("none", "allowlist"):
        cmd.argv += ["--net", "--network", "none"]
    if network == "allowlist":
        # Unix socket paths are limited to ~100 characters: keep it short, in /tmp.
        sock_dir = Path(tempfile.mkdtemp(prefix="sbx-net-"))
        cmd.temp_dirs.append(sock_dir)
        cmd.proxy = AllowlistProxy(
            res.allow, sock_dir / "proxy.sock", log_path=env.network_log, upstream=Upstream.from_env()
        )
        cmd.argv += ["--bind", f"{sock_dir}:{PROXY_DIR}:rw"]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            cmd.argv += ["--env", f"{key}={PROXY_URL}"]
        cmd.argv += ["--env", "NO_PROXY=localhost,127.0.0.1,::1", "--env", "no_proxy=localhost,127.0.0.1,::1",
                     "--env", "NODE_USE_ENV_PROXY=1"]
    if res.hostname:
        cmd.argv += ["--hostname", res.hostname]
    if res.gpu:
        cmd.argv.append("--nv")

    if backend == "apptainer":
        if res.cpus:
            cmd.argv += ["--cpus", str(res.cpus)]
        if res.memory_mb:
            cmd.argv += ["--memory", f"{res.memory_mb}M", "--memory-swap", f"{res.memory_mb}M"]
        if res.pids:
            cmd.argv += ["--pids-limit", str(res.pids)]

    for key, value in res.env.items():
        if network == "allowlist" and key.lower() in PROXY_VARS:
            continue  # the allowlist proxy settings above take precedence
        cmd.argv += ["--env", f"{key}={value}"]
    cmd.argv += res.apptainer_args
    cmd.argv.append(str(env.image))
    if network == "allowlist":
        cmd.argv += ["/bin/sh", "-c", RELAY_SCRIPT, "sandbox-relay"]
    cmd.argv += inner
    return cmd


def shell_argv(env: SandboxEnv) -> list[str]:
    if env.has_recipe("pixi") and (env.workspace / "pixi.toml").is_file():
        return ["pixi", "shell", "--manifest-path", f"{WORKSPACE_TARGET}/pixi.toml"]
    return ["/bin/bash", "-l"]


def run_command(env: SandboxEnv, cmd: Command, *, dry_run: bool = False, keep_proxy: bool = False) -> int:
    for warning in env.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for note in cmd.notes:
        print(f"note: {note}", file=sys.stderr)
    if dry_run:
        print(shlex.join(cmd.argv))
        if cmd.proxy:
            rules = "; ".join(f"{r.name}: {', '.join(r.domains)} (ports {', '.join(map(str, r.ports))})"
                              for r in cmd.proxy.rules) or "nothing"
            print(f"# allowlist proxy would allow: {rules}")
        _cleanup(cmd)
        return 0
    try:
        if cmd.proxy:
            cmd.proxy.start()
        return subprocess.run(cmd.argv, env=_host_env(keep_proxy)).returncode
    finally:
        if cmd.proxy:
            cmd.proxy.stop()
            print(cmd.proxy.summary(), file=sys.stderr)
        _cleanup(cmd)


PROXY_VARS = {"http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy"}


def _host_env(keep_proxy: bool = False) -> dict[str, str]:
    """Environment of the apptainer process.

    Apptainer forwards APPTAINERENV_* variables and proxy variables even with
    --cleanenv, so they are removed here. Proxies are kept only for build-time
    steps; pass them to a sandbox explicitly through [resources.env].
    """
    env = {}
    for key, value in os.environ.items():
        if key.startswith(("APPTAINERENV_", "SINGULARITYENV_", "APPTAINER_BIND", "SINGULARITY_BIND")):
            continue
        if key.lower() in PROXY_VARS and not keep_proxy:
            continue
        env[key] = value
    return env


def _cleanup(cmd: Command) -> None:
    for path in cmd.temp_dirs:
        shutil.rmtree(path, ignore_errors=True)
