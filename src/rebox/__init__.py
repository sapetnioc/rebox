"""rebox (research sandbox): rootless sandbox environments built from reusable recipes, Apptainer and Pixi."""

from __future__ import annotations


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("rebox")
    except PackageNotFoundError:  # running from a source checkout
        import tomllib
        from pathlib import Path

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        try:
            return tomllib.loads(pyproject.read_text())["project"]["version"] + "+dev"
        except (OSError, KeyError, tomllib.TOMLDecodeError):
            return "0+unknown"


__version__ = _version()
