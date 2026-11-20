"""Loading and resolving sandbox configuration.

A sandbox environment is resolved from three sources:

1. ``rebox-defaults.toml`` at the project root (unless ``inherit_defaults = false``),
2. ``envs/<name>/rebox.toml``,
3. the recipes in ``recipes/`` it references (plus their ``requires``).

Tables are deep-merged, scalar values and lists from the environment replace
the defaults. Named binds can be disabled with ``enabled = false``.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SANDBOX_HOME = "/home/sandbox"
WORKSPACE_TARGET = "/rebox"


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


# --------------------------------------------------------------------------- #
# Project layout
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Project:
    root: Path

    @property
    def defaults_file(self) -> Path:
        return self.root / "rebox-defaults.toml"

    @property
    def recipes_dir(self) -> Path:
        return self.root / "recipes"

    @property
    def envs_dir(self) -> Path:
        return self.root / "envs"

    def env_dir(self, name: str) -> Path:
        return self.envs_dir / name

    @classmethod
    def discover(cls, start: Path | None = None) -> "Project":
        """Find the project root (directory holding ``rebox-defaults.toml``)."""
        for var in ("REBOX_ROOT", "PIXI_PROJECT_ROOT"):
            value = os.environ.get(var)
            if value and (Path(value) / "rebox-defaults.toml").is_file():
                return cls(Path(value).resolve())
        here = (start or Path.cwd()).resolve()
        for candidate in (here, *here.parents):
            if (candidate / "rebox-defaults.toml").is_file():
                return cls(candidate)
        for candidate in (here, *here.parents):
            if (candidate / "defaults.toml").is_file() and (candidate / "envs").is_dir():
                raise ConfigError(
                    f"{candidate} looks like a project made by an older rebox: rename "
                    "defaults.toml to rebox-defaults.toml, envs/*/sandbox.toml to rebox.toml "
                    "and envs/*/sandbox.lock.toml to rebox.lock.toml"
                )
        raise ConfigError(
            "Could not find a rebox project (no rebox-defaults.toml in the current "
            "directory or its parents). Create one with `rebox init`, or set "
            "REBOX_ROOT to point to it."
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"File not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_host_vars(value: str, environ: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Expand ``${VAR}`` and ``${VAR:-default}`` from the host environment.

    Returns the expanded string and the list of referenced variables that were
    unset (and had no default).
    """
    env = os.environ if environ is None else environ
    missing: list[str] = []

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        missing.append(name)
        return ""

    return _VAR_RE.sub(repl, value), missing


def expand_path(value: str, base: Path) -> Path:
    # Variables first, so that defaults like ${XAUTHORITY:-~/.Xauthority} work.
    expanded, _ = expand_host_vars(value)
    path = Path(os.path.expanduser(expanded))
    return path if path.is_absolute() else (base / path)


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)


def parse_size_mb(value: str | int | None) -> int | None:
    """Parse '4G', '512M', '2048' (MB) into megabytes. Empty/0 -> None."""
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, int):
        return value
    match = _SIZE_RE.match(str(value))
    if not match:
        raise ConfigError(f"Invalid size: {value!r} (use e.g. '512M', '4G')")
    number, unit = float(match.group(1)), match.group(2).upper()
    factor = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[unit]
    return max(1, int(number * factor))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Resolved environment
# --------------------------------------------------------------------------- #


@dataclass
class Bind:
    name: str
    source: Path
    target: str
    mode: str = "ro"
    optional: bool = False

    def spec(self) -> str:
        return f"{self.source}:{self.target}:{self.mode}"


@dataclass
class AllowRule:
    """A named group of allowed destinations for ``network = "allowlist"``."""

    name: str
    domains: list[str]
    ports: list[int] = field(default_factory=lambda: [80, 443])
    allow_private: bool = False

    def matches(self, host: str, port: int) -> bool:
        host = normalize_host(host)
        return port in self.ports and any(domain_matches(d, host) for d in self.domains)


def normalize_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def domain_matches(pattern: str, host: str) -> bool:
    """'example.com' matches exactly; '*.example.com' matches any subdomain (not the apex)."""
    pattern = normalize_host(pattern)
    host = normalize_host(host)
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and len(host) > len(pattern) - 1
    return host == pattern


_DOMAIN_RE = re.compile(r"^(\*\.)?[a-z0-9_]([a-z0-9_.:-]*[a-z0-9_])?$")


@dataclass
class Resources:
    network: str = "none"
    cpus: float | None = None
    memory_mb: int | None = None
    pids: int | None = None
    limits_backend: str = "auto"
    home: str = "persistent"
    writable_tmpfs: bool = True
    gpu: bool | str = False  # False, True (auto-detect), "nvidia", "amd" or "dri"
    display: bool = False
    nested: bool = False  # expose /dev/fuse so that rebox/apptainer can run inside
    hostname: str | None = None
    binds: list[Bind] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    apptainer_args: list[str] = field(default_factory=list)
    allow: list[AllowRule] = field(default_factory=list)


