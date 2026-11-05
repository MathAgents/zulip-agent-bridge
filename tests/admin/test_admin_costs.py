"""The cost estimate (docs/ADMIN.md §11.2, `zab.admin.costs`): a recorded state history, priced
by hand."""

import calendar

import pytest

from zab.admin import costs
from zab.admin.costs import FIXED_HOUR, History, Seen, dollars, month_end, month_start


def t(day: int, hour: int = 0, minute: int = 0, month: int = 10) -> float:
    return float(calendar.timegm((2026, month, day, hour, minute, 0)))


NOW = t(20, 12)  # Tue Oct 20, 12:00 UTC
M, RG = "m7a.2xlarge", "us-west-2"
PRICE = costs.PRICES[RG, M]


def test_the_fixed_cost_is_the_volume_and_the_address():
    # 200 GB of gp3 at $0.08 a GB-month over 730 hours, and $0.005 an hour for the address:
    # about $20 a month (§11.2)
    assert FIXED_HOUR == pytest.approx(0.0269178, abs=1e-7)
    assert 730 * FIXED_HOUR == pytest.approx(19.65)


def test_the_month_and_the_day_are_utc():
    assert month_start(NOW) == t(1)
    assert month_end(NOW) == t(1, month=11)
    assert month_end(t(15, month=12)) == calendar.timegm((2027, 1, 1, 0, 0, 0))
    assert costs.day_start(NOW) == t(20)


def history_of_one_vm() -> History:
    """Created Oct 10; ran Oct 19 10:00 to 14:00; running again since Oct 20 08:00. Sampled every
    minute while the collector watched; it saw nothing before Oct 19."""
    history = History()
    vm = {"type": "m7a.2xlarge", "created": t(10)}
    for minute in range(0, 24 * 60 + 12 * 60, 1):  # Oct 19 00:00 to Oct 20 12:00
        now = t(19) + 60 * minute
        if t(19, 10) <= now <= t(19, 14):
            vm |= {"state": "running", "launched": t(19, 10)}
        elif now >= t(20, 8):
            vm |= {"state": "running", "launched": t(20, 8)}
        else:
            vm |= {"state": "stopped", "launched": t(19, 10)}
        history.observe("i-1", "harvard", "alice", vm, now)
    history.observe("i-1", "harvard", "alice", vm, NOW)
    return history


def test_a_recorded_history_matches_a_hand_computation():
    history = history_of_one_vm()
    seen = history.seen["i-1"]
    assert seen.runs == [[t(19, 10), t(19, 14), M], [t(20, 8), NOW, M]]
    assert seen.created == t(10)
    cost = seen.cost(NOW, present=True, region=RG)
    assert cost is not None
    # This month: 8 running hours at $0.46368 = $3.70944; the volume and the address from
    # Oct 10 00:00 to now, 252 hours at $0.02691781 = $6.78329. Together $10.49.
    assert cost.month == 10.49
    # Projected: 276 hours are left (to Nov 1 00:00). The last 7 days (since Oct 13 12:00, 168
    # hours) it ran 8: a share of 0.047619. So 276 * (0.02691781 + 0.047619 * 0.46368)
    # = 276 * 0.04899781 = $13.52339 more: $24.02.
    assert cost.projected == 24.02
    assert (cost.hours_today, cost.hours_month) == (4.0, 8.0)
    totals = history.workshop("harvard", {"i-1"}, NOW, RG)
    assert totals == {"month": 10.49, "projected": 24.02, "hours_today": 4.0, "unpriced": [],
                      "unpriced_region": None}  # fmt: skip


@pytest.mark.parametrize("region", costs.REGIONS)
def test_the_three_regions_have_the_same_prices(region):
    """#499: the table holds us-east-1, us-east-2 and us-west-2, whose prices AWS's price list
    gave alike on Oct 23, 2026; the history above prices the same in each."""
    assert {kind: price for (where, kind), price in costs.PRICES.items() if where == region} == {
        "m7a.xlarge": 0.23184, "m7a.2xlarge": 0.46368, "m7a.4xlarge": 0.92736,
        "r7a.xlarge": 0.3043, "r7a.2xlarge": 0.6086, "t3.medium": 0.0416,
    }  # fmt: skip
    cost = history_of_one_vm().seen["i-1"].cost(NOW, True, region)
    assert (cost.month, cost.projected) == (10.49, 24.02)


