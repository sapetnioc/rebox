"""Tests of `rebox init`, the builtin recipe source and packaging helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebox import __version__, init as init_mod
from rebox.cli import main
from rebox.config import ConfigError, Project, load_env
from rebox.export import pack_env
from rebox.init import has_rebox_dependency, init_project, pixi_manifest
from rebox.recipes import read_locked


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("REBOX_ROOT", raising=False)
    monkeypatch.delenv("PIXI_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(init_mod, "installed_channel", lambda: "file:///opt/channel")
    return tmp_path / "proj"


def test_init_creates_a_working_project(project_dir, monkeypatch, capsys):
    assert main(["init", str(project_dir), "--examples"]) == 0
    for path in ("defaults.toml", "recipes/source.toml", ".gitignore", "pixi.toml",
                 "envs/malware-lab/sandbox.toml", "envs/vscode/workspace/pixi.toml"):
        assert (project_dir / path).is_file(), path
    manifest = (project_dir / "pixi.toml").read_text()
    assert '"file:///opt/channel", "conda-forge"' in manifest
    assert has_rebox_dependency(project_dir / "pixi.toml")

    # Example environments resolve against the builtin recipes.
    env = load_env(Project(project_dir), "malware-lab", check_binds=False)
    assert [r.ref for r in env.recipes][:2] == ["builtin:base@1.0.0", "builtin:pixi@1.0.0"]
    assert env.recipes[0].source == "builtin"

    with pytest.raises(ConfigError, match="already contains"):
        init_project(project_dir)


def test_local_recipes_override_builtin(project_dir):
    init_project(project_dir, pixi=False)
    custom = project_dir / "recipes" / "pixi" / "9.0.0.toml"
    custom.parent.mkdir()
    custom.write_text('requires = ["base"]\npost = "echo my pixi"\n')
    (project_dir / "envs" / "e").mkdir()
    (project_dir / "envs" / "e" / "sandbox.toml").write_text("[build]\nrecipes = []\n")
    env = load_env(Project(project_dir), "e")
    assert [r.ref for r in env.recipes] == ["builtin:base@1.0.0", "local:pixi@9.0.0"]


def test_builtin_lock_entry(project_dir, monkeypatch):
    monkeypatch.setattr("rebox.export.apptainer_version", lambda: "test")
    init_project(project_dir, pixi=False)
    (project_dir / "envs" / "e").mkdir()
    (project_dir / "envs" / "e" / "sandbox.toml").write_text("[build]\nrecipes = []\n")
    from rebox.export import write_lock
    env = load_env(Project(project_dir), "e")
    write_lock(env)
    text = env.lock_file.read_text()
    assert 'source_type = "builtin"' in text and f'rebox_version = "{__version__}"' in text
    assert set(read_locked(env.lock_file)) == {("builtin", "base"), ("builtin", "pixi")}


def test_pack_requires_rebox_in_pixi_toml(project_dir, monkeypatch):
    init_project(project_dir, pixi=False)
    (project_dir / "envs" / "e").mkdir()
    (project_dir / "envs" / "e" / "sandbox.toml").write_text("[build]\nrecipes = []\n")
    env = load_env(Project(project_dir), "e")
    env.image.parent.mkdir(parents=True)
    env.image.write_text("fake image")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    with pytest.raises(ConfigError, match="must exist and depend on rebox"):
        pack_env(env, project_dir / "out.tar")


def test_pixi_manifest_pins_rebox():
    text = pixi_manifest("demo", ["conda-forge"])
    assert 'name = "demo"' in text and "rebox = " in text
