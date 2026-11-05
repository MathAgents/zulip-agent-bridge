"""`!rollout`'s defaults (#704): without `lean-shared`, the shared Mathlib where a VM's isn't
shared yet; without `idle-stop=`, idle stop at 60 minutes, on the rows whose waker is on alone,
on the VM the waker names. `!adopt-spare` gives a late joiner's VM the same, when the fleet's
record leaves them out."""

import dataclasses

import pytest

from zab.admin.actions import (
    IDLE_STOP_DEFAULT,
    Live,
    build,
    digests,
    plan_hash,
    waker_rows,
)
from zab.admin.fleet import ApplyEnd, Ledger

from .test_admin_actions import FLEET, OUT, ROLL, WHL, command

#: Instance IDs, as EC2 writes them: alice's VM, bob's, carol's, dave's, and a broken one
A, B, C, D, X = (f"i-0{c * 16}" for c in "abcde")
#: The waker's config (`push_waker.py --config-dir`): alice and dave opted in; bob has one bot
#: only, a config cut short by hand; carol's two bots name two VMs; carol is opted in under
#: another workshop too, which is not this one's.
CONFIG = f"""\
starter = "/run/zab-admin-starter/start.sock"

[[workshop]]
name = "harvard-2026"
organizers_channel = 44
[[workshop.bots]]
name = "codex-alice"
instance_id = "{A}"
[[workshop.bots]]
name = "claude-alice"
instance_id = "{A}"
[[workshop.bots]]
name = "codex-bob"
instance_id = "{B}"
[[workshop.bots]]
name = "codex-carol"
instance_id = "{C}"
[[workshop.bots]]
name = "claude-carol"
instance_id = "{X}"
[[workshop.bots]]
name = "claude-dave"
instance_id = "{D}"
[[workshop.bots]]
name = "codex-dave"
instance_id = "{D}"

[[workshop]]
name = "zab-test"
organizers_channel = 45
[[workshop.bots]]
name = "codex-carol"
instance_id = "{C}"
[[workshop.bots]]
name = "claude-carol"
instance_id = "{C}"
"""
DEFAULTS = f"--lean-shared-default --idle-stop-default {IDLE_STOP_DEFAULT}"
IDLE_NOTE = (
    "**Idle stop: 60 min (default)** where the waker is on for the VM and no timer is on, "
    "unless it was turned off on purpose (`!idle-stop 0`); a timer that is on stays as it is. "
    "Each row's result says what it found."
)


@pytest.fixture
def layout(tmp_path):
    from zab.admin.actions import Layout

    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "zulip_agent_bridge-0.2.2-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "waker").mkdir()
    return Layout(
        wheels=str(wheels), retired=str(tmp_path / "r.txt"), waker=str(tmp_path / "waker")
    )


def waker(layout, text: str | None = CONFIG) -> None:
    path = f"{layout.waker}/config.toml"
    with open(path, "w") as f:
        f.write(text or "")


def make(message, layout, fleet=FLEET):
    return build(command(message, fleet), fleet, layout=layout)


def push(layout, middle: str) -> tuple[str, ...]:
    return tuple((ROLL.format(release=WHL.format(W=layout.wheels)) + f" {middle}").split())


# ------------------------------------------------------------------------------- the waker


