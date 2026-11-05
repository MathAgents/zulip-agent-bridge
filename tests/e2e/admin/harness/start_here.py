"""What `!provision zulip` gives a fresh sandbox (#709), for A9's "Start here" part and A20.

The expected texts are the product's own, from this checkout's `zulip_provision.py` (the commit
under test, which the rig's server runs): `start_here_text` and `channel_description`, with the
rig's wording, `admin_toml.GIFTS_STEP` and `REFSERVE_URL`, as `zab-admin-e2e`'s entry has them.
The plan's `zulip_provision.py` command must carry both, as the bot passes them: the file server
as `--refserve-url`, the gift step as `--gifts-step-b64`, which decodes to it. The post's
rendering must keep the file host in code (Zulip would link a bare host to the server's root,
which answers `Not found`) and link each bot's name to a DM with it.

A sandbox posts "Start here" once, so a fresh one is needed each run: A9's throwaway row's, made
held by `!provision zulip hold` and released here, rather than `zab-test-a`'s emptied. Whether a
bot may delete or move its own older messages depends on the organization's settings, which
the rig doesn't change (AGENTS §5.3).
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import sys
from collections.abc import Mapping
from types import ModuleType
from urllib.parse import urlsplit

from e2e.admin.harness import admin_toml
from e2e.harness.config import CHECKOUT

TOPIC = "Start here"
SCRIPT = "admin/zulip_provision.py"


def provision() -> ModuleType:
    """This checkout's `workshop/admin/zulip_provision.py`, as a module of its own name, with
    its directory's imports (`admin_common`)."""
    name = "ae2e_zulip_provision"
    if name in sys.modules:
        return sys.modules[name]
    folder = str(CHECKOUT / "workshop" / "admin")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location(name, CHECKOUT / "workshop" / SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def wording() -> object:
    """`zab-admin-e2e`'s wording, as its `admin.toml` entry gives it to the script."""
    zp = provision()
    return zp.Wording(gifts=admin_toml.GIFTS_STEP, refserve_url=admin_toml.REFSERVE_URL)


def bot_ids(made: Mapping[str, int], slug: str) -> dict[str, int]:
    """`{"claude": id, "codex": id}` from the row's bots by full name (`Claude · <slug>`)."""
    return {kind: int(made[f"{kind.capitalize()} · {slug}"]) for kind in ("claude", "codex")}


def expected_post(slug: str, ids: Mapping[str, int]) -> str:
    """The "Start here" the row's Claude bot must have posted: its repository is the rig's
    `<org>/<slug>`, as the scenarios' rows name it."""
    zp = provision()
    return zp.start_here_text(slug, f"{admin_toml.GITHUB_ORG}/{slug}", dict(ids), wording())


def expected_description(slug: str, ids: Mapping[str, int]) -> str:
    return provision().channel_description(slug, dict(ids), wording())


def shown_line() -> str:
    """The dry run's line that shows the plan's approvers the gift step (#709)."""
    return provision().GIFTS_SHOWN.format(admin_toml.GIFTS_STEP)


def flag_problems(command: str) -> list[str]:
    """What is wrong with a plan's `zulip_provision.py` command (as `planning._step` shows it),
    for the rig's `zab-admin-e2e`: its file server and its gift step, as the bot passes them."""
    argv = shlex.split(command)
    if not any(word.endswith(f"/{SCRIPT}") for word in argv):
        return [f"not a {SCRIPT} command: {command[:120]}"]

    def values(flag: str) -> list[str]:
        return [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == flag]

    problems = []
    if values("--refserve-url") != [admin_toml.REFSERVE_URL]:
        problems.append(f"--refserve-url: {values('--refserve-url')}")
    if "--gifts-step" in argv:
        problems.append("--gifts-step: the bot passes the text base64url-encoded only")
    found = values("--gifts-step-b64")
    if len(found) != 1:
        problems.append(f"--gifts-step-b64: {len(found)} values")
    else:
        try:
            text = provision().decode_gifts_step(found[0])
        except ValueError as exc:
            problems.append(f"--gifts-step-b64: {exc}")
        else:
            if text != admin_toml.GIFTS_STEP:
                problems.append(f"--gifts-step-b64 decodes to {text!r}")
    return problems


def rendered_problems(html: str, ids: Mapping[str, int]) -> list[str]:
    """What is wrong with the post as Zulip rendered it (`apply_markdown`): the file host in
    code and not linked; each bot's name a link to a DM with it (`#narrow/dm/<id>-…`)."""
    host = urlsplit(admin_toml.REFSERVE_URL).netloc
    problems = []
    if f"<code>{host}</code>" not in html:
        problems.append(f"no <code>{host}</code>")
    if re.search(rf'href="https?://{re.escape(host)}/?"', html):
        problems.append(f"{host} is linked to the server's root")
    for kind, uid in ids.items():
        if not re.search(rf'href="[^"]*#narrow/dm/{int(uid)}-[^"]*"', html):
            problems.append(f"no link to a DM with the {kind} bot ({uid})")
    return problems
