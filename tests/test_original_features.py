"""Features carried over from Ubuntu Builder 2.x (no root, no network)."""

from pathlib import Path

import pytest
from ubuntu_builder import cli
from ubuntu_builder.core import customizer, graphical
from ubuntu_builder.core.runner import CommandRunner
from ubuntu_builder.models import CustomizationConfig, Workspace


def make_customizer(tmp_path: Path) -> tuple[customizer.Customizer, Workspace]:
    workspace = Workspace(tmp_path / "ws")
    workspace.iso_dir.mkdir(parents=True)
    return customizer.Customizer(workspace, CommandRunner()), workspace


@pytest.mark.parametrize(
    ("given", "expected"),
    [("id_ID", "id_ID.UTF-8"), ("id_ID.UTF-8", "id_ID.UTF-8"), ("pt_BR", "pt_BR.UTF-8"), ("C.UTF-8", None)],
)
def test_normalize_locale(given, expected):
    if expected is None:
        with pytest.raises(customizer.CustomizationError):
            customizer.normalize_locale(given)
    else:
        assert customizer.normalize_locale(given) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"description": 'Say "hi"'},
        {"description": "two\nlines"},
        {"welcome_message": "bell\x07"},
        {"locale": "../../etc"},
        {"local_debs": ["/nonexistent/pkg.deb"]},
    ],
)
def test_distribution_fields_are_validated(tmp_path, changes):
    tool, _ = make_customizer(tmp_path)
    with pytest.raises(customizer.CustomizationError):
        tool.validate(CustomizationConfig(**changes))


def test_local_debs_must_exist_and_be_debs(tmp_path):
    tool, _ = make_customizer(tmp_path)
    deb = tmp_path / "tool_1.0_all.deb"
    deb.write_bytes(b"!<arch>\n")
    tool.validate(CustomizationConfig(local_debs=[str(deb)]))
    text = tmp_path / "notes.txt"
    text.write_text("x")
    with pytest.raises(customizer.CustomizationError):
        tool.validate(CustomizationConfig(local_debs=[str(text)]))


def test_remove_windows_files(tmp_path):
    tool, workspace = make_customizer(tmp_path)
    for name in ("autorun.inf", "wubi.exe", "md5sum.txt"):
        (workspace.iso_dir / name).write_text("x")
    (workspace.iso_dir / "pics").mkdir()
    (workspace.iso_dir / "pics" / "a.png").write_text("x")
    tool._remove_windows_files()
    assert sorted(p.name for p in workspace.iso_dir.iterdir()) == ["md5sum.txt"]


def test_welcome_message_and_description(tmp_path):
    tool, _ = make_customizer(tmp_path)
    rootfs = tmp_path / "root"
    (rootfs / "etc/update-motd.d").mkdir(parents=True)
    (rootfs / "usr/lib").mkdir(parents=True)
    (rootfs / "usr/lib/os-release").write_text('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 26.04 LTS"\n')
    (rootfs / "etc/lsb-release").write_text("DISTRIB_ID=Ubuntu\nDISTRIB_DESCRIPTION=\"Ubuntu 26.04 LTS\"\n")
    tool._apply_welcome("Selamat datang!", rootfs)
    tool._apply_description("Nusantara OS 26.04", rootfs)
    assert (rootfs / customizer.WELCOME_FILE).read_text() == "Selamat datang!\n"
    script = rootfs / "etc/update-motd.d/10-help-text"
    assert script.stat().st_mode & 0o111 and customizer.WELCOME_FILE in script.read_text()
    assert 'PRETTY_NAME="Nusantara OS 26.04"' in (rootfs / "usr/lib/os-release").read_text()
    assert 'DISTRIB_DESCRIPTION="Nusantara OS 26.04"' in (rootfs / "etc/lsb-release").read_text()


def test_config_json_round_trip_with_new_fields():
    config = CustomizationConfig(
        distro_name="Nusantara OS", locale="id_ID.UTF-8", welcome_message="Halo", remove_windows_files=True,
        local_debs=["/tmp/a.deb"], commands="apt-get install -y htop\n",
    )  # fmt: skip
    assert CustomizationConfig.from_dict(config.to_dict()) == config
    assert not config.is_empty() and CustomizationConfig().is_empty()


def test_desktop_session_detection(tmp_path):
    assert graphical.find_desktop_session(tmp_path) is None
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin/openbox-session").write_text("")
    assert graphical.find_desktop_session(tmp_path) == ("openbox-session",)
    (tmp_path / "usr/bin/xfce4-session").write_text("")
    assert graphical.find_desktop_session(tmp_path) == ("xfce4-session",)  # preferred over openbox


def test_only_local_displays_are_accepted():
    assert graphical._display_number(":0") == "0"
    assert graphical._display_number(":1.0") == "1"
    with pytest.raises(graphical.GraphicalError):
        graphical._display_number("remote.example:0")


def test_helper_accepts_only_whitelisted_variables(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    cli._apply_setenv(["DISPLAY=:1"])
    import os

    assert os.environ["DISPLAY"] == ":1"
    for bad in (["LD_PRELOAD=/tmp/x.so"], ["PYTHONPATH=/tmp"], ["DISPLAY"]):
        with pytest.raises(SystemExit):
            cli._apply_setenv(bad)


def test_cli_parser_knows_the_original_commands():
    parser = cli.build_parser()
    for argv in (["synaptic"], ["desktop", "--resolution", "1024x768"], ["build", "x.iso", "--config", "c.json"]):
        assert parser.parse_args(argv).command == argv[0]
