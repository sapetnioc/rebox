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


def defaults_for_host(*, display: bool | None = None, gpu: bool | None = None,
                      notes: list[str] | None = None) -> str:
    """The rebox-defaults.toml template, with display/gpu enabled if the host has them.

    ``None`` means auto-detect; ``True`` / ``False`` force the value.
    """
    from .hostcaps import detect_display, detect_gpus

    notes = [] if notes is None else notes
    text = (DATA_DIR / "rebox-defaults.toml").read_text()
    displays = detect_display()
    gpus = detect_gpus()
    use_display = bool(displays) if display is None else display
    use_gpu = bool(gpus) if gpu is None else gpu
    if use_display:
        found = f"found by rebox init: {', '.join(displays)}" if displays else "forced by rebox init"
        text = text.replace("\ndisplay = false\n", f"\ndisplay = true  # {found}\n", 1)
    if use_gpu:
        found = f"found by rebox init: {', '.join(gpus)}" if gpus else "forced by rebox init"
        text = text.replace("\ngpu = false\n", f"\ngpu = true  # {found}\n", 1)
    notes.append(f"display access: {'on' if use_display else 'off'}"
                 + (f" ({', '.join(displays)})" if displays else " (no display found)"))
    notes.append(f"GPU access: {'on' if use_gpu else 'off'}"
                 + (f" ({', '.join(gpus)})" if gpus else " (no GPU found)"))
    return text


def init_project(directory: Path, *, examples: bool = False, display: bool | None = None,
                 gpu: bool | None = None) -> list[str]:
    """Create a rebox project in ``directory``; return notes for the user."""
    notes: list[str] = []
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "rebox-defaults.toml").exists():
        raise ConfigError(f"{directory} already contains a rebox project (rebox-defaults.toml exists)")
    (directory / "rebox-defaults.toml").write_text(defaults_for_host(display=display, gpu=gpu, notes=notes))
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