@dataclass
class SandboxEnv:
    project: Project
    name: str
    description: str
    base_image: str
    requested: list[str]
    recipes: list[Any]  # list[recipes.Recipe], in build order; empty if not resolved
    recipe_args: dict[str, dict[str, str]]
    resources: Resources
    raw: dict[str, Any]
    sources: dict[str, Any] = field(default_factory=dict)  # name -> recipes.Source
    changes: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # Paths ----------------------------------------------------------------- #
    @property
    def dir(self) -> Path:
        return self.project.env_dir(self.name)

    @property
    def workspace(self) -> Path:
        return self.dir / "workspace"

    @property
    def state_dir(self) -> Path:
        return self.dir / ".state"

    @property
    def image(self) -> Path:
        return self.state_dir / "image.sif"

    @property
    def def_file(self) -> Path:
        return self.state_dir / "build.def"

    @property
    def home_dir(self) -> Path:
        return self.state_dir / "home"

    @property
    def network_log(self) -> Path:
        return self.state_dir / "network.log"

    @property
    def lock_file(self) -> Path:
        return self.dir / "rebox.lock.toml"

    def has_recipe(self, name: str) -> bool:
        if self.recipes:
            return any(r.name == name for r in self.recipes)
        from .recipes import locked_names
        return name in locked_names(self.lock_file)


VALID_NETWORK = {"none", "host", "allowlist"}
VALID_BACKENDS = {"auto", "apptainer", "systemd-run", "none"}
VALID_HOME = {"persistent", "ephemeral"}
VALID_MODES = {"ro", "rw"}
ENV_KEYS = {"name", "description", "inherit_defaults", "build", "resources", "sources"}
BUILD_KEYS = {"base_image", "default_recipes", "recipes", "recipe_args"}
RESOURCE_KEYS = {
    "network", "cpus", "memory", "pids", "limits_backend", "home", "writable_tmpfs",
    "gpu", "display", "nested", "hostname", "binds", "env", "apptainer_args", "allow",
}


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}")


def merged_config(project: Project, name: str) -> dict[str, Any]:
    env_file = project.env_dir(name) / "rebox.toml"
    if not env_file.is_file():
        if (project.env_dir(name) / "sandbox.toml").is_file():
            raise ConfigError(f"Rename {project.env_dir(name)}/sandbox.toml to rebox.toml (and "
                              "sandbox.lock.toml to rebox.lock.toml): file names changed in rebox 0.4")
        raise ConfigError(f"Environment '{name}' not found ({env_file} is missing)")
    env_data = load_toml(env_file)
    _check_keys(env_data, ENV_KEYS, str(env_file))
    if env_data.get("inherit_defaults", True):
        defaults = load_toml(project.defaults_file) if project.defaults_file.is_file() else {}
        _check_keys(defaults, ENV_KEYS - {"name", "description", "inherit_defaults"}, str(project.defaults_file))
    else:
        defaults = {}
    return deep_merge(defaults, env_data)


def _parse_resources(data: dict[str, Any], env_dir: Path, warnings: list[str]) -> Resources:
    _check_keys(data, RESOURCE_KEYS, "[resources]")
    res = Resources()
    res.network = str(data.get("network", res.network))
    if res.network not in VALID_NETWORK:
        raise ConfigError(f"resources.network must be one of {sorted(VALID_NETWORK)}")
    cpus = data.get("cpus")
    res.cpus = float(cpus) if cpus not in (None, 0, "") else None
    res.memory_mb = parse_size_mb(data.get("memory"))
    pids = data.get("pids")
    res.pids = int(pids) if pids else None
    res.limits_backend = str(data.get("limits_backend", res.limits_backend))
    if res.limits_backend not in VALID_BACKENDS:
        raise ConfigError(f"resources.limits_backend must be one of {sorted(VALID_BACKENDS)}")
    res.home = str(data.get("home", res.home))
    if res.home not in VALID_HOME:
        raise ConfigError(f"resources.home must be one of {sorted(VALID_HOME)}")
    res.writable_tmpfs = bool(data.get("writable_tmpfs", res.writable_tmpfs))
    gpu = data.get("gpu", res.gpu)
    if not isinstance(gpu, bool) and gpu not in ("nvidia", "amd", "dri"):
        raise ConfigError('resources.gpu must be true (auto-detect), false, "nvidia", "amd" or "dri"')
    res.gpu = gpu
    display = data.get("display", res.display)
    if not isinstance(display, bool):
        raise ConfigError("resources.display must be true or false")
    res.display = display
    nested = data.get("nested", res.nested)
    if not isinstance(nested, bool):
        raise ConfigError("resources.nested must be true or false")
    res.nested = nested
    res.hostname = data.get("hostname")
    res.apptainer_args = [str(a) for a in data.get("apptainer_args", [])]

    for bind_name, spec in data.get("binds", {}).items():
        if not isinstance(spec, dict):
            raise ConfigError(f"resources.binds.{bind_name} must be a table")
        _check_keys(spec, {"source", "target", "mode", "optional", "enabled"}, f"resources.binds.{bind_name}")
        if not spec.get("enabled", True):
            continue
        if "source" not in spec or "target" not in spec:
            raise ConfigError(f"resources.binds.{bind_name}: 'source' and 'target' are required")
        mode = spec.get("mode", "ro")
        if mode not in VALID_MODES:
            raise ConfigError(f"resources.binds.{bind_name}.mode must be 'ro' or 'rw'")
        bind = Bind(
            name=bind_name,
            source=expand_path(spec["source"], env_dir),
            target=spec["target"],
            mode=mode,
            optional=bool(spec.get("optional", False)),
        )
        if not bind.source.exists():
            if bind.optional:
                warnings.append(f"Skipping optional bind '{bind_name}': {bind.source} does not exist")
                continue
            raise ConfigError(f"Bind '{bind_name}': source {bind.source} does not exist")
        res.binds.append(bind)

    for group, spec in data.get("allow", {}).items():
        if not isinstance(spec, dict):
            raise ConfigError(f"resources.allow.{group} must be a table")
        _check_keys(spec, {"domains", "ports", "allow_private", "enabled"}, f"resources.allow.{group}")
        if not spec.get("enabled", True):
            continue
        domains = [normalize_host(str(d)) for d in spec.get("domains", [])]
        for domain in domains:
            if not _DOMAIN_RE.match(domain):
                raise ConfigError(f"resources.allow.{group}: invalid domain pattern '{domain}'")
        try:
            ports = [int(p) for p in spec.get("ports", [80, 443])]
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"resources.allow.{group}.ports must be a list of integers") from exc
        res.allow.append(AllowRule(group, domains, ports, bool(spec.get("allow_private", False))))
    if res.network == "allowlist" and not res.allow:
        warnings.append("network = 'allowlist' but no [resources.allow.*] group is enabled: "
                        "every connection will be refused")

    for key, value in data.get("env", {}).items():
        expanded, missing = expand_host_vars(str(value))
        if missing:
            warnings.append(f"Env var {key} not passed: host variable(s) {', '.join(missing)} unset")
            continue
        res.env[key] = expanded
    return res


