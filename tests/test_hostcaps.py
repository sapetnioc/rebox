"""Tests of display and GPU access (fake host filesystems, no Apptainer)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from rebox import hostcaps
from rebox import run as run_mod
from rebox.config import ConfigError, Project, load_env
from rebox.hostcaps import X11_AUTH_TARGET, display_access, detect_display, gpu_args, x11_socket
from rebox.init import defaults_for_host
from rebox.run import build_command


def unix_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))
    return sock


@pytest.fixture()
def fake_root(tmp_path: Path):
    root = tmp_path / "root"
    sockets = [unix_socket(root / "tmp" / ".X11-unix" / "X0")]
    yield root
    for s in sockets:
        s.close()


def test_x11_socket_parsing(fake_root):
    assert x11_socket(":0", fake_root) == fake_root / "tmp/.X11-unix/X0"
    assert x11_socket(":0.1", fake_root) and x11_socket("unix:0", fake_root)
    assert x11_socket(":1", fake_root) is None
    assert x11_socket("localhost:10.0", fake_root) is None


def test_x11_access(fake_root, tmp_path):
    auth = tmp_path / "xauth"
    auth.write_text("cookie")
    access = display_access("allowlist", {"DISPLAY": ":0", "XAUTHORITY": str(auth)}, fake_root)
    assert access.binds == [f"{fake_root}/tmp/.X11-unix:/tmp/.X11-unix:ro", f"{auth}:{X11_AUTH_TARGET}:ro"]
    assert access.env == {"DISPLAY": ":0", "XAUTHORITY": X11_AUTH_TARGET}
    assert access.found == ["X11 :0"] and not access.notes


def test_x11_over_tcp_needs_host_network(fake_root):
    env = {"DISPLAY": "localhost:10.0", "HOME": "/nonexistent"}
    assert display_access("host", env, fake_root).env == {"DISPLAY": "localhost:10.0"}
    access = display_access("allowlist", env, fake_root)
    assert access.env == {} and "network = \"host\"" in access.notes[0]


def test_wayland_access_uses_a_private_runtime_dir(tmp_path):
    runtime = tmp_path / "run-user"
    sock = unix_socket(runtime / "wayland-1")
    (runtime / "bus").write_text("dbus socket must not be exposed")
    try:
        access = display_access("none", {"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": str(runtime)})
    finally:
        sock.close()
    private = access.temp_dirs[0]
    assert access.binds == [f"{private}:/run/rebox-runtime:rw",
                            f"{runtime}/wayland-1:/run/rebox-runtime/wayland-0:rw"]
    assert access.env == {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/rebox-runtime"}
    assert sorted(p.name for p in private.iterdir()) == ["wayland-0"]
    assert oct(private.stat().st_mode & 0o777) == "0o700"


def test_no_display():
    access = display_access("none", {})
    assert access.binds == [] and "no display was found" in access.notes[0]
    assert detect_display({}) == []


def make_gpu_root(tmp_path: Path, *devices: str) -> Path:
    root = tmp_path / "gpu-root"
    for device in devices:
        (root / "dev" / device).parent.mkdir(parents=True, exist_ok=True)
        (root / "dev" / device).touch()
    (root / "dev").mkdir(parents=True, exist_ok=True)
    return root


@pytest.mark.parametrize("devices,setting,expected", [
    ((), False, []),
    (("nvidiactl", "dri/renderD128"), True, ["--nv", "--bind", "/dev/dri"]),
    (("nvidiactl",), True, ["--nv"]),
    (("kfd", "dri/renderD128"), True, ["--rocm"]),
    (("dri/renderD128",), True, ["--bind", "/dev/dri"]),
    (("nvidiactl", "dri/renderD128"), "nvidia", ["--nv"]),
    (("dri/renderD128",), "dri", ["--bind", "/dev/dri"]),
])
def test_gpu_args(tmp_path, devices, setting, expected):
    args, notes = gpu_args(setting, make_gpu_root(tmp_path, *devices))
    assert args == expected and not notes


def test_gpu_requested_but_missing(tmp_path):
    root = make_gpu_root(tmp_path)
    assert gpu_args(True, root) == ([], ["gpu = true but no GPU was found on this host"])
    assert gpu_args("amd", root)[1] == ['gpu = "amd" but no such GPU was found on this host']


def test_init_enables_what_it_finds(monkeypatch):
    monkeypatch.setattr(hostcaps, "detect_display", lambda: ["X11 :0"])
    monkeypatch.setattr(hostcaps, "detect_gpus", lambda: [])
    notes: list[str] = []
    text = defaults_for_host(notes=notes)
    assert "\ndisplay = true  # found by rebox init: X11 :0\n" in text
    assert "\ngpu = false\n" in text
    assert notes == ["display access: on (X11 :0)", "GPU access: off (no GPU found)"]
    forced = defaults_for_host(display=False, gpu=True)
    assert "\ndisplay = false\n" in forced and "\ngpu = true  # forced by rebox init\n" in forced


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.setattr(run_mod, "apptainer_bin", lambda: "apptainer")
    monkeypatch.setattr(run_mod, "user_systemd_available", lambda: False)
    (tmp_path / "recipes" / "base").mkdir(parents=True)
    (tmp_path / "recipes" / "base" / "1.0.0.toml").write_text('post = "true"\n')
    (tmp_path / "rebox-defaults.toml").write_text(
        '[sources.local]\npath = "recipes"\n[build]\nbase_image = "docker://debian:12"\n'
        'default_recipes = ["base"]\n[resources]\nnetwork = "allowlist"\ndisplay = true\n'
        '[resources.env]\nDISPLAY = "${DISPLAY:-:9}"\n')
    (tmp_path / "envs" / "e").mkdir(parents=True)
    (tmp_path / "envs" / "e" / "rebox.toml").write_text("")
    return Project(tmp_path)


def test_display_resource_in_command(project, monkeypatch, fake_root):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/nonexistent")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    real = hostcaps.display_access
    monkeypatch.setattr(hostcaps, "display_access", lambda network: real(network, None, fake_root))
    argv = build_command(load_env(project, "e"), ["xterm"]).argv
    assert f"{fake_root}/tmp/.X11-unix:/tmp/.X11-unix:ro" in argv
    assert argv.count("DISPLAY=:0") == 1 and not any(a == "DISPLAY=:9" for a in argv)
    assert argv[argv.index("--network") + 1] == "none"  # allowlist: no direct network


def test_invalid_values(project):
    defaults = project.root / "rebox-defaults.toml"
    defaults.write_text(defaults.read_text().replace("display = true", 'display = "yes"'))
    with pytest.raises(ConfigError, match="display must be true or false"):
        load_env(project, "e")
    defaults.write_text(defaults.read_text().replace('display = "yes"', 'gpu = "intel"'))
    with pytest.raises(ConfigError, match="resources.gpu must be"):
        load_env(project, "e")
