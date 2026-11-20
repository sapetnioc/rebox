"""Tests of `rebox init`, the builtin recipe source and host environment packing."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebox import __version__
from rebox.cli import main
from rebox.config import ConfigError, Project, load_env
from rebox.init import init_project
from rebox.recipes import BuiltinSource, read_locked


def latest(name: str) -> str:
    """Newest version of a builtin recipe (tests must not hardcode it)."""
    return f"builtin:{name}@{BuiltinSource('builtin').versions(name)[0]}"


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
    assert [r.ref for r in env.recipes][:2] == [latest("base"), latest("pixi")]

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
    assert [r.ref for r in env.recipes] == [latest("base"), "local:pixi@9.0.0"]


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


def test_pack_and_unpack(project_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("rebox.export.apptainer_version", lambda: "test")
    init_project(project_dir)
    add_env(project_dir)
    env = load_env(Project(project_dir), "e")
    env.image.parent.mkdir(parents=True)
    env.image.write_bytes(b"fake image")
    (env.workspace / "pixi.toml").parent.mkdir(parents=True)
    (env.workspace / "pixi.toml").write_text("[workspace]\n")
    (env.workspace / ".pixi" / "envs" / "default").mkdir(parents=True)
    (env.home_dir / "secret").parent.mkdir(parents=True)
    (env.home_dir / "secret").write_text("token")

    from rebox.export import pack_env, unpack_bundle
    bundle = pack_env(env, tmp_path / "e-bundle.tar")
    import tarfile
    names = tarfile.open(bundle).getnames()
    assert "e-bundle/rebox-bundle.toml" in names
    assert "e-bundle/envs/e/.state/image.sif" in names
    assert "e-bundle/envs/e/workspace/.pixi/envs/default" in names
    assert not any("secret" in n for n in names)  # home left out by default
    assert not any(n.endswith(("host-env.sh", "install.sh")) for n in names)

    monkeypatch.chdir(tmp_path)
    assert main(["unpack", str(bundle), "deployed"]) == 0
    assert "rebox shell e" in capsys.readouterr().out
    deployed = Project(tmp_path / "deployed")
    unpacked = load_env(deployed, "e")
    assert unpacked.image.read_bytes() == b"fake image"
    assert [r.ref for r in unpacked.recipes] == [r.ref for r in env.recipes]

    with pytest.raises(ConfigError, match="not empty"):
        unpack_bundle(bundle, tmp_path / "deployed")


def test_unpack_rejects_corrupted_or_foreign_archives(tmp_path):
    import io, tarfile
    from rebox.export import unpack_bundle
    foreign = tmp_path / "foreign.tar"
    with tarfile.open(foreign, "w") as tar:
        data = b"hello"
        info = tarfile.TarInfo("x/readme.txt"); info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ConfigError, match="not a rebox bundle"):
        unpack_bundle(foreign, tmp_path / "out")

    bad = tmp_path / "bad.tar"
    manifest = b'format = 1\nenvironment = "e"\nimage_sha256 = "0"\n'
    image = b"tampered"
    with tarfile.open(bad, "w") as tar:
        for name, data in (("b/rebox-bundle.toml", manifest), ("b/envs/e/.state/image.sif", image)):
            info = tarfile.TarInfo(name); info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ConfigError, match="corrupted"):
        unpack_bundle(bad, tmp_path / "out2")
    assert not (tmp_path / "out2").exists()
