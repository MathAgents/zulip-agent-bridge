import argparse
from types import SimpleNamespace

import pytest

from zab.core.creation import clone_project, create_session, new_options

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("text", ["!new --unknown", "!new --model"])
async def test_bad_options_raise_an_error_not_system_exit(text):
    with pytest.raises((ValueError, argparse.ArgumentError)) as exc:
        await create_session(SimpleNamespace(), {}, text, {})
    assert not isinstance(exc.value, AttributeError)


async def test_unmapped_channel_explains_configuration_fix():
    bridge = SimpleNamespace(
        config=SimpleNamespace(projects=[], bridge=SimpleNamespace(default_project=""))
    )
    with pytest.raises(ValueError, match="projects"):
        await create_session(bridge, {"stream_id": 1}, "prompt", {"name": "unknown"})


@pytest.mark.parametrize(
    "prompt",
    ["don't change this", '"unterminated', "first\n  second", "# heading\n```py\nx = 'q'\n```"],
)
def test_new_preserves_opaque_prompt(prompt):
    args, text = new_options('!new --model "" ' + prompt)
    assert args.model == ""
    assert text == prompt


async def test_auto_clone_is_opt_in_and_uses_argument_vector(tmp_path):
    from unittest.mock import AsyncMock

    ws = SimpleNamespace(
        root=str(tmp_path),
        cwd=str(tmp_path),
        run=AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout="", stderr="")),
    )
    bridge = SimpleNamespace(
        config=SimpleNamespace(
            bridge=SimpleNamespace(auto_clone=False, projects_root=str(tmp_path))
        ),
        backend=SimpleNamespace(workspace=lambda root, cwd: ws),
    )
    channel = {"description": "project\nrepo: owner/repo\n"}
    assert await clone_project(bridge, channel) is None
    ws.run.assert_not_called()
    bridge.config.bridge.auto_clone = True
    project = await clone_project(bridge, channel)
    assert project.path == str(tmp_path / "repo")
    assert ws.run.call_args.args[0][-3:] == [
        "--",
        "https://github.com/owner/repo.git",
        project.path,
    ]
    assert await clone_project(bridge, {"description": "repo: ../escape"}) is None


async def test_auto_clone_rejects_wrong_existing_origin(tmp_path):
    from zab.workspace.local import LocalWorkspace

    path = tmp_path / "app"
    path.mkdir()
    ws = LocalWorkspace(str(path))
    await ws.run(["git", "init", "-q"])
    await ws.run(["git", "remote", "add", "origin", "https://github.com/alice/app.git"])
    bridge = SimpleNamespace(
        config=SimpleNamespace(
            bridge=SimpleNamespace(auto_clone=True, projects_root=str(tmp_path))
        ),
        backend=SimpleNamespace(workspace=lambda root, cwd: LocalWorkspace(root, cwd)),
    )
    with pytest.raises(ValueError, match="does not match"):
        await clone_project(bridge, {"description": "repo: bob/app"})
    assert (await clone_project(bridge, {"description": "repo: alice/app"})).path == str(path)
