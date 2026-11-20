"""Detecting and exposing the host display (X11, Wayland) and GPUs.

Used by `rebox init` (to enable ``display`` / ``gpu`` by default when the host
has them) and when starting a sandbox (to build the Apptainer options).

Display sockets are bind-mounted as files, which works in every network mode
(``none``, ``allowlist``, ``host``): only abstract Unix sockets, which belong
to the host network namespace, are unreachable, and X11 clients fall back to
the ``/tmp/.X11-unix/X<n>`` file. X11 over TCP (e.g. ``ssh -X``) only works
with ``network = "host"``.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

X11_DIR = "/tmp/.X11-unix"
X11_AUTH_TARGET = "/run/rebox-x11/Xauthority"
RUNTIME_TARGET = "/run/rebox-runtime"  # XDG_RUNTIME_DIR inside the sandbox

GPU_KINDS = ("nvidia", "amd", "dri")


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #


@dataclass
class DisplayAccess:
    """What a sandbox needs to talk to the host display."""

    binds: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    temp_dirs: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    found: list[str] = field(default_factory=list)  # e.g. ["X11 :0", "Wayland wayland-0"]


_LOCAL_DISPLAY = re.compile(r"^(?:unix)?:(\d+)(?:\.\d+)?$")


def x11_socket(display: str, root: Path = Path("/")) -> Path | None:
    """Socket file of a local X11 display (':0', ':1.0', 'unix:0'), if it exists."""
    match = _LOCAL_DISPLAY.match(display.strip())
    if not match:
        return None
    socket = root / X11_DIR.lstrip("/") / f"X{match.group(1)}"
    return socket if socket.exists() else None


def wayland_socket(environ: dict[str, str]) -> Path | None:
    name = environ.get("WAYLAND_DISPLAY")
    if not name:
        return None
    path = Path(name)
    if not path.is_absolute():
        runtime = environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            return None
        path = Path(runtime) / name
    return path if path.exists() else None


def xauthority_file(environ: dict[str, str]) -> Path | None:
    candidate = environ.get("XAUTHORITY") or str(Path(environ.get("HOME", "~")).expanduser() / ".Xauthority")
    path = Path(candidate)
    return path if path.is_file() else None


def detect_display(environ: dict[str, str] | None = None, root: Path = Path("/")) -> list[str]:
    """Human-readable list of the displays a sandbox could use."""
    env = dict(os.environ if environ is None else environ)
    found = []
    display = env.get("DISPLAY", "")
    if display and x11_socket(display, root):
        found.append(f"X11 {display}")
    elif display:
        found.append(f"X11 {display} (TCP: only with network = \"host\")")
    if wayland_socket(env):
        found.append(f"Wayland {env['WAYLAND_DISPLAY']}")
    return found


def display_access(network: str, environ: dict[str, str] | None = None,
                   root: Path = Path("/")) -> DisplayAccess:
    """Apptainer binds and variables giving a sandbox access to the host display."""
    env = dict(os.environ if environ is None else environ)
    access = DisplayAccess()

    display = env.get("DISPLAY", "")
    if display:
        socket = x11_socket(display, root)
        if socket:
            access.binds.append(f"{socket.parent}:{X11_DIR}:ro")
            access.env["DISPLAY"] = display
            access.found.append(f"X11 {display}")
        elif network == "host":
            access.env["DISPLAY"] = display  # X11 over TCP (ssh -X...)
            access.found.append(f"X11 {display}")
        else:
            access.notes.append(f"DISPLAY={display} is not a local X11 socket: it needs network = \"host\"")
        if "DISPLAY" in access.env:
            auth = xauthority_file(env)
            if auth:
                access.binds.append(f"{auth}:{X11_AUTH_TARGET}:ro")
                access.env["XAUTHORITY"] = X11_AUTH_TARGET

    socket = wayland_socket(env)
    if socket:
        # A private, empty XDG_RUNTIME_DIR holding only the Wayland socket: the
        # host runtime directory also holds D-Bus, keyring, ssh/gpg agents...
        runtime = Path(tempfile.mkdtemp(prefix="rebox-runtime-"))
        runtime.chmod(0o700)
        (runtime / "wayland-0").touch()
        access.temp_dirs.append(runtime)
        access.binds += [f"{runtime}:{RUNTIME_TARGET}:rw", f"{socket}:{RUNTIME_TARGET}/wayland-0:rw"]
        access.env.update({"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": RUNTIME_TARGET})
        access.found.append(f"Wayland {env['WAYLAND_DISPLAY']}")

    if not access.found and not access.notes:
        access.notes.append("display = true but no display was found (DISPLAY / WAYLAND_DISPLAY unset)")
    return access


# --------------------------------------------------------------------------- #
# GPU
# --------------------------------------------------------------------------- #


def detect_gpus(root: Path = Path("/")) -> list[str]:
    """GPU kinds usable from a sandbox, most specific first."""
    dev = root / "dev"
    found = []
    if (dev / "nvidiactl").exists() or (root / "proc" / "driver" / "nvidia").exists():
        found.append("nvidia")
    if (dev / "kfd").exists():
        found.append("amd")
    if any((dev / "dri").glob("renderD*")) if (dev / "dri").is_dir() else False:
        found.append("dri")
    return found


def gpu_args(setting: bool | str, root: Path = Path("/")) -> tuple[list[str], list[str]]:
    """Apptainer options for the ``gpu`` resource, and notes for the user."""
    if setting is False:
        return [], []
    detected = detect_gpus(root)
    if setting is True:
        if not detected:
            return [], ["gpu = true but no GPU was found on this host"]
        kinds = detected
    else:
        kinds = [setting]
        if setting not in detected:
            return [], [f'gpu = "{setting}" but no such GPU was found on this host']
    args: list[str] = []
    if "nvidia" in kinds:
        args.append("--nv")  # driver libraries, nvidia-smi and /dev/nvidia*
    if "amd" in kinds:
        args.append("--rocm")  # ROCm libraries, /dev/kfd and /dev/dri
    elif "dri" in kinds and (root / "dev" / "dri").is_dir():
        args += ["--bind", "/dev/dri"]  # render nodes (Intel/AMD/Mesa acceleration)
    return args, []