def test_the_rows_whose_waker_is_on_are_read_from_its_config(layout, tmp_path):
    """Both bots, one instance between them, this workshop's entry."""
    waker(layout)
    path = f"{layout.waker}/config.toml"
    assert waker_rows(path, "harvard-2026") == {"alice": A, "dave": D}
    assert waker_rows(path, "zab-test") == {"carol": C}
    assert waker_rows(str(tmp_path / "none.toml"), "harvard-2026") == {}
    (tmp_path / "bad.toml").write_text("[[workshop]\nname=")
    assert waker_rows(str(tmp_path / "bad.toml"), "harvard-2026") is None
    (tmp_path / "odd.toml").write_text('workshop = "harvard-2026"\n')
    assert waker_rows(str(tmp_path / "odd.toml"), "harvard-2026") == {}
    (tmp_path / "bytes.toml").write_bytes(b"\xff\xfe")
    assert waker_rows(str(tmp_path / "bytes.toml"), "harvard-2026") is None
    # an instance that isn't one never reaches an argument list
    (tmp_path / "id.toml").write_text(CONFIG.replace(A, "i-1 --only=x"))
    assert waker_rows(str(tmp_path / "id.toml"), "harvard-2026") == {"dave": D}
    (tmp_path / "list.toml").write_text(CONFIG.replace(f'"{A}"', f'["{A}"]', 1))
    assert waker_rows(str(tmp_path / "list.toml"), "harvard-2026") == {"dave": D}
    # a config past the bound is never read in part, whatever its first MiB says
    big = CONFIG + "\n".join(f"# {'x' * 100}" for _ in range(11000)) + "\n"
    assert len(big.encode()) > 1024 * 1024
    (tmp_path / "big.toml").write_text(big)
    assert waker_rows(str(tmp_path / "big.toml"), "harvard-2026") is None


# ------------------------------------------------------------------------------ !rollout


def test_a_rollout_without_the_words_applies_both_defaults(layout):
    """Idle stop where the waker is on (alice, dave), each row named with the VM the waker
    names, which push_config.py checks on the VM; the plan says it all."""
    waker(layout)
    action = make("!rollout v0.2.2 all", layout)
    every = "--only alice --only bob --only carol --only dave"
    woken = f"--has-waker alice={A} --has-waker dave={D}"
    assert action.steps[0].argv == push(layout, f"{DEFAULTS} {woken} {every}")
    assert action.notes == (
        "**Shared Mathlib (default):** set up as `lean-shared` does on each VM whose Mathlib "
        "isn't shared yet; a VM that has it is only probed, one with no Lean workspace noted.",
        IDLE_NOTE,
        "Not on bob, carol: the waker isn't on there. The waker first: `!waker on bob carol`, "
        "then this rollout again, or `!idle-stop 60 bob carol`, which turns it on even where "
        "it was off on purpose.",
        "The dry run shows every step the defaults may take, as on a new VM.",
    )


@pytest.mark.parametrize(
    ("words", "middle", "notes"),
    [
        ("lean-shared idle-stop=60", "--lean-shared --idle-stop 60", 0),
        ("lean-shared idle-stop=0", "--lean-shared --idle-stop 0", 0),  # 0 kept: off on purpose
        ("idle-stop=30", "--idle-stop 30 --lean-shared-default", 2),
        (
            "lean-shared",
            f"--lean-shared --idle-stop-default {IDLE_STOP_DEFAULT} --has-waker dave={D}",
            3,
        ),
    ],
)
def test_explicit_words_keep_their_meaning(layout, words, middle, notes):
    waker(layout)
    action = make(f"!rollout v0.2.2 bob dave {words}", layout)
    assert action.steps[0].argv == push(layout, f"{middle} --only bob --only dave")
    assert len(action.notes) == notes


def test_without_any_waker_the_note_names_the_rows(layout):
    """No config, as before any `!waker on`: no row is named. For `all`, `!waker on all`, but
    never `!idle-stop 60 all`, which would turn idle stop on where it was off on purpose."""
    action = make("!rollout v0.2.2 all", layout)
    assert "--has-waker" not in action.steps[0].argv
    assert (
        "Not on alice, bob, carol, dave: the waker isn't on there. The waker first: "
        "`!waker on all`, then this rollout again, or `!idle-stop 60 alice bob carol dave`, "
        "which turns it on even where it was off on purpose."
    ) in action.notes
    many = dataclasses.replace(FLEET, rows=FLEET.rows + tuple(
        dataclasses.replace(FLEET.rows[2], slug=f"p{n}") for n in range(4)))  # fmt: skip
    six = make("!rollout v0.2.2 alice bob carol p0 p1 p2", layout, many)
    assert (
        "Not on alice, bob, carol, p0, p1 and 1 more: the waker isn't on there. The waker "
        "first: `!waker on <row>`, then this rollout again, or `!idle-stop 60 <row>`, which "
        "turns it on even where it was off on purpose."
    ) in six.notes


