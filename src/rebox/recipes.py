"""Recipe sources, versions, resolution and locking.

Layout of a recipe source (a local directory or a git repository, optionally
in a sub-directory of it)::

    <source root>/
        source.toml                 optional metadata (description)
        <recipe>/
            <version>.toml          one file per published version
            <version>.toml

A recipe is referenced as ``[source:]name[@constraint]``, e.g. ``nodejs``,
``nodejs@1.2``, ``team:nodejs@>=1.2,<2``. Without a source, sources are
searched in the order they are declared (for ``requires``, the source of the
requiring recipe is searched first).

Resolution prefers the versions recorded in the environment's
``rebox.lock.toml``; ``rebox update`` resolves again to the newest versions
allowed by the constraints.
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
import subprocess
import tomllib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from .config import ConfigError, Project, load_toml, sha256_text

BUILTIN_RECIPES = Path(__file__).resolve().parent / "data" / "recipes"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
RECIPE_KEYS = {"description", "requires", "args", "environment", "post", "runscript"}

# --------------------------------------------------------------------------- #
# Versions and constraints
# --------------------------------------------------------------------------- #

_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:[-+.]?([0-9A-Za-z.+-]+))?$")


@functools.total_ordering
@dataclass(frozen=True)
class Version:
    text: str
    release: tuple[int, ...]
    suffix: str = ""  # pre-release / local part, e.g. "rc1": sorts before the release

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _VERSION_RE.match(text.strip())
        if not match:
            raise ConfigError(f"Invalid version '{text}' (expected e.g. 1.0.0, 2.1, 1.0.0-rc1)")
        return cls(text.strip(), tuple(int(p) for p in match.group(1).split(".")), match.group(2) or "")

    def _key(self) -> tuple:
        release = self.release + (0,) * (8 - len(self.release))
        return (release, self.suffix == "", self.suffix)

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return self.text


_CLAUSE_RE = re.compile(r"^(==|!=|>=|<=|>|<|=)?\s*([0-9][0-9A-Za-z.+*-]*)$")


@dataclass(frozen=True)
class Constraint:
    """Comma-separated clauses, all of which must hold.

    ``1.2`` / ``1.2.*``: any version starting with 1.2 (1.2, 1.2.0, 1.2.7...);
    ``==1.2.0``: exactly; ``>=1.2``, ``>1``, ``<2``, ``<=1.5``, ``!=1.3.0``;
    empty, ``*`` or ``latest``: any version.
    """

    text: str = ""

    @functools.cached_property
    def clauses(self) -> list[tuple[str, Version]]:
        text = self.text.strip()
        if text in ("", "*", "latest"):
            return []
        clauses = []
        for part in text.split(","):
            match = _CLAUSE_RE.match(part.strip())
            if not match:
                raise ConfigError(f"Invalid version constraint '{self.text}'")
            op, value = match.group(1) or "prefix", match.group(2)
            if value.endswith(".*"):
                if op not in ("prefix", "=", "=="):
                    raise ConfigError(f"Wildcards only work without operator: '{self.text}'")
                op, value = "prefix", value[:-2]
            if op == "=":
                op = "prefix"
            clauses.append((op, Version.parse(value)))
        return clauses

    def allows(self, version: Version) -> bool:
        for op, ref in self.clauses:
            if op == "prefix":
                ok = (version.release[: len(ref.release)] == ref.release
                      and (not ref.suffix or version.suffix == ref.suffix))
            elif op == "==":
                ok = version == ref
            elif op == "!=":
                ok = version != ref
            elif op == ">=":
                ok = version >= ref
            elif op == "<=":
                ok = version <= ref
            elif op == ">":
                ok = version > ref
            else:  # "<"
                ok = version < ref
            if not ok:
                return False
        return True

    def __str__(self) -> str:
        return self.text or "*"


@dataclass(frozen=True)
class RecipeRef:
    source: str | None
    name: str
    constraint: Constraint

    @classmethod
    def parse(cls, text: str) -> "RecipeRef":
        spec, _, constraint = text.strip().partition("@")
        source, _, name = spec.rpartition(":")
        name = name.strip()
        if not NAME_RE.match(name) or (source and not NAME_RE.match(source)):
            raise ConfigError(f"Invalid recipe reference '{text}' (expected [source:]name[@constraint])")
        ref = cls(source or None, name, Constraint(constraint.strip()))
        ref.constraint.clauses  # validate early
        return ref

    def __str__(self) -> str:
        text = f"{self.source}:{self.name}" if self.source else self.name
        return f"{text}@{self.constraint}" if self.constraint.text else text


# --------------------------------------------------------------------------- #
# Recipes
# --------------------------------------------------------------------------- #


@dataclass
class Recipe:
    """One version of a recipe, as read from its source."""

    source: str
    name: str
    version: Version
    text: str
    commit: str | None = None  # git sources only
    location: str = ""  # path inside the source
    description: str = ""
    requires: list[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    post: str = ""
    runscript: str | None = None

    @property
    def digest(self) -> str:
        return sha256_text(self.text)

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.name}@{self.version}"

    @classmethod
    def from_text(cls, source: str, name: str, version: Version, text: str,
                  commit: str | None = None, location: str = "") -> "Recipe":
        where = f"recipe {source}:{name} {version}"
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Invalid TOML in {where}: {exc}") from exc
        unknown = set(data) - RECIPE_KEYS
        if unknown:
            raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
        if "post" not in data:
            raise ConfigError(f"{where}: missing 'post' script")
        requires = [str(r) for r in data.get("requires", [])]
        for req in requires:
            RecipeRef.parse(req)
        return cls(
            source=source, name=name, version=version, text=text, commit=commit, location=location,
            description=data.get("description", ""),
            requires=requires,
            args={k: str(v) for k, v in data.get("args", {}).items()},
            environment={k: str(v) for k, v in data.get("environment", {}).items()},
            post=data["post"],
            runscript=data.get("runscript"),
        )


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def _index(paths: Iterable[str], prefix: str) -> dict[str, dict[Version, str]]:
    """Map recipe name -> {version: path} from '<prefix><name>/<version>.toml' paths."""
    index: dict[str, dict[Version, str]] = {}
    for path in paths:
        if not path.startswith(prefix) or not path.endswith(".toml"):
            continue
        parts = path[len(prefix):].split("/")
        if len(parts) != 2 or not NAME_RE.match(parts[0]):
            continue
        try:
            version = Version.parse(parts[1][: -len(".toml")])
        except ConfigError:
            continue
        index.setdefault(parts[0], {})[version] = path
    return index


class Source:
    kind = "abstract"

    def __init__(self, name: str) -> None:
        self.name = name

    def index(self, commit: str | None = None) -> dict[str, dict[Version, str]]:
        raise NotImplementedError

    def read_text(self, path: str, commit: str | None = None) -> str:
        raise NotImplementedError

    def current_commit(self) -> str | None:
        return None

    def fetch(self) -> None:
        """Refresh remote content (git sources)."""

    def describe(self) -> str:
        raise NotImplementedError

    def lock_info(self) -> dict[str, str]:
        raise NotImplementedError

    # Shared helpers ------------------------------------------------------- #
    def versions(self, name: str, commit: str | None = None) -> list[Version]:
        return sorted(self.index(commit).get(name, {}), reverse=True)

    def recipe_names(self) -> list[str]:
        return sorted(self.index())

    def load(self, name: str, version: Version, commit: str | None = None) -> Recipe:
        commit = commit or self.current_commit()
        path = self.index(commit).get(name, {}).get(version)
        if path is None:
            where = f" at commit {commit[:12]}" if commit else ""
            raise ConfigError(f"Recipe {self.name}:{name} {version} not found{where}")
        return Recipe.from_text(self.name, name, version, self.read_text(path, commit), commit, path)


class LocalSource(Source):
    kind = "local"

    def __init__(self, name: str, root: Path, display_path: str) -> None:
        super().__init__(name)
        self.root = root
        self.display_path = display_path

    def index(self, commit: str | None = None) -> dict[str, dict[Version, str]]:
        if not self.root.is_dir():
            raise ConfigError(f"Recipe source '{self.name}': directory {self.root} does not exist")
        paths = [p.relative_to(self.root).as_posix() for p in self.root.glob("*/*.toml")]
        return _index(paths, "")

    def read_text(self, path: str, commit: str | None = None) -> str:
        return (self.root / path).read_text()

    def describe(self) -> str:
        return f"local  {self.root}"

    def lock_info(self) -> dict[str, str]:
        return {"source_type": "local", "source_path": self.display_path}


class BuiltinSource(LocalSource):
    """The recipes shipped inside the rebox package."""

    kind = "builtin"

    def __init__(self, name: str) -> None:
        super().__init__(name, BUILTIN_RECIPES, "")

    def describe(self) -> str:
        return f"builtin (rebox {__version__})"

    def lock_info(self) -> dict[str, str]:
        return {"source_type": "builtin", "rebox_version": __version__}


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ConfigError("git is not installed (it is a dependency of the host Pixi environment)") from exc
    if check and result.returncode != 0:
        raise ConfigError(f"git {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


class GitSource(Source):
    """Recipes stored in a git repository, read from a local bare mirror."""

    kind = "git"

    def __init__(self, name: str, url: str, ref: str, subdir: str, cache_dir: Path) -> None:
        super().__init__(name)
        self.url = url
        self.ref = ref
        self.subdir = subdir.strip("/")
        digest = hashlib.sha256(url.encode()).hexdigest()[:10]
        self.mirror = cache_dir / f"{name}-{digest}.git"
        self._indexes: dict[str, dict[str, dict[Version, str]]] = {}
        self._head: str | None = None

    def _ensure(self) -> None:
        if not (self.mirror / "HEAD").is_file():
            self.mirror.parent.mkdir(parents=True, exist_ok=True)
            print(f"Cloning recipe source '{self.name}' from {self.url}...", flush=True)
            _git("clone", "--mirror", "--quiet", self.url, str(self.mirror))

    def fetch(self) -> None:
        self._ensure()
        print(f"Fetching recipe source '{self.name}'...", flush=True)
        _git("remote", "update", "--prune", cwd=self.mirror)
        self._head = None
        self._indexes.clear()

    def current_commit(self) -> str:
        if self._head is None:
            self._ensure()
            result = _git("rev-parse", "--verify", "--quiet", f"{self.ref}^{{commit}}", cwd=self.mirror, check=False)
            if result.returncode != 0:
                raise ConfigError(f"Recipe source '{self.name}': ref '{self.ref}' not found in {self.url}")
            self._head = result.stdout.strip()
        return self._head

    def _has_commit(self, commit: str) -> bool:
        return _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=self.mirror, check=False).returncode == 0

    def _commit(self, commit: str | None) -> str:
        if commit is None:
            return self.current_commit()
        self._ensure()
        if not self._has_commit(commit):
            self.fetch()
            if not self._has_commit(commit):
                raise ConfigError(f"Recipe source '{self.name}': commit {commit} not found in {self.url}")
        return commit

    def index(self, commit: str | None = None) -> dict[str, dict[Version, str]]:
        commit = self._commit(commit)
        if commit not in self._indexes:
            prefix = f"{self.subdir}/" if self.subdir else ""
            listing = _git("ls-tree", "-r", "--name-only", commit, "--", prefix or ".", cwd=self.mirror)
            self._indexes[commit] = _index(listing.stdout.splitlines(), prefix)
        return self._indexes[commit]

    def read_text(self, path: str, commit: str | None = None) -> str:
        return _git("show", f"{self._commit(commit)}:{path}", cwd=self.mirror).stdout

    def describe(self) -> str:
        sub = f" ({self.subdir}/)" if self.subdir else ""
        return f"git    {self.url} @ {self.ref}{sub}"

    def lock_info(self) -> dict[str, str]:
        info = {"source_type": "git", "source_url": self.url, "source_ref": self.ref}
        if self.subdir:
            info["source_subdir"] = self.subdir
        return info


SOURCE_KEYS = {"path", "git", "builtin", "ref", "subdir", "enabled", "description"}


def load_sources(project: Project, config: dict[str, Any]) -> dict[str, Source]:
    """Build the ordered source table from a merged ``[sources]`` config."""
    sources: dict[str, Source] = {}
    for name, spec in config.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"sources.{name} must be a table")
        unknown = set(spec) - SOURCE_KEYS
        if unknown:
            raise ConfigError(f"sources.{name}: unknown keys {sorted(unknown)}; allowed: {sorted(SOURCE_KEYS)}")
        if not spec.get("enabled", True):
            continue
        if not NAME_RE.match(name):
            raise ConfigError(f"Invalid source name '{name}'")
        kinds = [k for k in ("path", "git", "builtin") if spec.get(k)]
        if len(kinds) != 1:
            raise ConfigError(f"sources.{name}: set exactly one of 'path', 'git' or 'builtin = true'")
        if kinds == ["builtin"]:
            sources[name] = BuiltinSource(name)
        elif "path" in spec:
            from .config import expand_path
            sources[name] = LocalSource(name, expand_path(spec["path"], project.root), spec["path"])
        else:
            sources[name] = GitSource(name, spec["git"], spec.get("ref", "main"), spec.get("subdir", ""),
                                      project.root / ".sources")
    if not sources:
        raise ConfigError("No recipe source is configured (add a [sources.<name>] table to rebox-defaults.toml)")
    return sources


# --------------------------------------------------------------------------- #
# Lock file
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Locked:
    source: str
    name: str
    version: Version
    sha256: str
    commit: str | None = None


def read_locked(lock_path: Path) -> dict[tuple[str, str], Locked]:
    if not lock_path.is_file():
        return {}
    data = load_toml(lock_path)
    locked = {}
    for entry in data.get("recipes", []):
        if not {"source", "name", "version", "sha256"} <= set(entry):
            continue  # lock written by an older version of the tool
        item = Locked(entry["source"], entry["name"], Version.parse(entry["version"]),
                      entry["sha256"], entry.get("commit"))
        locked[(item.source, item.name)] = item
    return locked


def locked_names(lock_path: Path) -> list[str]:
    return [name for _, name in read_locked(lock_path)]


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


@dataclass
class Resolution:
    recipes: list[Recipe]  # in build order (dependencies first)
    from_lock: set[tuple[str, str]]  # recipes whose locked version was kept
    changes: list[tuple[str, str | None, str | None]]  # (name, old version, new version)


def resolve(
    requested: list[str],
    sources: dict[str, Source],
    locked: dict[tuple[str, str], Locked] | None = None,
    refresh: set[str] | None = None,
    refresh_all: bool = False,
) -> Resolution:
    """Choose one version of every requested recipe and of its dependencies.

    Locked versions are kept when they still satisfy every constraint, unless
    the recipe is in ``refresh`` (or ``refresh_all``): then the newest allowed
    version is taken.
    """
    locked = locked or {}
    refresh = refresh or set()
    refs = [RecipeRef.parse(r) for r in requested]
    order = list(sources)

    locked_source = {name: source for (source, name) in locked}

    def locate(ref: RecipeRef, requester: str | None) -> tuple[str, str]:
        if not ref.source and not refresh_all and ref.name not in refresh:
            # Keep the source a recipe was locked from, even if another source
            # declared earlier now has a recipe with the same name.
            source = locked_source.get(ref.name)
            if source in sources and ref.name in sources[source].index(locked[(source, ref.name)].commit):
                return source, ref.name
        if ref.source:
            if ref.source not in sources:
                raise ConfigError(f"Recipe '{ref}': unknown source '{ref.source}' "
                                  f"(configured: {', '.join(order)})")
            if ref.name not in sources[ref.source].index():
                raise ConfigError(f"Recipe '{ref.name}' not found in source '{ref.source}'")
            return ref.source, ref.name
        search = ([requester] if requester else []) + [s for s in order if s != requester]
        for source in search:
            if ref.name in sources[source].index():
                return source, ref.name
        raise ConfigError(f"Unknown recipe '{ref.name}' (searched sources: {', '.join(search)})")

    def choose(key: tuple[str, str], constraints: list[tuple[Constraint, str]]) -> tuple[Recipe, bool]:
        source = sources[key[0]]
        lock = locked.get(key)
        keep = lock is not None and not refresh_all and key[1] not in refresh
        if keep and all(c.allows(lock.version) for c, _ in constraints):
            try:
                recipe = source.load(key[1], lock.version, lock.commit)
            except ConfigError:
                recipe = None  # locked version disappeared: resolve again
            if recipe is not None:
                if recipe.digest != lock.sha256:
                    raise ConfigError(
                        f"Recipe {key[0]}:{key[1]} {lock.version} changed since it was locked "
                        f"(sha256 {lock.sha256[:12]} -> {recipe.digest[:12]}). Published versions "
                        f"must not be modified: add a new version, or accept the change with "
                        f"`rebox update <env> {key[1]}`."
                    )
                return recipe, True
        allowed = [v for v in source.versions(key[1]) if all(c.allows(v) for c, _ in constraints)]
        if not allowed:
            wanted = "; ".join(f"{c} (from {origin})" for c, origin in constraints)
            available = ", ".join(map(str, source.versions(key[1]))) or "none"
            raise ConfigError(f"No version of {key[0]}:{key[1]} satisfies {wanted}. Available: {available}")
        return source.load(key[1], allowed[0]), False

    carried: dict[tuple[str, str], list[tuple[Constraint, str]]] = {}
    for _ in range(25):
        constraints: dict[tuple[str, str], list[tuple[Constraint, str]]] = {}
        chosen: dict[tuple[str, str], Recipe] = {}
        kept: set[tuple[str, str]] = set()
        deps: dict[tuple[str, str], list[tuple[str, str]]] = {}
        by_name: dict[str, tuple[str, str]] = {}
        roots: list[tuple[str, str]] = []
        queue = deque((ref, None, "environment", None) for ref in refs)
        while queue:
            ref, requester, origin, parent = queue.popleft()
            key = locate(ref, requester)
            if by_name.setdefault(key[1], key) != key:
                raise ConfigError(f"Recipe '{key[1]}' is requested from two sources: "
                                  f"{by_name[key[1]][0]} and {key[0]}")
            constraints.setdefault(key, []).append((ref.constraint, origin))
            if parent is None:
                roots.append(key)
            else:
                deps.setdefault(parent, []).append(key)
            if key in chosen:
                continue
            recipe, from_lock = choose(key, carried.get(key, []) + constraints[key])
            chosen[key] = recipe
            if from_lock:
                kept.add(key)
            for dep in recipe.requires:
                queue.append((RecipeRef.parse(dep), key[0], f"{key[1]} {recipe.version}", key))
        if all(c.allows(chosen[k].version) for k, cs in constraints.items() for c, _ in cs):
            break
        for key, cs in constraints.items():  # retry knowing every constraint
            carried.setdefault(key, [])
            carried[key] += [c for c in cs if c not in carried[key]]
    else:
        raise ConfigError("Could not find recipe versions satisfying every constraint")

    # Build order: depth-first, dependencies first.
    ordered: list[Recipe] = []
    state: dict[tuple[str, str], str] = {}

    def visit(key: tuple[str, str], chain: list[str]) -> None:
        if state.get(key) == "done":
            return
        if state.get(key) == "visiting":
            raise ConfigError("Recipe dependency cycle: " + " -> ".join([*chain, key[1]]))
        state[key] = "visiting"
        for dep in deps.get(key, []):
            visit(dep, [*chain, key[1]])
        state[key] = "done"
        ordered.append(chosen[key])

    for key in roots:
        visit(key, [])

    changes = []
    for key, recipe in chosen.items():
        old = locked.get(key)
        if old is None or old.version != recipe.version or old.sha256 != recipe.digest:
            changes.append((f"{key[0]}:{key[1]}", str(old.version) if old else None, str(recipe.version)))
    for key, old in locked.items():
        if key not in chosen:
            changes.append((f"{key[0]}:{key[1]}", str(old.version), None))
    return Resolution(ordered, kept, changes)


def lock_entries(recipes: list[Recipe], sources: dict[str, Source],
                 recipe_args: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    entries = []
    for recipe in recipes:
        entry: dict[str, Any] = {
            "source": recipe.source,
            "name": recipe.name,
            "version": str(recipe.version),
            "sha256": recipe.digest,
            **sources[recipe.source].lock_info(),
            "path": recipe.location,
        }
        if recipe.commit:
            entry["commit"] = recipe.commit
        entry["args"] = recipe_args.get(recipe.name, {})
        entries.append(entry)
    return entries