def load_env(
    project: Project,
    name: str,
    *,
    check_binds: bool = True,
    resolve_recipes: bool = True,
    refresh: set[str] | None = None,
    refresh_all: bool = False,
    fetch: bool = False,
) -> SandboxEnv:
    """Load and resolve an environment.

    Recipe versions come from the lock file when it is still valid; ``refresh``
    / ``refresh_all`` pick the newest allowed versions instead (``fetch``
    updates git sources first). With ``resolve_recipes=False`` recipes are not
    loaded at all (enough to run an already built image, and works offline).
    """
    data = merged_config(project, name)
    warnings: list[str] = []
    build = data.get("build", {})
    _check_keys(build, BUILD_KEYS, "[build]")
    base_image = build.get("base_image")
    if not base_image:
        raise ConfigError("build.base_image is not set (in rebox-defaults.toml or rebox.toml)")
    if base_image.startswith("docker://") and "@sha256:" not in base_image:
        warnings.append(
            f"base_image '{base_image}' is not pinned by digest; builds may differ over time"
        )

    requested = [str(r) for r in (*build.get("default_recipes", []), *build.get("recipes", []))]
    sources_config = data.get("sources") or {"builtin": {"builtin": True}}

    recipes: list[Any] = []
    sources: dict[str, Any] = {}
    changes: list[tuple[str, str | None, str | None]] = []
    recipe_args: dict[str, dict[str, str]] = {}
    overrides = build.get("recipe_args", {})
    if resolve_recipes:
        from .recipes import load_sources, read_locked, resolve
        sources = load_sources(project, sources_config)
        if fetch:
            for source in sources.values():
                source.fetch()
        locked = read_locked(project.env_dir(name) / "rebox.lock.toml")
        resolution = resolve(requested, sources, locked, refresh=refresh, refresh_all=refresh_all)
        recipes, changes = resolution.recipes, resolution.changes
        for name_ in overrides:
            if not any(r.name == name_ for r in recipes):
                raise ConfigError(f"build.recipe_args.{name_}: recipe is not used by this environment")
        for recipe in recipes:
            args = dict(recipe.args)
            for key, value in overrides.get(recipe.name, {}).items():
                if key not in recipe.args:
                    raise ConfigError(f"Recipe {recipe.name} {recipe.version} has no arg '{key}' "
                                      f"(has: {sorted(recipe.args)})")
                args[key] = str(value)
            recipe_args[recipe.name] = args

    env_dir = project.env_dir(name)
    resources_data = data.get("resources", {})
    if not check_binds:
        resources_data = {**resources_data, "binds": {
            k: {**v, "optional": True} for k, v in resources_data.get("binds", {}).items()
            if isinstance(v, dict)
        }}
    resources = _parse_resources(resources_data, env_dir, warnings)

    return SandboxEnv(
        project=project,
        name=name,
        description=data.get("description", ""),
        base_image=base_image,
        requested=requested,
        recipes=recipes,
        recipe_args=recipe_args,
        resources=resources,
        raw=data,
        sources=sources,
        changes=changes,
        warnings=warnings,
    )


def list_envs(project: Project) -> list[str]:
    if not project.envs_dir.is_dir():
        return []
    return sorted(p.parent.name for p in project.envs_dir.glob("*/rebox.toml"))
