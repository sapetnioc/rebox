"""Unit tests for rebox (no Apptainer needed)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rebox import run as run_mod
from rebox.build import render_def
from rebox.config import (
    ConfigError,
    Project,
    deep_merge,
    expand_host_vars,
    load_env,
    parse_size_mb,
)
from rebox.export import export_env, resolved_sandbox_toml
from rebox.run import build_command


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.setattr(run_mod, "apptainer_bin", lambda: "apptainer")
    monkeypatch.setattr(run_mod, "user_systemd_available", lambda: False)
    (tmp_path / "data").mkdir()
    write(tmp_path / "defaults.toml", f"""
        [build]
        base_image = "docker://debian:12-slim"
        default_recipes = ["base"]

        [resources]
        network = "none"
        cpus = 2
        memory = "4G"

        [resources.env]
        TERM = "${{TERM:-dumb}}"

        [resources.binds.data]
        source = "{tmp_path / 'data'}"
        target = "/data"
    """)
    write(tmp_path / "recipes" / "base" / "1.0.0.toml", """
        description = "base"
        post = '''
        echo base
        cat > /etc/demo <<EOF
        hello
        EOF
        '''
        [environment]
        LANG = "C.UTF-8"
    """)
    write(tmp_path / "recipes" / "tool" / "1.0.0.toml", """
        description = "tool"
        requires = ["base"]
        post = "echo tool $TOOL_VERSION"
        [args]
        TOOL_VERSION = "1.0"
    """)
    write(tmp_path / "recipes" / "agent" / "1.0.0.toml", """
        description = "agent"
        requires = ["tool"]
        post = "echo agent"
    """)
    write(tmp_path / "envs" / "demo" / "sandbox.toml", """
        description = "demo"
        [build]
        recipes = ["agent"]
        [build.recipe_args.tool]
        TOOL_VERSION = "2.0"
        [resources]
        network = "host"
        memory = "1G"
        [resources.binds.data]
        mode = "rw"
    """)
    return Project(tmp_path)


def test_deep_merge_overrides_nested_values():
    merged = deep_merge({"a": {"b": 1, "c": [1]}, "d": 1}, {"a": {"c": [2]}, "e": 3})
    assert merged == {"a": {"b": 1, "c": [2]}, "d": 1, "e": 3}


def test_expand_host_vars():
    assert expand_host_vars("${A}-${B:-x}", {"A": "1"}) == ("1-x", [])
    assert expand_host_vars("${MISSING}", {}) == ("", ["MISSING"])


@pytest.mark.parametrize("value,expected", [("4G", 4096), ("512M", 512), ("1.5G", 1536), (2048, 2048), ("", None)])
def test_parse_size(value, expected):
    assert parse_size_mb(value) == expected


def test_recipes_resolved_in_dependency_order(project):
    env = load_env(project, "demo")
    assert [r.name for r in env.recipes] == ["base", "tool", "agent"]
    assert env.recipe_args["tool"] == {"TOOL_VERSION": "2.0"}


def test_resources_merge_defaults(project):
    env = load_env(project, "demo")
    res = env.resources
    assert res.network == "host"          # overridden
    assert res.cpus == 2                  # inherited
    assert res.memory_mb == 1024          # overridden
    assert [(b.name, b.mode) for b in res.binds] == [("data", "rw")]
    assert "TERM" in res.env


def test_disable_default_bind_and_inheritance(project):
    path = project.env_dir("demo") / "sandbox.toml"
    path.write_text(path.read_text().replace('mode = "rw"', "enabled = false"))
    assert load_env(project, "demo").resources.binds == []

    write(project.env_dir("bare") / "sandbox.toml", """
        inherit_defaults = false
        [build]
        base_image = "docker://alpine:3"
        recipes = ["base"]
    """)
    env = load_env(project, "bare")
    assert env.resources.binds == [] and env.resources.cpus is None


def test_unknown_recipe_and_cycle(project):
    write(project.env_dir("bad") / "sandbox.toml", '[build]\nrecipes = ["nope"]\n')
    with pytest.raises(ConfigError, match="Unknown recipe 'nope'"):
        load_env(project, "bad")
    write(project.recipes_dir / "base" / "1.0.0.toml", 'requires = ["agent"]\npost = "true"\n')
    with pytest.raises(ConfigError, match="cycle"):
        load_env(project, "demo")


def test_unknown_keys_are_rejected(project):
    write(project.env_dir("typo") / "sandbox.toml", "[resources]\nnetwrok = 'host'\n")
    with pytest.raises(ConfigError, match="unknown keys"):
        load_env(project, "typo")


def test_render_def(project):
    definition = render_def(load_env(project, "demo"))
    assert "Bootstrap: docker\nFrom: debian:12-slim" in definition
    assert (definition.index("recipe: builtin:base@1.0.0") < definition.index("recipe: builtin:tool@1.0.0")
            < definition.index("recipe: builtin:agent@1.0.0"))
    assert "export TOOL_VERSION=2.0" in definition
    assert 'export LANG="C.UTF-8"' in definition
    assert "\nEOF\n" in definition  # heredoc terminators are not indented


def test_build_command(project):
    env = load_env(project, "demo")
    argv = build_command(env, ["bash"]).argv
    assert argv[:4] == ["apptainer", "--quiet", "exec", "--containall"]
    assert "--network" not in argv  # network = host
    assert f"{env.workspace}:/sandbox:rw" in argv
    assert any(a.endswith(":/data:rw") for a in argv)
    assert "--memory" not in argv  # no systemd user session -> limits not enforced (auto)
    assert argv[-2:] == [str(env.image), "bash"]

    env.resources.network = "none"
    env.resources.limits_backend = "apptainer"
    argv = build_command(env, ["bash"]).argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--memory") + 1] == "1024M"


def test_export_is_standalone(project, tmp_path):
    env = load_env(project, "demo")
    data = resolved_sandbox_toml(env)
    assert data["inherit_defaults"] is False
    assert data["build"]["recipes"] == ["builtin:base@==1.0.0", "builtin:tool@==1.0.0", "builtin:agent@==1.0.0"]
    assert data["sources"] == {"builtin": {"path": "recipes/builtin"}}

    out = export_env(env, tmp_path / "export")
    exported = load_env(Project(out), "demo")
    assert render_def(exported) == render_def(env)
    assert exported.resources.memory_mb == env.resources.memory_mb


def test_allowlist_network(project):
    path = project.env_dir("demo") / "sandbox.toml"
    path.write_text(path.read_text().replace('network = "host"', 'network = "allowlist"') + textwrap.dedent("""
        [resources.allow.github]
        domains = ["github.com", "*.github.com"]
        [resources.allow.off]
        enabled = false
        domains = ["example.org"]
    """))
    env = load_env(project, "demo")
    assert [r.name for r in env.resources.allow] == ["github"]
    assert env.resources.allow[0].ports == [80, 443]
    cmd = build_command(env, ["bash"])
    argv = cmd.argv
    assert argv[argv.index("--network") + 1] == "none"
    assert "HTTPS_PROXY=http://127.0.0.1:3128" in argv
    assert any(a.endswith(":/run/sandbox-net:rw") for a in argv)
    assert argv[-5:-3] == ["/bin/sh", "-c"] and argv[-1] == "bash"
    assert cmd.proxy is not None and cmd.proxy.match("api.github.com", 443)
    assert cmd.proxy.match("example.org", 443) is None


def test_invalid_allow_domain(project):
    write(project.env_dir("bad") / "sandbox.toml", """
        [resources.allow.x]
        domains = ["exa mple.com"]
    """)
    with pytest.raises(ConfigError, match="invalid domain"):
        load_env(project, "bad")
