"""A9's "Start here" readers and A20's flag check (#709), offline, against what the product
itself writes: the rig's admin.toml under the strict loader, `!provision zulip` and
`!adopt-spare` as `actions.build` makes them from it and the plan shows them, and the texts of
`zulip_provision.py`. Zulip's rendering can't run here: `rendered_problems` reads a rendering
written as Zulip's Markdown writes a code span and a link."""

from __future__ import annotations

import base64
import dataclasses

from admin.test_admin_actions import FLEET, layout, make
from e2e.admin import fakes
from e2e.admin.harness import admin_toml, start_here
from e2e.admin.harness.rig import E2E, E2E2
from zab.admin import planning
from zab.admin.config import parse_admin_config

__all__ = ["layout"]  # the fixture, used by name
SLUG = "zab-test-tmp-6a3f1c2b"  # `ending.throwaway`'s form
IDS = {"claude": 4_000_001, "codex": 4_000_002}


def rig_fleet():
    """FLEET's rows and records, in the rig's `zab-admin-e2e`, as the rig's admin.toml has it."""
    ws = parse_admin_config(fakes.test_toml()).workshop(E2E.name)
    return dataclasses.replace(FLEET, workshop=ws, others=())


def shown(action) -> list[str]:
    return [planning._step(step.argv, step.env) for step in action.steps]


def test_the_rigs_admin_toml_has_the_gift_step():
    config = parse_admin_config(fakes.test_toml())
    assert config.workshop(E2E.name).gifts_step == admin_toml.GIFTS_STEP
    assert config.workshop(E2E2.name).gifts_step is None  # the release's default there
    assert not admin_toml.GIFTS_STEP.isascii() and "](https://" in admin_toml.GIFTS_STEP
    assert start_here.provision().gifts_step_ok(admin_toml.GIFTS_STEP)


def test_the_plans_commands_carry_the_file_server_and_the_gift_step(layout):
    """A9's `!provision zulip <row>` and A20's `!adopt-spare` (its step 2, held)."""
    fleet = rig_fleet()
    (alone,) = shown(make("!provision zulip carol", layout, fleet))
    assert start_here.flag_problems(alone) == []
    held = dataclasses.replace(fleet, held=True)  # as the rig's fleet always is (A20)
    adopt = shown(make("!adopt-spare carol zab-spare-1", layout, held))
    assert "--hold" in adopt[2].split() and start_here.flag_problems(adopt[2]) == []
    # each difference is found
    other = base64.urlsafe_b64encode(b"Accept any gifts.").decode()
    b64 = alone.split("--gifts-step-b64 ", 1)[1].split()[0]
    for broken, why in (
        (alone.replace(" --refserve-url https://files.mathagents.org", ""), "--refserve-url"),
        (alone.replace(b64, other), "decodes to"),
        (alone.replace(b64, b64.rstrip("=") + "A"), "--gifts-step-b64: not"),
        (alone.replace(f" --gifts-step-b64 {b64}", ""), "0 values"),
        (alone + " --gifts-step x", "base64url-encoded only"),
        ("python /x/admin/push_config.py", "not a admin/zulip_provision.py command"),
    ):
        assert any(why in p for p in start_here.flag_problems(broken)), (why, broken[-80:])
    # zab-admin-e2e2 has no gift step: no flag, and the check says so
    ws2 = parse_admin_config(fakes.test_toml()).workshop(E2E2.name)
    (default,) = shown(
        make("!provision zulip carol", layout, dataclasses.replace(fleet, workshop=ws2))
    )
    assert "--gifts-step-b64" not in default
    assert start_here.flag_problems(default) == ["--gifts-step-b64: 0 values"]


def test_the_expected_texts_are_the_products_with_the_rigs_wording():
    zp = start_here.provision()
    post = start_here.expected_post(SLUG, IDS)
    assert post == zp.start_here_text(SLUG, f"MathAgents/{SLUG}", IDS, start_here.wording())
    assert f"\n1. {admin_toml.GIFTS_STEP} Sign in to Claude and ChatGPT" in post
    assert "Links to `files.mathagents.org` in the agents' answers" in post
    assert f"(#narrow/dm/{IDS['claude']}-Claude-.C2.B7-{SLUG})" in post
    description = start_here.expected_description(SLUG, IDS)
    assert admin_toml.GIFTS_STEP in description and len(description) < 1024
    assert start_here.TOPIC == zp.START_TOPIC
    assert start_here.shown_line() == zp.GIFTS_SHOWN.format(admin_toml.GIFTS_STEP)
    made = {f"Claude · {SLUG}": IDS["claude"], f"Codex · {SLUG}": IDS["codex"]}
    assert start_here.bot_ids(made, SLUG) == IDS


def test_the_rendering_keeps_the_host_in_code_and_links_the_bots():
    good = (
        f'<p>3. <strong>Claude:</strong> click <strong><a href="#narrow/dm/{IDS["claude"]}-'
        f'Claude-.C2.B7-{SLUG}">Claude · {SLUG}</a></strong> to open …</p>\n'
        f'<p>4. … <a href="#narrow/dm/{IDS["codex"]}-Codex-.C2.B7-{SLUG}">Codex · {SLUG}</a></p>'
        "\n<p>Links to <code>files.mathagents.org</code> in the agents' answers …</p>"
    )
    assert start_here.rendered_problems(good, IDS) == []
    linked = good.replace(
        "<code>files.mathagents.org</code>",
        '<a href="https://files.mathagents.org">files.mathagents.org</a>',
    )
    assert start_here.rendered_problems(linked, IDS) == [
        "no <code>files.mathagents.org</code>",
        "files.mathagents.org is linked to the server's root",
    ]
    unlinked = good.replace(f'href="#narrow/dm/{IDS["codex"]}-', 'href="#narrow/channel/1-')
    assert start_here.rendered_problems(unlinked, IDS) == [
        f"no link to a DM with the codex bot ({IDS['codex']})"
    ]
