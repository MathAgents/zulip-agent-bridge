"""`[jobs]` (#655; SPEC §5.2, §7.14): the limits of background jobs, per machine."""

from pathlib import Path

import pytest

from zab.config import ConfigError, JobsConfig, load_config

pytestmark = pytest.mark.contract
EXAMPLE = Path(__file__).parents[2] / "config/zab.example.toml"


def jobs(tmp_path, body: str) -> JobsConfig:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return load_config(path).jobs


def test_the_defaults_are_the_answered_ones(tmp_path):
    """D44: a report turn on by default, 12 hours per job, 4 jobs and 75% of the memory per
    machine, and no limit per topic."""
    assert jobs(tmp_path, "") == JobsConfig(
        report=True, max_running=4, max_hours=12.0, max_memory_percent=75
    )
    assert load_config(EXAMPLE).jobs == JobsConfig()
    assert {"max_per_session", "max_per_topic"}.isdisjoint(vars(JobsConfig()))


def test_each_key_is_read(tmp_path):
    found = jobs(
        tmp_path,
        "[jobs]\nreport = false\nmax_running = 0\nmax_hours = 0.05\nmax_memory_percent = 50\n",
    )
    assert found == JobsConfig(report=False, max_running=0, max_hours=0.05, max_memory_percent=50)


def test_whole_hours_are_hours(tmp_path):
    found = jobs(tmp_path, "[jobs]\nmax_hours = 24\n")
    assert found.max_hours == 24.0 and isinstance(found.max_hours, float)


@pytest.mark.parametrize(
    "line,key",
    [
        ("max_running = -1", "jobs.max_running"),
        ("max_running = 65", "jobs.max_running"),
        ("max_running = 4.0", "jobs.max_running"),
        ("max_hours = 0", "jobs.max_hours"),
        ("max_hours = -2", "jobs.max_hours"),
        ("max_hours = 168.5", "jobs.max_hours"),
        ("max_hours = nan", "jobs.max_hours"),
        ("max_hours = 1" + "0" * 400, "jobs.max_hours"),  # too large for a float
        ("max_hours = true", "jobs.max_hours"),
        ('max_hours = "12"', "jobs.max_hours"),
        ("max_memory_percent = 9", "jobs.max_memory_percent"),
        ("max_memory_percent = 91", "jobs.max_memory_percent"),
        ("max_memory_percent = 75.0", "jobs.max_memory_percent"),
        ('report = "yes"', "jobs.report"),
        ("max_per_session = 2", "jobs.max_per_session"),
    ],
)
def test_a_value_out_of_range_names_its_key(tmp_path, line, key):
    with pytest.raises(ConfigError, match=key.replace(".", r"\.")):
        jobs(tmp_path, f"[jobs]\n{line}\n")


@pytest.mark.parametrize(
    "line",
    ["max_running = 64", "max_hours = 168", "max_memory_percent = 10", "max_memory_percent = 90"],
)
def test_the_bounds_themselves_are_allowed(tmp_path, line):
    jobs(tmp_path, f"[jobs]\n{line}\n")