def test_a_workshop_in_another_region_is_unpriced_not_priced_as_us():
    """#499: on-demand prices differ by region (eu-west-1's are 10 to 12% higher), and so do the
    volume's. A workshop in a region outside the table gets no dollars, per VM or in total,
    and its totals name the region; the hours still count."""
    history = history_of_one_vm()
    cost = history.seen["i-1"].cost(NOW, True, "eu-west-1")
    assert (cost.month, cost.projected, cost.unpriced) == (None, None, ())
    assert (cost.hours_today, cost.hours_month) == (4.0, 8.0)
    totals = history.workshop("harvard", {"i-1"}, NOW, "eu-west-1")
    assert totals == {"month": None, "projected": None, "hours_today": 4.0, "unpriced": [],
                      "unpriced_region": "eu-west-1"}  # fmt: skip
    stopped = Seen("harvard", "f", M, created=t(20), last=NOW)  # no run: its volume alone
    assert stopped.cost(NOW, True, "eu-west-1").month is None


def test_a_vm_no_longer_seen_projects_nothing_more():
    history = history_of_one_vm()
    cost = history.seen["i-1"].cost(NOW + 3600, present=False, region=RG)
    # nothing after its last sighting: the same as at NOW, an hour's running cost aside
    assert cost is not None and cost.month == 10.49 and cost.projected == cost.month


def test_only_this_months_part_counts():
    seen = Seen("harvard", "bob", "m7a.2xlarge", created=t(25, month=9), last=NOW)
    seen.runs = [[t(30, 22, month=9), t(1, 2), M]]  # across the month's start: 2 of 4 hours
    cost = seen.cost(NOW, present=True, region=RG)
    assert cost is not None and cost.hours_month == 2.0
    # 2 running hours, and the fixed cost from Oct 1, 468 hours
    assert cost.month == round(2 * PRICE + 468 * FIXED_HOUR, 2) == 13.52


def test_a_young_vm_projects_at_its_current_state():
    running = Seen("harvard", "c", "m7a.2xlarge", created=NOW - 600, last=NOW)
    running.runs = [[NOW - 600, NOW, M]]
    stopped = Seen("harvard", "d", "m7a.2xlarge", created=NOW - 600, last=NOW)
    left = (month_end(NOW) - NOW) / 3600
    up, down = running.cost(NOW, True, RG), stopped.cost(NOW, True, RG)
    assert up is not None and down is not None
    assert up.projected == pytest.approx(up.month + left * (FIXED_HOUR + PRICE), abs=0.01)
    assert down.projected == pytest.approx(down.month + left * FIXED_HOUR, abs=0.01)


def test_a_type_without_a_price_is_named_not_guessed():
    history = History()
    history.observe("i-9", "harvard", "eve", {"type": "x9.huge", "state": "running",
                                              "launched": NOW - 60}, NOW)  # fmt: skip
    cost = history.seen["i-9"].cost(NOW, True, RG)
    assert (cost.month, cost.projected, cost.unpriced) == (None, None, ("x9.huge",))
    assert history.workshop("harvard", {"i-9"}, NOW, RG)["unpriced"] == ["x9.huge"]


def test_a_run_without_a_price_is_named_and_its_hours_still_count():
    """Codex on #490: an hour as an unlisted type, then a resize to a priced one: the type
    named is the run's, and the hour counts in the running hours."""
    history = History()
    vm = {"type": "x9.huge", "state": "running", "launched": t(20, 10), "created": t(20, 10)}
    history.observe("i-9", "harvard", "eve", vm, t(20, 11))
    history.observe("i-9", "harvard", "eve", vm | {"type": M, "state": "stopped"}, NOW)
    cost = history.seen["i-9"].cost(NOW, True, RG)
    assert (cost.month, cost.unpriced, cost.hours_today, cost.hours_month) == (
        None, ("x9.huge",), 1.0, 1.0)  # fmt: skip
    totals = history.workshop("harvard", {"i-9"}, NOW, RG)
    assert totals == {"month": 0, "projected": 0, "hours_today": 1.0, "unpriced": ["x9.huge"],
                      "unpriced_region": None}  # fmt: skip
    # gone, with only priced runs, a VM whose type now has no price is priced in full
    gone = Seen("harvard", "f", "x9.huge", created=t(20), last=t(20, 2))
    assert gone.cost(NOW, present=False, region=RG).unpriced == ()


