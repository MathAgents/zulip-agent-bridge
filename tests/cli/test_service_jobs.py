"""`zab-jobs.slice` (#655; SPEC §7.14, §13.3): `zab service install` writes it, with the memory
share of `[jobs]`, and never enables, starts or restarts it, which would stop every running job;
`uninstall` stops it, and the jobs with it, then removes it."""

import shutil
import subprocess

import pytest
from test_service import setup as setup

from zab.commands import service
from zab.config import BridgeConfig, Config, JobsConfig, RenderConfig

pytestmark = pytest.mark.integration
SLICE = "zab-jobs.slice"


def slice_text(percent: int) -> str:
    return (
        "# Managed by zab service install\n"
        "[Unit]\nDescription=zab background jobs\n"
        f"[Slice]\nMemoryMax={percent}%\nMemorySwapMax=0\nCPUWeight=50\nTasksMax=4096\n"
    )


def test_the_slice_holds_the_jobs_memory_share(tmp_path):
    config = Config(tmp_path / "config.toml", BridgeConfig(), [], {}, [], RenderConfig())
    assert service.units(config)[SLICE] == slice_text(75)
    config.jobs = JobsConfig(max_memory_percent=50)
    assert service.units(config)[SLICE] == slice_text(50)


@pytest.mark.parametrize("percent", [10, 75, 90])
def test_a_job_past_the_share_is_killed_not_swapped(tmp_path, percent):
    """The #655 probe on the test VM, which has 8 GB of swap: with `MemoryMax` alone a job past
    it swapped and ran on; with `MemorySwapMax=0` beside it, it was oom-killed in 0.3 s. So the
    slice allows no swap, whatever the share."""
    config = Config(tmp_path / "config.toml", BridgeConfig(), [], {}, [], RenderConfig())
    config.jobs = JobsConfig(max_memory_percent=percent)
    lines = service.units(config)[SLICE].splitlines()
    assert f"MemoryMax={percent}%" in lines and "MemorySwapMax=0" in lines


def test_install_writes_the_slice_and_never_enables_or_starts_it(setup):
    _, control, _ = setup
    assert service.main(["install"]) == 0
    directory = service.unit_directory()
    assert (directory / SLICE).read_text() == slice_text(75)
    assert directory / SLICE in service.managed_units(directory)
    control.assert_any_call("daemon-reload")
    control.assert_any_call("enable", "--now", "--no-block", "zab.service", "zab-sentinel.service")
    assert not any(SLICE in call.args for call in control.call_args_list)


def test_a_new_share_reaches_the_slice_by_the_reload_alone(setup):
    """A running slice is never restarted: that would stop its jobs. The manager's reload
    hands it the new limit (VERIFY #655 V2)."""
    config, control, run = setup
    assert service.main(["install"]) == 0
    control.reset_mock()
    run.return_value.returncode = 0  # every unit active, the slice included
    config.jobs = JobsConfig(max_memory_percent=60)
    assert service.main(["install"]) == 0
    assert (service.unit_directory() / SLICE).read_text() == slice_text(60)
    names = [call.args[0] for call in control.call_args_list]
    assert "daemon-reload" in names
    assert not any(SLICE in call.args for call in control.call_args_list)
    assert not any(SLICE in call.args[0] for call in run.call_args_list)  # no is-active either


def test_status_leaves_the_idle_slice_out(setup, capsys):
    """`systemctl --user status` exits 3 when any unit it names is inactive (systemd 255), and
    the slice is inactive until a job runs: named, it would fail a healthy machine's status."""
    _, _, run = setup
    assert service.main(["install"]) == 0
    assert service.unit_directory() / SLICE in service.managed_units(service.unit_directory())
    run.reset_mock()
    run.return_value.returncode = 0
    assert service.main(["status"]) == 0
    (argv,) = (call.args[0] for call in run.call_args_list)
    assert argv[1:4] == ["--user", "status", "--no-pager"]
    assert argv[4:] == ["zab-sentinel.service", "zab.service"]


def test_status_with_only_the_slice_left_shows_the_bridge(setup):
    _, _, run = setup
    directory = service.unit_directory()
    directory.mkdir(parents=True)
    (directory / SLICE).write_text(slice_text(75))
    run.reset_mock()
    service.main(["status"])
    (argv,) = (call.args[0] for call in run.call_args_list)
    assert argv[4:] == ["zab.service"]


def test_uninstall_stops_the_slice_and_its_jobs_then_removes_it(setup):
    _, control, _ = setup
    assert service.main(["install"]) == 0
    control.reset_mock()
    assert service.main(["uninstall"]) == 0
    assert not (service.unit_directory() / SLICE).exists()
    control.assert_any_call("stop", SLICE)
    control.assert_any_call("disable", "--now", "zab.service")
    assert not any(
        call.args[:1] == ("disable",) and SLICE in call.args for call in control.call_args_list
    )
    assert control.call_args_list[-1].args == ("daemon-reload",)
    # the slice last: while the bridge stops, a turn's `zab job start` would start it again,
    # from no file and with no limit, and that job would outlive the uninstall
    calls = [call.args for call in control.call_args_list]
    stop = calls.index(("stop", SLICE))
    assert stop > calls.index(("disable", "--now", "zab.service"))
    assert stop > calls.index(("disable", "--now", "zab-sentinel.service"))


def test_a_failed_slice_stop_is_said_and_leaves_the_services_stopped(setup, capsys):
    _, control, _ = setup
    assert service.main(["install"]) == 0
    control.reset_mock()

    def control_(*args):
        if args == ("stop", SLICE):
            raise subprocess.TimeoutExpired(["systemctl"], 45)
        return mock_result

    mock_result = control.return_value
    control.side_effect = control_
    assert service.main(["uninstall"]) == 1
    control.assert_any_call("disable", "--now", "zab.service")
    control.assert_any_call("disable", "--now", "zab-sentinel.service")
    assert control.call_args_list[-1].args == ("daemon-reload",)
    # the services are gone; the slice's file stays, so its jobs keep their limits and the
    # next uninstall stops them (review round 2 of #930)
    directory = service.unit_directory()
    assert [p.name for p in directory.iterdir()] == [SLICE]
    err = capsys.readouterr().err
    assert "the services are stopped, but stopping zab-jobs.slice failed (TimeoutExpired" in err
    assert err.rstrip().endswith("systemctl --user stop zab-jobs.slice")
    control.reset_mock()
    control.side_effect = None
    assert service.main(["uninstall"]) == 0  # the retry
    assert control.call_args_list[0].args == ("stop", SLICE)
    assert list(directory.iterdir()) == []


def test_an_unmanaged_slice_is_neither_overwritten_nor_removed(setup):
    _, control, _ = setup
    directory = service.unit_directory()
    directory.mkdir(parents=True)
    (directory / SLICE).write_text("# mine\n")
    assert service.main(["install"]) == 1
    control.assert_not_called()
    assert service.main(["uninstall"]) == 0
    assert (directory / SLICE).read_text() == "# mine\n"
    assert not any(SLICE in call.args for call in control.call_args_list)


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_systemd_reads_every_line_of_the_slice(tmp_path):
    """systemd ignores an unknown key with a warning, and `verify` still exits 0: so no line of
    its output may name the file."""
    path = tmp_path / SLICE
    path.write_text(slice_text(75))
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["systemd-analyze", "--user", "verify", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert not [line for line in (result.stdout + result.stderr).splitlines() if str(path) in line]
