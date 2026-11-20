"""Command line interface: ``rebox <command> ...``."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import __version__
from .build import build_image, clean, prepare_workspace, render_def
from .config import WORKSPACE_TARGET, ConfigError, Project, list_envs, load_env, load_toml
from .run import build_command, limits_backend, run_command, shell_argv

SANDBOX_TEMPLATE = """\
description = "{description}"

[build]
# Recipes installed in the image, after the default recipes of rebox-defaults.toml.
# Syntax: [source:]name[@constraint], e.g. "nodejs", "nodejs@1.2",
# "team:analysis@>=2,<3". Dependencies (requires) are added automatically.
# The chosen versions are recorded in rebox.lock.toml; change them with
# `rebox update {name}`.
recipes = [{recipes}]

# Override recipe arguments:
# [build.recipe_args.nodejs]
# NODE_VERSION = "24.20.0"

[resources]
# Anything set here overrides rebox-defaults.toml. Examples:
# network = "host"        # "none", "host" or "allowlist"
# cpus = 4
# memory = "8G"
#
# [resources.binds.project]
# source = "~/code/my-project"
# target = "/work"
# mode = "rw"
#
# Disable a bind inherited from rebox-defaults.toml:
# [resources.binds.gitconfig]
# enabled = false
#
# [resources.env]
# ANTHROPIC_API_KEY = "${{ANTHROPIC_API_KEY}}"
"""

WORKSPACE_TEMPLATE = """\
[workspace]
name = "{name}"
channels = ["conda-forge"]
platforms = ["linux-64"]