def test_a_restart_and_a_new_launch_time():
    history = History()
    vm = {"type": "m7a.2xlarge", "state": "running", "launched": t(20, 9)}
    history.observe("i-1", "harvard", "alice", vm, t(20, 10))
    # read back after a restart of the collector: the run goes on from where it was
    history = History.of(history.to_json())
    history.observe("i-1", "harvard", "alice", vm, t(20, 11))
    assert history.seen["i-1"].runs == [[t(20, 9), t(20, 11), M]]
    # a stop and a start the collector didn't see: the first run ends where it last saw it
    vm["launched"] = t(20, 11, 30)
    history.observe("i-1", "harvard", "alice", vm, NOW)
    assert history.seen["i-1"].runs == [[t(20, 9), t(20, 11), M], [t(20, 11, 30), NOW, M]]
    # a launch time before the last run's end (a clock set back) is not a new run
    vm["launched"] = t(20, 10)
    history.observe("i-1", "harvard", "alice", vm, NOW + 60)
    assert len(history.seen["i-1"].runs) == 2


def test_the_history_reads_back_or_drops_an_entry():
    history = history_of_one_vm()
    data = history.to_json()
    assert History.of(data).to_json() == data
    entry = {"workshop": "harvard", "slug": "x", "type": "m7a.2xlarge", "created": 1, "last": 2}
    data["i-2"] = entry | {"runs": [[5, 3, M]]}  # a run that ends before it starts
    data["i-5"] = entry | {"runs": [[1, 2]]}  # a run without its type
    data["i-3"] = {"workshop": "harvard", "slug": "x", "type": 7, "created": 1, "last": 2,
                   "runs": []}  # fmt: skip
    data["i-4"] = "not an entry"
    assert set(History.of(data).seen) == {"i-1"}
    assert History.of(None).seen == {} and History.of([1, 2]).seen == {}


def test_prune_forgets_what_no_estimate_needs():
    history = history_of_one_vm()
    old = Seen("harvard", "z", "m7a.2xlarge", created=t(1, month=8), last=t(2, month=8))
    old.runs = [[t(1, month=8), t(2, month=8), M]]
    history.seen["i-old"] = old
    history.seen["i-1"].runs.insert(0, [t(1, month=9), t(2, month=9), M])  # before the window
    history.prune(NOW)
    assert "i-old" not in history.seen
    assert history.seen["i-1"].runs[0] == [t(19, 10), t(19, 14), M]


def test_a_vm_moved_to_another_workshop_starts_afresh():
    history = history_of_one_vm()
    history.observe("i-1", "mit", "alice", {"type": "m7a.2xlarge", "state": "stopped"}, NOW)
    seen = history.seen["i-1"]
    assert (seen.workshop, seen.runs, seen.created) == ("mit", [], NOW)
    assert history.workshop("harvard", set(), NOW, RG)["month"] == 0


def test_dollars():
    assert dollars(1050.4) == "$1,050" and dollars(0) == "$0" and dollars(412.6) == "$413"


def test_a_resize_reprices_only_what_follows_it():
    """Codex on #490: 100 hours as m7a.2xlarge, then stopped and resized to m7a.4xlarge, cost
    what they cost; the projection is at the new type's price."""
    history = History()
    vm = {"type": M, "state": "running", "launched": t(10), "created": t(10)}
    history.observe("i-1", "harvard", "alice", vm, t(10))
    history.observe("i-1", "harvard", "alice", vm, t(14, 4))  # 100 hours
    vm["state"] = "stopped"
    history.observe("i-1", "harvard", "alice", vm, t(14, 5))
    before = history.seen["i-1"].cost(t(14, 5), present=True, region=RG)
    vm["type"] = "m7a.4xlarge"
    history.observe("i-1", "harvard", "alice", vm, t(14, 5))
    after = history.seen["i-1"].cost(t(14, 5), present=True, region=RG)
    assert before is not None and after is not None
    # 100 hours at $0.46368, and 101 hours of the volume and the address: $49.09
    assert after.month == before.month == round(100 * PRICE + 101 * FIXED_HOUR, 2) == 49.09
    assert after.projected > before.projected  # what is left, at the larger type's price
    vm |= {"state": "running", "launched": t(14, 6)}
    history.observe("i-1", "harvard", "alice", vm, t(14, 7))
    assert history.seen["i-1"].runs[-1] == [t(14, 6), t(14, 7), "m7a.4xlarge"]
    later = history.seen["i-1"].cost(t(14, 7), present=True, region=RG)
    assert later is not None
    assert later.month == round(100 * PRICE + costs.PRICES[RG, "m7a.4xlarge"] + 103 * FIXED_HOUR, 2)


