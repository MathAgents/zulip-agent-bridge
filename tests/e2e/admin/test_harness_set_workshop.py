"""A27's set-up, offline (#1043): its start puts e2e2's image to a placeholder and takes its gift
step out, that entry's lines alone, in a file the loader takes; and it is listed as stage 7's."""

from __future__ import annotations

import dataclasses

from e2e.admin import test_scenarios_8_set_workshop as a27
from e2e.admin.fakes import test_toml
from e2e.admin.harness import rig as rigs
from zab.admin.config import parse_admin_config


def test_the_start_changes_e2e2s_image_and_gift_step_alone():
    rig = test_toml()
    head, tail = rig.rsplit("\n[workshop.bot]", 1)  # e2e2's, the last entry
    with_gifts = f'{head}\ngifts_step = "Gifts."\n[workshop.bot]{tail}'
    for toml in (rig, with_gifts):
        start = a27.with_ami(toml, rigs.E2E2.name, a27.PLACEHOLDER)
        before, after = parse_admin_config(toml), parse_admin_config(start)
        assert after.workshops[0] == before.workshops[0]  # e2e's, its gift step included
        was = before.workshop(rigs.E2E2.name)
        aws = dataclasses.replace(was.aws, ami=a27.PLACEHOLDER)
        assert after.workshop(rigs.E2E2.name) == dataclasses.replace(was, aws=aws, gifts_step=None)
        assert a27.entry_of(start, rigs.E2E2.name)["aws"]["ami"] == a27.PLACEHOLDER


def test_a27_is_stage_7s_and_selectable():
    """ADMIN §8.1: a scenario is listed under the stage it needs; A27 needs `!new-workshop`'s
    config helper, stage 7, as A16 does. Not a go criterion (§8.2)."""
    assert rigs.label(27) == "A27" and rigs.SCENARIOS[27][2] == rigs.SCENARIOS[16][2] == 7
    assert rigs.select("A27") == [27] and 27 in rigs.select("all") and 27 not in rigs.GO
