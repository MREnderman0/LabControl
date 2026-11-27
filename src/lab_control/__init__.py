"""LabControl desktop workstation."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("labcontrol")
except PackageNotFoundError:
    # Running directly from a source checkout before an editable install.
    import tomllib
    from pathlib import Path

    with (Path(__file__).resolve().parents[2] / "pyproject.toml").open("rb") as project:
        __version__ = tomllib.load(project)["project"]["version"]