def stopped_since(created: float) -> dict:
    return {"type": M, "state": "stopped", "launched": created, "created": created}


def test_a_volume_is_priced_at_the_size_read_from_its_creation():
    """#553: the first size EC2 shows counts from the VM's creation; until one is read, and
    without one, 200 GB."""
    history = History()
    history.observe("i-1", "harvard", "alice", stopped_since(t(10)) | {"volume_gb": 300}, NOW)
    history.observe("i-2", "harvard", "bob", stopped_since(t(10)), NOW)
    big, plain = history.seen["i-1"], history.seen["i-2"]
    assert big.volumes == [[t(10), 300]] and plain.volumes == [] and plain.volume_gb == 200
    hours = 10 * 24 + 12  # Oct 10 00:00 to Oct 20 12:00
    assert big.cost(NOW, True, RG).month == round(hours * costs.fixed_hour(300), 2)
    assert plain.cost(NOW, True, RG).month == round(hours * FIXED_HOUR, 2)
    left = (month_end(NOW) - NOW) / 3600
    projected = big.cost(NOW, True, RG).projected
    assert projected == pytest.approx(
        round(hours * costs.fixed_hour(300), 2) + left * costs.fixed_hour(300), abs=0.01
    )
    assert costs.fixed_hour(200) == FIXED_HOUR


def test_a_grown_volume_reprices_only_what_follows_its_read():
    history = History()
    vm = stopped_since(t(10)) | {"volume_gb": 200}
    history.observe("i-1", "harvard", "alice", vm, t(15))
    history.observe("i-1", "harvard", "alice", vm | {"volume_gb": 500}, t(18))
    history.observe("i-1", "harvard", "alice", vm | {"volume_gb": 500}, NOW)
    seen = history.seen["i-1"]
    assert seen.volumes == [[t(10), 200], [t(18), 500]] and seen.volume_gb == 500
    month = 8 * 24 * costs.fixed_hour(200) + (2 * 24 + 12) * costs.fixed_hour(500)
    assert seen.cost(NOW, True, RG).month == round(month, 2)
    for odd in (None, 0, -5, 2.5, True, "300", 70000):  # not a size EC2 gives: ignored
        history.observe("i-1", "harvard", "alice", vm | {"volume_gb": odd}, NOW)
    assert seen.volumes == [[t(10), 200], [t(18), 500]]


def test_volume_sizes_read_back_and_are_pruned():
    history = History()
    vm = stopped_since(t(10, month=8)) | {"volume_gb": 200}
    history.observe("i-1", "harvard", "alice", vm, t(10, month=8))
    history.observe("i-1", "harvard", "alice", vm | {"volume_gb": 300}, t(20, month=8))
    history.observe("i-1", "harvard", "alice", vm | {"volume_gb": 400}, t(5))
    again = History.of(history.to_json())
    assert again.seen["i-1"].volumes == history.seen["i-1"].volumes
    old = history.to_json()["i-1"]
    assert Seen.of({k: v for k, v in old.items() if k != "volumes"}).volumes == []  # pre-#553
    for bad in ([[1, 0]], [[1]], [["x", 3]], [[5, 3], [1, 4]], {"a": 1}):
        assert Seen.of(old | {"volumes": bad}) is None
    history.prune(NOW)  # the 300 GB of Aug 20 is still in force at the month's start
    assert history.seen["i-1"].volumes == [[t(20, month=8), 300], [t(5), 400]]
