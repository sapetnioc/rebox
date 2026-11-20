"""Creating rebox projects (`rebox init`)."""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import ConfigError

DATA_DIR = Path(__file__).resolve().parent / "data"

GITIGNORE = """\
# rebox generated files
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


def init_project(directory: Path, *, examples: bool = False) -> list[str]:
    """Create a rebox project in ``directory``; return notes for the user."""
    notes: list[str] = []
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "rebox-defaults.toml").exists():
        raise ConfigError(f"{directory} already contains a rebox project (rebox-defaults.toml exists)")
    shutil.copy2(DATA_DIR / "rebox-defaults.toml", directory / "rebox-defaults.toml")
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
    return notes
