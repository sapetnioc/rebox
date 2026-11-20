"""Tests of recipe sources, versions, resolution, locking and updates."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from rebox import run as run_mod
from rebox.cli import main
from rebox.config import ConfigError, Project, load_env
from rebox.export import export_env, write_lock
from rebox.build import render_def
from rebox.recipes import Constraint, RecipeRef, Version, read_locked


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def recipe(path: Path, *, post: str = "true", requires=(), args=None) -> None:
    lines = [f"requires = {list(requires)!r}".replace("'", '"'), f'post = "{post}"']
    if args:
        lines.append("[args]")
        lines += [f'{k} = "{v}"' for k, v in args.items()]
    write(path, "\n".join(lines) + "\n")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
                               "HOME": str(cwd)}).stdout.strip()


# --------------------------------------------------------------------------- #
# Versions and constraints
# --------------------------------------------------------------------------- #

def test_version_ordering():
    versions = [Version.parse(v) for v in ["1.10.0", "1.2", "1.2.0-rc1", "1.2.1", "0.9", "2"]]
    assert [v.text for v in sorted(versions)] == ["0.9", "1.2.0-rc1", "1.2", "1.2.1", "1.10.0", "2"]
    assert Version.parse("1.2") == Version.parse("1.2.0")
    with pytest.raises(ConfigError):
        Version.parse("latest")


@pytest.mark.parametrize("constraint,version,ok", [
    ("", "3.1", True),
    ("latest", "3.1", True),
    ("1.2", "1.2.7", True),
    ("1.2", "1.3.0", False),
    ("1.2.*", "1.2.0", True),
    ("1", "1.9", True),
    ("==1.2.0", "1.2", True),
    ("==1.2.0", "1.2.1", False),
    (">=1.2,<2", "1.9.9", True),
    (">=1.2,<2", "2.0", False),
    ("!=1.3", "1.3.0", False),
    (">1.0", "1.0.0", False),
])
def test_constraints(constraint, version, ok):
    assert Constraint(constraint).allows(Version.parse(version)) is ok


def test_recipe_ref():
    ref = RecipeRef.parse("team:node-js@>=1.2,<2")
    assert (ref.source, ref.name, ref.constraint.text) == ("team", "node-js", ">=1.2,<2")
    assert RecipeRef.parse("base").source is None
    for bad in ("Bad Name", "x@>>1", "a:b:c"):
        with pytest.raises(ConfigError):
            RecipeRef.parse(bad)


# --------------------------------------------------------------------------- #
# Projects with several sources
# --------------------------------------------------------------------------- #

@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.setattr(run_mod, "apptainer_bin", lambda: "apptainer")
    monkeypatch.setattr(run_mod, "user_systemd_available", lambda: False)
    monkeypatch.setenv("REBOX_ROOT", str(tmp_path))
    monkeypatch.setattr("rebox.export.apptainer_version", lambda: "test")

    # A git repository holding team recipes in a sub-directory.
    repo = tmp_path / "team-repo"
    recipe(repo / "recipes" / "tool" / "1.0.0.toml", post="echo team tool 1.0")
    recipe(repo / "recipes" / "extra" / "1.0.0.toml", requires=["tool"])
    write(repo / "README.md", "team recipes\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "v1")

    write(tmp_path / "defaults.toml", f"""
        [sources.builtin]
        path = "recipes"

        [sources.team]
        git = "{repo.as_uri()}"
        ref = "main"
        subdir = "recipes"

        [build]
        base_image = "docker://debian:12-slim"
        default_recipes = ["base"]
    """)
    recipe(tmp_path / "recipes" / "base" / "1.0.0.toml", post="echo base 1.0")
    recipe(tmp_path / "recipes" / "base" / "1.1.0.toml", post="echo base 1.1")
    recipe(tmp_path / "recipes" / "base" / "2.0.0.toml", post="echo base 2.0")
    recipe(tmp_path / "recipes" / "node" / "1.0.0.toml", requires=["base@1"], args={"NODE": "20"})
    recipe(tmp_path / "recipes" / "tool" / "0.5.toml", post="echo builtin tool")
    write(tmp_path / "envs" / "demo" / "sandbox.toml", """
        [build]
        recipes = ["node", "team:extra"]
    """)
    return Project(tmp_path)


def names(env):
    return [f"{r.source}:{r.name}@{r.version}" for r in env.recipes]


def test_resolution_across_sources(project):
    env = load_env(project, "demo")
    # base is limited to 1.x by node; extra's "tool" comes from its own source.
    assert names(env) == ["builtin:base@1.1.0", "builtin:node@1.0.0", "team:tool@1.0.0", "team:extra@1.0.0"]
    tool = next(r for r in env.recipes if r.name == "tool")
    assert tool.commit and len(tool.commit) == 40
    assert "echo team tool 1.0" in render_def(env)


def test_unqualified_reference_uses_source_order(project):
    path = project.env_dir("demo") / "sandbox.toml"
    path.write_text('[build]\nrecipes = ["tool"]\n')
    assert "builtin:tool@0.5" in names(load_env(project, "demo"))
    path.write_text('[build]\nrecipes = ["team:tool"]\n')
    assert "team:tool@1.0.0" in names(load_env(project, "demo"))


def test_same_name_from_two_sources_is_an_error(project):
    path = project.env_dir("demo") / "sandbox.toml"
    path.write_text('[build]\nrecipes = ["tool", "team:extra"]\n')
    with pytest.raises(ConfigError, match="two sources"):
        load_env(project, "demo")


def test_unsatisfiable_constraint(project):
    path = project.env_dir("demo") / "sandbox.toml"
    path.write_text('[build]\nrecipes = ["base@2", "node"]\n')
    with pytest.raises(ConfigError, match="No version of builtin:base"):
        load_env(project, "demo")


def test_env_can_add_and_disable_sources(project, tmp_path):
    recipe(tmp_path / "mine" / "own" / "3.0.toml")
    path = project.env_dir("demo") / "sandbox.toml"
    path.write_text(textwrap.dedent(f"""
        [sources.team]
        enabled = false
        [sources.mine]
        path = "{tmp_path / 'mine'}"
        [build]
        recipes = ["own"]
    """))
    assert names(load_env(project, "demo"))[-1] == "mine:own@3.0"
    path.write_text(path.read_text().replace('recipes = ["own"]', 'recipes = ["team:extra"]'))
    with pytest.raises(ConfigError, match="unknown source 'team'"):
        load_env(project, "demo")


# --------------------------------------------------------------------------- #
# Lock and update
# --------------------------------------------------------------------------- #

def test_lock_pins_versions_until_update(project, capsys):
    env = load_env(project, "demo")
    write_lock(env)
    locked = read_locked(env.lock_file)
    assert locked[("builtin", "base")].version == Version.parse("1.1.0")

    # New versions are published: the lock keeps the environment unchanged...
    recipe(project.root / "recipes" / "base" / "1.2.0.toml", post="echo base 1.2")
    repo = project.root / "team-repo"
    recipe(repo / "recipes" / "tool" / "1.1.0.toml", post="echo team tool 1.1")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "v1.1")
    env = load_env(project, "demo")
    assert "builtin:base@1.1.0" in names(env) and "team:tool@1.0.0" in names(env)
    assert env.changes == []

    # ... until an update is requested (git sources are fetched).
    assert main(["outdated", "demo"]) == 0
    out = capsys.readouterr().out
    assert "builtin:base" in out and "1.2.0" in out and "2 recipe(s) can be updated" in out

    assert main(["update", "demo", "--no-build"]) == 0
    env = load_env(project, "demo")
    assert "builtin:base@1.2.0" in names(env) and "team:tool@1.1.0" in names(env)
    assert "echo team tool 1.1" in render_def(env)


def test_update_single_recipe(project):
    write_lock(load_env(project, "demo"))
    recipe(project.root / "recipes" / "base" / "1.2.0.toml")
    recipe(project.root / "recipes" / "node" / "1.1.0.toml", requires=["base@1"], args={"NODE": "22"})
    env = load_env(project, "demo", refresh={"node"})
    assert "builtin:node@1.1.0" in names(env) and "builtin:base@1.1.0" in names(env)
    assert env.recipe_args["node"] == {"NODE": "22"}


def test_modified_published_version_is_detected(project):
    write_lock(load_env(project, "demo"))
    recipe(project.root / "recipes" / "base" / "1.1.0.toml", post="echo tampered")
    with pytest.raises(ConfigError, match="changed since it was locked"):
        load_env(project, "demo")
    env = load_env(project, "demo", refresh={"base"})  # accepting the change
    assert ("builtin:base", "1.1.0", "1.1.0") in env.changes


def test_lock_follows_sandbox_toml_changes(project):
    write_lock(load_env(project, "demo"))
    path = project.env_dir("demo") / "sandbox.toml"
    path.write_text('[build]\nrecipes = ["node", "team:extra", "base@1.0"]\n')
    env = load_env(project, "demo")
    assert "builtin:base@1.0.0" in names(env)
    assert ("builtin:base", "1.1.0", "1.0.0") in env.changes


def test_git_lock_is_reproducible_after_new_commits(project, tmp_path):
    env = load_env(project, "demo")
    write_lock(env)
    commit = read_locked(env.lock_file)[("team", "tool")].commit
    # Rewrite the published file on the branch (bad practice, but the lock
    # still reads the locked commit) and drop the local mirror.
    repo = project.root / "team-repo"
    recipe(repo / "recipes" / "tool" / "1.0.0.toml", post="echo rewritten")
    git(repo, "commit", "-qam", "rewrite")
    import shutil
    shutil.rmtree(project.root / ".sources")
    env = load_env(project, "demo")
    tool = next(r for r in env.recipes if r.name == "tool")
    assert tool.commit == commit and "echo team tool 1.0" in tool.text


def test_export_inlines_sources(project, tmp_path):
    env = load_env(project, "demo")
    out = export_env(env, tmp_path / "export")
    assert (out / "recipes" / "team" / "tool" / "1.0.0.toml").is_file()
    exported = load_env(Project(out), "demo")
    assert names(exported) == names(env)
    assert render_def(exported) == render_def(env)
    assert all(e.commit is None for e in read_locked(exported.lock_file).values())