[dependencies]
"""


def cmd_init(args: argparse.Namespace) -> int:
    from .init import init_project
    directory = Path(args.directory).resolve()
    notes = init_project(directory, examples=args.examples, display=args.display, gpu=args.gpu)
    print(f"Created a rebox project in {directory}")
    for note in notes:
        print(f"  {note}")
    print("Next: rebox new NAME [RECIPE...], then rebox build NAME")
    return 0


def _default_sources(project: Project):
    from .recipes import load_sources
    defaults = load_toml(project.defaults_file) if project.defaults_file.is_file() else {}
    return load_sources(project, defaults.get("sources") or {"builtin": {"builtin": True}})


def cmd_sources(project: Project, args: argparse.Namespace) -> int:
    sources = _default_sources(project)
    for source in sources.values():
        if args.fetch:
            source.fetch()
        try:
            count = len(source.recipe_names())
            commit = source.current_commit()
            state = f"{count} recipe(s)" + (f", commit {commit[:12]}" if commit else "")
        except ConfigError as exc:
            state = f"unavailable: {exc}"
        print(f"{source.name:<14} {source.describe()}\n{'':<14} {state}")
    return 0


def cmd_list(project: Project, args: argparse.Namespace) -> int:
    sources = _default_sources(project)
    print("Recipes (newest version first; the first source listing a name wins):")
    for source in sources.values():
        print(f"  [{source.name}] {source.describe()}")
        try:
            names = source.recipe_names()
        except ConfigError as exc:
            print(f"    unavailable: {exc}")
            continue
        for name in names:
            versions = source.versions(name)
            latest = source.load(name, versions[0])
            requires = f"  (requires: {', '.join(latest.requires)})" if latest.requires else ""
            shown = ", ".join(map(str, versions[:4])) + (", ..." if len(versions) > 4 else "")
            print(f"    {name:<16} {shown:<18} {latest.description}{requires}")
    print("\nEnvironments:")
    for name in list_envs(project):
        env = load_env(project, name, check_binds=False, resolve_recipes=False)
        built = "built" if env.image.is_file() else "not built"
        locked = "locked" if env.lock_file.is_file() else "not locked"
        print(f"  {name:<20} [{built}, {locked}] {env.description}")
    return 0


def cmd_new(project: Project, args: argparse.Namespace) -> int:
    from .recipes import resolve
    env_dir = project.env_dir(args.name)
    if (env_dir / "rebox.toml").exists():
        raise ConfigError(f"Environment '{args.name}' already exists")
    resolve(args.recipes, _default_sources(project))  # validate the references
    (env_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (env_dir / "rebox.toml").write_text(SANDBOX_TEMPLATE.format(
        name=args.name,
        description=args.description or f"Sandbox {args.name}",
        recipes=", ".join(f'"{r}"' for r in args.recipes),
    ))
    (env_dir / "workspace" / "pixi.toml").write_text(WORKSPACE_TEMPLATE.format(name=args.name))
    (env_dir / ".gitignore").write_text(".state/\nworkspace/.pixi/\n")
    print(f"Created {env_dir}. Edit rebox.toml, then run: rebox build {args.name}")
    return 0


def _print_changes(changes, header: str) -> None:
    print(header)
    width = max((len(name) for name, _, _ in changes), default=10)
    for name, old, new in changes:
        if old is None:
            print(f"  + {name:<{width}} {new}")
        elif new is None:
            print(f"  - {name:<{width}} {old} (removed)")
        elif old == new:
            print(f"  ~ {name:<{width}} {old} (content changed)")
        else:
            print(f"  ↑ {name:<{width}} {old} -> {new}")


def cmd_show(project: Project, args: argparse.Namespace) -> int:
    env = load_env(project, args.name, check_binds=False)
    res = env.resources
    print(f"Environment: {env.name} — {env.description}")
    print(f"Base image:  {env.base_image}")
    print("Recipes:")
    for recipe in env.recipes:
        where = f"{recipe.source}" + (f" @ {recipe.commit[:12]}" if recipe.commit else "")
        print(f"  {recipe.name:<16} {str(recipe.version):<10} [{where}] {recipe.description}")
        if env.recipe_args[recipe.name]:
            pairs = ", ".join(f"{k}={v}" for k, v in env.recipe_args[recipe.name].items())
            print(f"  {'':<16} args: {pairs}")
    if not env.lock_file.is_file():
        print("  (not locked yet: versions are fixed by the next `build` or `lock`)")
    elif env.changes:
        _print_changes(env.changes, "  Lock is out of date with rebox.toml; the next build applies:")
    print(f"Network:     {res.network}")
    if res.network == "allowlist" or args.definition:
        for rule in res.allow:
            print(f"  allow {rule.name}: {', '.join(rule.domains)} "
                  f"(ports {', '.join(map(str, rule.ports))}{', private IPs ok' if rule.allow_private else ''})")
        if res.network == "allowlist" and not res.allow:
            print("  allow: nothing (every connection is refused)")
    print(f"CPUs:        {res.cpus or 'unlimited'}   Memory: "
          f"{f'{res.memory_mb} MB' if res.memory_mb else 'unlimited'}   "
          f"PIDs: {res.pids or 'unlimited'}   (limits via {limits_backend(env)})")
    from .hostcaps import detect_display, detect_gpus
    gpu = {False: "no", True: "auto"}.get(res.gpu, res.gpu) if isinstance(res.gpu, bool) else res.gpu
    print(f"Home:        {res.home}   Writable tmpfs: {res.writable_tmpfs}")
    found = ", ".join(detect_display()) or "none found"
    print(f"Display:     {'yes' if res.display else 'no'}" + (f" ({found})" if res.display else ""))
    gpus = ", ".join(detect_gpus()) or "none found"
    print(f"GPU:         {gpu}" + (f" (host: {gpus})" if res.gpu is not False else ""))
    print(f"Workspace:   {env.workspace} -> {WORKSPACE_TARGET} (rw)")
    for bind in res.binds:
        print(f"Bind:        {bind.name}: {bind.source} -> {bind.target} ({bind.mode})")
    for key in res.env:
        print(f"Env:         {key}")
    print(f"Image:       {env.image} ({'built' if env.image.is_file() else 'not built'})")
    for warning in env.warnings:
        print(f"warning: {warning}")
    if args.definition:
        print("\n" + render_def(env))
    return 0


def _build_and_lock(env, *, force: bool = False, workspace: bool = True, dry_run: bool = False) -> None:
    from .export import write_lock
    for warning in env.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    env.warnings = []
    build_image(env, force=force, dry_run=dry_run)
    if workspace:
        prepare_workspace(env, dry_run=dry_run)
    if not dry_run:
        print(f"Wrote {write_lock(env)}")


def cmd_build(project: Project, args: argparse.Namespace) -> int:
    env = load_env(project, args.name, check_binds=False)
    if env.changes:
        header = "Locking recipe versions:" if not env.lock_file.is_file() else "Lock updated for rebox.toml changes:"
        _print_changes(env.changes, header)
    _build_and_lock(env, force=args.force, workspace=not args.no_workspace, dry_run=args.dry_run)
    return 0


def cmd_outdated(project: Project, args: argparse.Namespace) -> int:
    from .recipes import read_locked
    env = load_env(project, args.name, check_binds=False, refresh_all=True, fetch=not args.no_fetch)
    locked = {name: lock for (_, name), lock in read_locked(env.lock_file).items()}
    print(f"{'recipe':<26} {'locked':<10} {'update to':<10} newest")
    outdated = 0
    for recipe in env.recipes:
        lock = locked.get(recipe.name)
        newest = env.sources[recipe.source].versions(recipe.name)[0]
        old = str(lock.version) if lock else "-"
        changed = lock is None or lock.version != recipe.version or lock.sha256 != recipe.digest
        outdated += changed
        target = str(recipe.version) if changed else "="
        note = "" if newest == recipe.version else "  (excluded by a constraint)"
        print(f"{recipe.source + ':' + recipe.name:<26} {old:<10} {target:<10} {newest}{note}")
    print(f"\n{outdated} recipe(s) can be updated" + (f": rebox update {env.name}" if outdated else ""))
    return 0


def cmd_update(project: Project, args: argparse.Namespace) -> int:
    names = set(args.recipes)
    env = load_env(project, args.name, check_binds=False, refresh=names, refresh_all=not names,
                   fetch=not args.no_fetch)
    unknown = names - {r.name for r in env.recipes}
    if unknown:
        raise ConfigError(f"Not used by '{env.name}': {', '.join(sorted(unknown))}")
    if not env.changes:
        print(f"'{env.name}' is up to date: nothing to rebuild.")
        return 0
    _print_changes(env.changes, f"Updating '{env.name}':")
    if args.dry_run:
        return 0
    if args.no_build:
        from .export import write_lock
        env.def_file.parent.mkdir(parents=True, exist_ok=True)
        print(f"Wrote {write_lock(env)} (the image is rebuilt by the next `rebox build {env.name}`)")
        return 0
    _build_and_lock(env, workspace=False)
    return 0


def cmd_shell(project: Project, args: argparse.Namespace) -> int:
    env = load_env(project, args.name, resolve_recipes=False)
    cmd = build_command(env, shell_argv(env), network_override=args.network)
    return run_command(env, cmd, dry_run=args.dry_run)


def cmd_run(project: Project, args: argparse.Namespace) -> int:
    env = load_env(project, args.name, resolve_recipes=False)
    inner = list(args.command)
    if inner and inner[0] == "--":
        inner = inner[1:]
    if not inner:
        raise ConfigError("No command given, e.g.: rebox run NAME -- python script.py")
    if not args.no_pixi and env.has_recipe("pixi") and (env.workspace / "pixi.toml").is_file():
        inner = ["pixi", "run", "--manifest-path", f"{WORKSPACE_TARGET}/pixi.toml", "--", *inner]
    cmd = build_command(env, inner, network_override=args.network)
    return run_command(env, cmd, dry_run=args.dry_run)


def cmd_lock(project: Project, args: argparse.Namespace) -> int:
    from .export import write_lock
    env = load_env(project, args.name, check_binds=False)
    if env.changes:
        _print_changes(env.changes, "Lock changes:")
    print(f"Wrote {write_lock(env)}")
    return 0


def cmd_export(project: Project, args: argparse.Namespace) -> int:
    from .export import export_env
    env = load_env(project, args.name, check_binds=False)
    out = export_env(env, Path(args.output).resolve())
    print(f"Exported '{env.name}' to {out}")
    print(f"Rebuild elsewhere with: cd {out} && rebox build {env.name}")
    return 0


def cmd_pack(project: Project, args: argparse.Namespace) -> int:
    from .export import pack_env
    env = load_env(project, args.name, check_binds=False)
    output = Path(args.output or f"{env.name}-bundle.tar").resolve()
    pack_env(env, output, include_home=args.include_home)
    size = output.stat().st_size / 1024 ** 2
    print(f"Bundle written: {output} ({size:.0f} MB)")
    print(f"On the target machine (with rebox installed): rebox unpack {output.name}")
    return 0


def cmd_unpack(args: argparse.Namespace) -> int:
    from .export import unpack_bundle
    from . import __version__
    target, manifest = unpack_bundle(Path(args.bundle).resolve(),
                                     Path(args.directory) if args.directory else None)
    packed_with = manifest.get("rebox_version", "?")
    if packed_with.split(".")[:2] != __version__.split("+")[0].split(".")[:2]:
        print(f"note: bundle made with rebox {packed_with}, unpacked with rebox {__version__}")
    name = manifest["environment"]
    print(f"Unpacked environment '{name}' into {target}")
    print(f"Next: cd {target} && rebox shell {name}")
    return 0


def cmd_clean(project: Project, args: argparse.Namespace) -> int:
    env = load_env(project, args.name, check_binds=False)
    clean(env, all_state=args.all)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rebox", description="rebox (research sandbox): rootless Apptainer + Pixi sandboxes")
    p.add_argument("--version", action="version", version=f"rebox {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create a rebox project (rebox-defaults.toml, recipes/, envs/)")
    s.add_argument("directory", nargs="?", default=".")
    s.add_argument("--examples", action="store_true", help="add the example environments")
    s.add_argument("--display", action=argparse.BooleanOptionalAction, default=None,
                   help="enable display access by default (default: if a display is found)")
    s.add_argument("--gpu", action=argparse.BooleanOptionalAction, default=None,
                   help="enable GPU access by default (default: if a GPU is found)")
    s.set_defaults(func=cmd_init, needs_project=False)

    sub.add_parser("list", help="list recipes (all versions) and environments").set_defaults(func=cmd_list)

    s = sub.add_parser("sources", help="show recipe sources")
    s.add_argument("--fetch", action="store_true", help="fetch git sources first")
    s.set_defaults(func=cmd_sources)

    s = sub.add_parser("new", help="create a new environment")
    s.add_argument("name")
    s.add_argument("recipes", nargs="*", help="recipes to install (besides default recipes)")
    s.add_argument("-d", "--description")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("show", help="show the resolved configuration")
    s.add_argument("name")
    s.add_argument("--definition", action="store_true", help="also print the Apptainer .def file")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("build", help="build the image and install the workspace Pixi environment")
    s.add_argument("name")
    s.add_argument("--force", action="store_true", help="rebuild even if up to date")
    s.add_argument("--no-workspace", action="store_true", help="skip `pixi install` in the workspace")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_build)

    for name, func, help_ in (("shell", cmd_shell, "open an interactive shell in the sandbox"),
                              ("run", cmd_run, "run a command in the sandbox")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("name")
        s.add_argument("--network", choices=["none", "host", "allowlist"], help="override the network setting")
        s.add_argument("--dry-run", action="store_true", help="print the command instead of running it")
        if name == "run":
            s.add_argument("--no-pixi", action="store_true", help="do not wrap the command in `pixi run`")
            s.add_argument("command", nargs="*", help="command to run, after --")
        s.set_defaults(func=func)

    s = sub.add_parser("lock", help="resolve recipe versions into envs/NAME/rebox.lock.toml (no build)")
    s.add_argument("name")
    s.set_defaults(func=cmd_lock)

    s = sub.add_parser("outdated", help="show recipes with newer versions")
    s.add_argument("name")
    s.add_argument("--no-fetch", action="store_true", help="do not fetch git sources")
    s.set_defaults(func=cmd_outdated)

    s = sub.add_parser("update", help="move recipes to their newest allowed versions and rebuild the image")
    s.add_argument("name")
    s.add_argument("recipes", nargs="*", help="recipes to update (default: all)")
    s.add_argument("--dry-run", action="store_true", help="only show what would change")
    s.add_argument("--no-build", action="store_true", help="update the lock file only")
    s.add_argument("--no-fetch", action="store_true", help="do not fetch git sources")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("export", help="export a standalone, reproducible configuration")
    s.add_argument("name")
    s.add_argument("-o", "--output", required=True, help="output directory")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("pack", help="create an offline bundle (configuration, image, Pixi workspace)")
    s.add_argument("name")
    s.add_argument("-o", "--output", help="output tarball (.tar or .tar.gz)")
    s.add_argument("--include-home", action="store_true",
                   help="include the persistent home directory (may contain secrets)")
    s.set_defaults(func=cmd_pack)

    s = sub.add_parser("unpack", help="unpack a bundle made by `rebox pack` (no network needed)")
    s.add_argument("bundle", help="bundle tarball")
    s.add_argument("directory", nargs="?", help="target directory (default: ./<bundle name>)")
    s.set_defaults(func=cmd_unpack, needs_project=False)

    s = sub.add_parser("clean", help="remove the built image")
    s.add_argument("name")
    s.add_argument("--all", action="store_true", help="also remove the persistent home")
    s.set_defaults(func=cmd_clean)
    return p


def _expose_environment_tools() -> None:
    """Make apptainer, mksquashfs, git... of rebox's own environment reachable.

    With `pixi global install rebox`, only the `rebox` command is exposed; the
    tools it runs live next to the Python interpreter.
    """
    bindir = str(Path(sys.prefix) / "bin")
    path = os.environ.get("PATH", "")
    if bindir not in path.split(os.pathsep):
        os.environ["PATH"] = bindir + (os.pathsep + path if path else "")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    _expose_environment_tools()
    args = parser().parse_args(argv)
    try:
        if not getattr(args, "needs_project", True):
            return args.func(args)
        project = Project.discover()
        return args.func(project, args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed ({exc.returncode}): {shlex.join(map(str, exc.cmd))}", file=sys.stderr)
        return exc.returncode or 1
    except KeyboardInterrupt:
        return 130