def test_a_waker_config_that_cant_be_read_turns_idle_stop_on_nowhere(layout):
    waker(layout, "this is not toml [")
    action = make("!rollout v0.2.2 alice", layout)
    assert action.steps[0].argv == push(layout, f"{DEFAULTS} --only alice")
    assert any("the waker's config can't be read" in note for note in action.notes)


def test_a_row_opted_in_or_out_since_the_plan_makes_it_stale(layout):
    """The rows and their VMs are in the argument list, which the hash covers: the apply's
    rebuild differs."""
    waker(layout)
    live = Live(dry_run="dry", tmpdir="/var/lib/zab-admin/tmp/P-1")

    def hashed():
        action = make("!rollout v0.2.2 all", layout)
        return plan_hash(action, release="v1", digests=digests(action), live=live)

    first = hashed()
    assert hashed() == first
    waker(layout, CONFIG.replace('"codex-bob"', '"claude-bob"'))  # bob: still one bot
    assert hashed() == first
    waker(layout, CONFIG.replace('"codex-alice"', '"codex-alicia"'))  # alice: opted out
    assert hashed() != first
    waker(layout, CONFIG.replace(A, X))  # alice: `!waker on alice` again, after a new VM
    assert hashed() != first


# --------------------------------------------------------------------------- !adopt-spare


def adopt(layout, fleet):
    return make("!adopt-spare carol zab-spare-1", layout, fleet)


def test_a_late_joiner_gets_the_defaults_after_the_waker(layout):
    """#704 (6): the fleet's rollouts never named the words; its waker is on: the new VM is
    opted in, then both push_config steps give the shared Mathlib and idle stop at 60. The
    chain's own waker step names the VM it finds, the spare, just before: no instance."""
    action = adopt(layout, dataclasses.replace(FLEET, waker=True, settings=()))
    scripts = [next(w for w in s.argv if w.endswith(".py")).rsplit("/", 1)[1] for s in action.steps]
    assert scripts.index("push_waker.py") < scripts.index("push_config.py")
    tail = f"{DEFAULTS} --has-waker carol"
    assert action.steps[4].argv == push(layout, f"{tail} --adopt-spare carol --only carol")
    assert action.steps[6].argv == push(layout, f"{tail} --only carol")
    assert len(action.notes) == 2 and "Not on" not in " ".join(action.notes)


def test_a_late_joiner_without_the_fleets_waker_gets_no_idle_stop(layout):
    action = adopt(layout, dataclasses.replace(FLEET, waker=False, settings=("steer=off",)))
    assert action.steps[3].argv == push(
        layout, f"--no-claude-steer {DEFAULTS} --adopt-spare carol --only carol"
    )
    # no rollout to send again in this plan (review round 3)
    assert action.notes[-1] == (
        "Not on carol: the waker isn't on there. The waker first: `!waker on carol`, then "
        "`!idle-stop 60 carol`."
    )


def test_a_late_joiner_gets_the_fleets_own_words_where_they_were_given(layout):
    fleet = dataclasses.replace(FLEET, waker=True, settings=("idle-stop=0", "lean-shared"))
    action = adopt(layout, fleet)
    assert action.steps[6].argv == push(layout, "--idle-stop 0 --lean-shared --only carol")
    assert action.notes == ()


def test_the_ledger_of_a_rollout_without_the_words_gives_the_defaults(layout):
    """The ledger records nothing for the words a rollout of `all` left out: `!adopt-spare`
    then gives their defaults, as the rollout did."""
    ledger = Ledger()
    for verb, args in (("provision zulip", ["all"]), ("rollout", ["v0.2.2", "all"]),
                       ("waker", ["on", "all"])):  # fmt: skip
        end = ApplyEnd("harvard-2026", "P-3", verb, tuple(args), 0)
        ledger.observe({"event": "apply_end", **end.fields()})
    state = ledger.workshop("harvard-2026")
    assert state is not None and (state.settings, state.waker) == ((), True)
    fleet = dataclasses.replace(FLEET, settings=state.settings, waker=state.waker)
    action = adopt(layout, fleet)
    assert f"{DEFAULTS} --has-waker carol --only carol" in " ".join(action.steps[6].argv)
    assert OUT in " ".join(action.steps[6].argv)
