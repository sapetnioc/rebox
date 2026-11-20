"""Tests of `rebox init`, the builtin recipe source and host environment packing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebox import __version__
from rebox.cli import main
from rebox.config import ConfigError, Project, load_env
from rebox.hostenv import host_manifest, installed_packages
from rebox.init import init_project
from rebox.recipes import read_locked


@pytest.fixture()
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("REBOX_ROOT", raising=False)
    monkeypatch.delenv("PIXI_PROJECT_ROOT", raising=False)
    return tmp_path / "proj"


def add_env(project_dir: Path, name: str = "e", recipes: str = "[]") -> None:
    (project_dir / "envs" / name).mkdir(parents=True)
    (project_dir / "envs" / name / "rebox.toml").write_text(f"[build]\nrecipes = {recipes}\n")


def test_init_creates_a_working_project(project_dir):
    assert main(["init", str(project_dir), "--examples"]) == 0
    for path in ("rebox-defaults.toml", "recipes/source.toml", ".gitignore",
                 "envs/malware-lab/rebox.toml", "envs/vscode/workspace/pixi.toml"):
        assert (project_dir / path).is_file(), path
    assert not (project_dir / "pixi.toml").exists()

    env = load_env(Project(project_dir), "malware-lab", check_binds=False)
    assert [r.ref for r in env.recipes][:2] == ["builtin:base@1.0.0", "builtin:pixi@1.1.0"]

    with pytest.raises(ConfigError, match="already contains"):
        init_project(project_dir)


def test_old_file_names_are_reported(project_dir, monkeypatch):
    (project_dir / "envs" / "old").mkdir(parents=True)
    (project_dir / "defaults.toml").write_text("")
    (project_dir / "envs" / "old" / "sandbox.toml").write_text("")
    monkeypatch.chdir(project_dir)
    with pytest.raises(ConfigError, match="rename defaults.toml to rebox-defaults.toml"):
        Project.discover()
    init_project(project_dir)
    with pytest.raises(ConfigError, match="sandbox.toml to rebox.toml"):
        load_env(Project(project_dir), "old")


def test_local_recipes_override_builtin(project_dir):
    init_project(project_dir)
    custom = project_dir / "recipes" / "pixi" / "9.0.0.toml"
    custom.parent.mkdir()
    custom.write_text('requires = ["base"]\npost = "echo my pixi"\n')
    add_env(project_dir)
    env = load_env(Project(project_dir), "e")
    assert [r.ref for r in env.recipes] == ["builtin:base@1.0.0", "local:pixi@9.0.0"]


def test_builtin_lock_entry(project_dir, monkeypatch):
    monkeypatch.setattr("rebox.export.apptainer_version", lambda: "test")
    init_project(project_dir)
    add_env(project_dir)
    from rebox.export import write_lock
    env = load_env(Project(project_dir), "e")
    write_lock(env)
    assert env.lock_file.name == "rebox.lock.toml"
    text = env.lock_file.read_text()
    assert 'source_type = "builtin"' in text and f'rebox_version = "{__version__}"' in text
    assert set(read_locked(env.lock_file)) == {("builtin", "base"), ("builtin", "pixi")}


def fake_prefix(tmp_path: Path, with_rebox: bool = True) -> Path:
    meta = tmp_path / "prefix" / "conda-meta"
    meta.mkdir(parents=True)
    records = [
        {"name": "python", "version": "3.13.1", "build": "h1_0", "subdir": "linux-64",
         "channel": "https://conda.anaconda.org/conda-forge"},
        {"name": "apptainer", "version": "1.5.4", "build": "h2_0", "subdir": "linux-64",
         "url": "https://conda.anaconda.org/conda-forge/linux-64/apptainer-1.5.4-h2_0.conda"},
    ]
    if with_rebox:
        records.append({"name": "rebox", "version": "0.4.0", "build": "pyh_0", "subdir": "noarch",
                        "channel": "file:///srv/channel"})
    for r in records:
        (meta / f"{r['name']}-{r['version']}-{r['build']}.json").write_text(json.dumps(r))
    (meta / "history").write_text("")
    return tmp_path / "prefix"


def test_host_manifest_pins_the_running_environment(tmp_path):
    manifest = host_manifest(installed_packages(fake_prefix(tmp_path)))
    assert manifest["workspace"]["channels"] == ["file:///srv/channel", "https://conda.anaconda.org/conda-forge"]
    assert manifest["workspace"]["platforms"] == ["linux-64"]
    assert manifest["dependencies"]["apptainer"] == {
        "version": "==1.5.4", "build": "h2_0", "channel": "https://conda.anaconda.org/conda-forge"}
    assert manifest["dependencies"]["rebox"]["channel"] == "file:///srv/channel"


def test_pack_needs_rebox_as_a_conda_package(tmp_path):
    with pytest.raises(ConfigError, match="not installed as a conda package"):
        host_manifest(installed_packages(fake_prefix(tmp_path, with_rebox=False)))
