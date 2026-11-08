"""The scope and region that the workshop scripts require (#808), as the tests give them: the
first fleet's, which the tests' fixtures name. A test that gives its own keeps it."""

from collections.abc import Callable, Sequence
from typing import Any

TAG, REGION, NAME = "workshop=nt-agents-2026", "us-west-2", "zab-workshop-test"
FLEET_SCOPE = {"workshop": "nt-agents-2026"}


def scoped(
    argv: Sequence[str], *, tag: bool = True, region: bool = True, name: bool = False
) -> list[str]:
    """`argv`, with `--tag`, `--region` (and `--name`) added where it has none of its own."""
    out = list(argv)
    for wanted, option, value in (
        (tag, "--tag", TAG),
        (region, "--region", REGION),
        (name, "--name", NAME),
    ):
        if wanted and option not in out:
            out += [option, value]
    return out


def with_scope(main: Callable[..., Any], argv: Sequence[str], *args: Any, **kw: Any) -> Any:
    """A script's `main`, given the scope and the region."""
    return main(scoped(argv), *args, **kw)


def with_tag(main: Callable[..., Any], argv: Sequence[str], *args: Any, **kw: Any) -> Any:
    """A script's `main` whose region is optional or absent: given the scope only."""
    return main(scoped(argv, region=False), *args, **kw)


def with_build(main: Callable[..., Any], argv: Sequence[str], *args: Any, **kw: Any) -> Any:
    """`build_ami.main`: the scope, the region and the image's name."""
    return main(scoped(argv, name="--smoke-only" not in argv), *args, **kw)
