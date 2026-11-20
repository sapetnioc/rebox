"""Creating rebox projects (`rebox init`) and their Pixi manifest."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError

DATA_DIR = Path(__file__).resolve().parent / "data"

GITIGNORE = """\
# rebox generated files
.pixi/
.sources/
envs/*/.state/
envs/*/workspace/.pixi/
*-bundle.tar
*-bundle.tar.gz
"""

LOCAL_SOURCE = """\
# Your own recipes. Layout: <recipe>/<version>.toml, e.g. myapp/1.0.0.toml.
# A published version must never be modified: add a new version instead.
description = "Recipes of this project"
"""


def installed_channel() -> str | None:
    """Channel the running rebox conda package was installed from, if any."""
    meta = Path(sys.prefix) / "conda-meta"
    for record in sorted(meta.glob("rebox-*.json")):
        try:
            data = json.loads(record.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("name") == "rebox" and data.get("channel"):
            return str(data["channel"]).rstrip("/")
    return None


def rebox_spec() -> str:
    return "*" if "+" in __version__ else f">={__version__}"


def pixi_manifest(name: str, channels: list[str]) -> str:
    channel_list = ", ".join(f'"{c}"' for c in channels)
    return f"""\
# Host tools of this rebox project: `pixi run rebox ...` uses the rebox
# version locked in pixi.lock, and `rebox pack` bundles this environment.
[workspace]
name = "{name}"
channels = [{channel_list}]
platforms = ["linux-64"]

[dependencies]
rebox = "{rebox_spec()}"
"""


def project_channels(extra: list[str] | None = None) -> list[str]:
    channels = list(extra or [])
    detected = installed_channel()
    if detected and not detected.endswith("/conda-forge") and detected != "conda-forge":
        channels.append(detected)
    channels.append("conda-forge")
    return list(dict.fromkeys(channels))


def init_project(directory: Path, *, examples: bool = False, channels: list[str] | None = None,
                 pixi: bool = True) -> list[str]:
    """Create a rebox project in ``directory``; return notes for the user."""
    notes: list[str] = []
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "defaults.toml").exists():
        raise ConfigError(f"{directory} already contains a rebox project (defaults.toml exists)")
    shutil.copy2(DATA_DIR / "defaults.toml", directory / "defaults.toml")
    recipes = directory / "recipes"
    recipes.mkdir(exist_ok=True)
    if not (recipes / "source.toml").exists():
        (recipes / "source.toml").write_text(LOCAL_SOURCE)
    envs = directory / "envs"
    envs.mkdir(exist_ok=True)
    if examples:
        for example in sorted((DATA_DIR / "examples").iterdir()):
            if example.is_dir() and not (envs / example.name).exists():
                shutil.copytree(example, envs / example.name)
                notes.append(f"example environment: envs/{example.name}")
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(GITIGNORE)
    if pixi:
        manifest = directory / "pixi.toml"
        if manifest.exists():
            notes.append("pixi.toml already exists: make sure it depends on `rebox`")
        else:
            chosen = project_channels(channels)
            manifest.write_text(pixi_manifest(directory.resolve().name, chosen))
            if len(chosen) == 1:
                notes.append("pixi.toml only lists conda-forge: add the channel rebox is published "
                             "on (rebox init --channel URL)")
    return notes


def has_rebox_dependency(manifest: Path) -> bool:
    import tomllib
    try:
        data = tomllib.loads(manifest.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "rebox" in data.get("dependencies", {})
