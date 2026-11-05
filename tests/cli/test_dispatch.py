"""The dispatcher contract of `zab dev` and the workshop tools (SPEC §13.1)."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zab import __version__, cli

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    ("argv", "module", "forwarded"),
    [(["dev", name, "--help"], module, ["--help"]) for name, module in cli.DEV_COMMANDS.items()]
    + [
        ([name, "--some-flag", "a b"], module, ["--some-flag", "a b"])
        for name, module in cli.COMMANDS.items()
    ],
)
def test_dispatch_preserves_arguments_and_exit_code(monkeypatch, argv, module, forwarded):
    hook = Mock(return_value=7)
    importer = Mock(return_value=SimpleNamespace(main=hook))
    monkeypatch.setattr(cli.importlib, "import_module", importer)
    assert cli.main(argv) == 7
    importer.assert_called_once_with(module)
    hook.assert_called_once_with(forwarded)


@pytest.mark.parametrize("argv", [[], ["dev"], ["version"], ["--help"], ["--version"]])
def test_help_and_version_need_no_backend(monkeypatch, capsys, argv):
    importer = Mock(side_effect=AssertionError("must not import"))
    monkeypatch.setattr(cli.importlib, "import_module", importer)
    if argv and argv[0].startswith("--"):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 0
    else:
        assert cli.main(argv) == 0
    output = capsys.readouterr().out
    assert __version__ in output if "version" in " ".join(argv) else "usage:" in output


def test_missing_hook_is_actionable(monkeypatch, capsys):
    def missing(name):
        raise ModuleNotFoundError(name="zab.tools")

    monkeypatch.setattr(cli.importlib, "import_module", missing)
    assert cli.main(["preflight"]) == 2
    assert "zab.tools.preflight is not installed yet" in capsys.readouterr().err


def test_missing_dependency_is_not_hidden(monkeypatch):
    def broken(name):
        raise ModuleNotFoundError(name="missing_dependency")

    monkeypatch.setattr(cli.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        cli.main(["preflight"])


@pytest.mark.parametrize("argv", [["bogus"], ["dev", "bogus"]])
def test_unknown_command(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2


def _runnable(module: str) -> bool:
    """A hook module that also runs as `python -m` (an `if __name__ == "__main__":` block)."""
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin is not None
    return 'if __name__ == "__main__":' in Path(spec.origin).read_text()


@pytest.mark.parametrize(
    "module",
    [m for m in [*cli.COMMANDS.values(), *cli.DEV_COMMANDS.values()] if _runnable(m)],
)
def test_python_m_passes_its_arguments(module):
    """#621: `python -m zab.commands.init` called `main()` without its argv and crashed."""
    result = subprocess.run(  # noqa: S603 - this interpreter, a module of cli.py's tables
        [sys.executable, "-m", module, "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage:")


def test_init_and_doctor_run_as_python_m():
    runnable = [m for m in cli.COMMANDS.values() if _runnable(m)]
    assert {"zab.commands.init", "zab.commands.doctor"} <= set(runnable)
