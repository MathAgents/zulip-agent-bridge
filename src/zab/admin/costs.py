"""The cost estimate, per VM and per workshop (docs/ADMIN.md §11.2; the collector, §4.1).

What the collector sees of each VM, sampled every minute, is all it prices:
- **its runs:** while EC2 shows it `running`, from its launch time (the last start) to the last
  time the collector saw it running;
- **its volume:** gp3, at the size EC2 shows (`DescribeVolumes`, §5's Read statement since
  #553: every volume attached to the VM, read every `collector.VOLUME_EVERY`), from the VM's
  creation, its volume's attachment, to the last time the collector saw it. A size read is
  priced from its first read on, and the first one from the VM's creation (`Seen.volumes`);
  until one is read, and where the role can't read it, 200 GB, `launch.py`'s root volume;
- **its public IPv4 address,** an Elastic IP, held running or stopped, over the same time.

Priced from `PRICES`, on-demand list prices (those compared with workshop fleets' bill matched
it: VERIFY V13, at `REGIONS` below); the exact figure is AWS's (§11.2). §11.2 puts the table in
admin.toml: that is a change of `config.py`, which is privileged (D32), so it is a constant here
for now (#489). The table holds the regions in `REGIONS`; a workshop in any other region gets no
dollars at all, and says so (`unpriced_region`), rather than US prices, which run low elsewhere
(#499).

- **This month:** the month to date, in UTC.
- **Projected:** that, plus the hours left in the month at the VM's fixed cost and its type's
  price times the share of the last 7 days it ran (of its life, when that is shorter); nothing
  more for a VM no longer seen.
- **Running hours today,** since midnight UTC.

Each run is priced at the type it ran as: a VM resized while stopped is repriced from its
next start only.

**What it doesn't see makes it err low:** a run the collector never saw (a start and a stop
while it was down) is missing, and a run whose end it didn't see ends when it last saw the
VM running. Otherwise it errs either way: the prices are list prices, some not checked against
a bill, and the volume is spread over a 730-hour month, where AWS prorates over the real one,
so a whole month of it reads about 2% high in a 31-day month and 8% low in February (the review
of #490). A volume grown (`!grow-disk`) is priced at its new size from the first read that
shows it, so from up to `collector.VOLUME_EVERY` late; one grown before the collector first
read it is priced at its new size from the VM's creation, high.
"""

from __future__ import annotations

import calendar
import itertools
import math
import time
from dataclasses import dataclass, field
from typing import Any

HOUR = 3600.0
#: The regions `PRICES` holds. Their prices are the same: these instance prices, and gp3's
#: below, as the price lists of AWS's EC2 and EBS pricing pages gave them on Oct 23, 2026
#: (eu-west-1's, for comparison: about 10 to 12% higher, m7a 11.5%, and $0.088 a GB-month),
#: and the address's, $0.005 an hour in every region; the EC2 page's list gave the same
#: instance prices on Nov 7. VERIFY V13 (DECISIONS, 2026-11-07) compared these with workshop
#: fleets' bill for October and Nov 1-6, 2026, and each one compared matched exactly:
#: m7a.2xlarge (us-east-2, us-west-2), m7a.xlarge (us-west-2), gp3 (us-east-2, us-west-2) and
#: the address, idle and in use (us-east-2, us-west-2). The lists' alone, as no VM of those
#: fleets ran them: m7a.xlarge in us-east-2, the other types, and every us-east-1 price.
REGIONS = ("us-east-1", "us-east-2", "us-west-2")
#: On-demand Linux prices in USD an hour, for the fleet's types and the rig's, by (region, type).
_PRICE = {
    "m7a.xlarge": 0.23184,
    "m7a.2xlarge": 0.46368,
    "m7a.4xlarge": 0.92736,
    "r7a.xlarge": 0.3043,
    "r7a.2xlarge": 0.6086,
    "t3.medium": 0.0416,
}
PRICES = {(region, kind): price for region in REGIONS for kind, price in _PRICE.items()}
VOLUME_GB = 200  # launch.py's gp3 root volume: a VM's until a size is read
GP3_GB_MONTH = 0.08
IPV4_HOUR = 0.005  # an Elastic IP, attached or not


def fixed_hour(gb: float) -> float:
    """A VM's volume of `gb` GB and its address, per hour; the volume over a 730-hour month
    (module doc)."""
    return gb * GP3_GB_MONTH / 730 + IPV4_HOUR


#: `fixed_hour` of `launch.py`'s volume: about $20 a month (§11.2).
FIXED_HOUR = fixed_hour(VOLUME_GB)
RECENT = 7 * 24 * HOUR  # the window whose share of running time projects the rest
#: How long a VM's history is kept once it is no longer seen: past any month it counts in.
KEEP = 40 * 24 * HOUR


def month_start(now: float) -> float:
    t = time.gmtime(now)
    return float(calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0)))


def month_end(now: float) -> float:
    t = time.gmtime(now)
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return float(calendar.timegm((year, month, 1, 0, 0, 0)))


def day_start(now: float) -> float:
    t = time.gmtime(now)
    return float(calendar.timegm((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0)))


def _overlap(runs: list[list[Any]], lo: float, hi: float) -> float:
    """Seconds of `runs` within [lo, hi]."""
    return sum(max(0.0, min(run[1], hi) - max(run[0], lo)) for run in runs)


def _priced(runs: list[list[Any]], lo: float, hi: float, region: str) -> tuple[float, set[str]]:
    """USD of `runs` within [lo, hi], each run at its own type's price in `region`, and the
    types of those that count but have no price."""
    total, unpriced = 0.0, set()
    for start, end, kind in runs:
        seconds = max(0.0, min(end, hi) - max(start, lo))
        if seconds and (region, kind) not in PRICES:
            unpriced.add(kind)
        total += seconds * PRICES.get((region, kind), 0.0) / HOUR
    return total, unpriced


@dataclass(slots=True)
class Seen:
    """One instance as the collector has seen it."""

    workshop: str
    slug: str
    type: str  # its type now: a stopped VM can be resized
    created: float  # its volume's attachment, else when it was first seen
    last: float  # the last time it was seen, in any state
    #: [start, end, type], in order: each run at the type it ran as, so that a resize
    #: reprices only what follows it (Codex on #490)
    runs: list[list[Any]] = field(default_factory=list)
    #: [since, GB], in order: its volumes' size from each time on; `VOLUME_GB` before the first
    #: (module doc)
    volumes: list[list[Any]] = field(default_factory=list)

    def observe(
        self, state: str, launched: float | None, now: float, volume_gb: int | None = None
    ) -> None:
        """One sighting: in `state`, last started at `launched`; with its volumes' size, when
        EC2 showed it. The first size counts from the VM's creation, a change from `now`."""
        self.last = max(self.last, now)
        if volume_gb is not None and (not self.volumes or self.volumes[-1][1] != volume_gb):
            self.volumes.append([min(self.created, now) if not self.volumes else now, volume_gb])
        if state != "running" or launched is None or launched > now:
            return
        if self.runs and self.runs[-1][0] == launched:
            self.runs[-1][1] = max(self.runs[-1][1], now)
        elif not self.runs or launched >= self.runs[-1][1]:
            self.runs.append([launched, now, self.type])

    def cost(self, now: float, present: bool, region: str) -> Cost:
        """This VM's estimate at `now`, in its workshop's `region`. Its hours whatever the
        prices; no dollars, and the types named, when a type it ran as this month, or its type
        now for the projection, has no price (Codex on #490); no dollars in a region outside
        `REGIONS`, whose volume and address are unpriced too (#499)."""
        lo, end = month_start(now), month_end(now)
        today = _overlap(self.runs, day_start(now), now) / HOUR
        hours = _overlap(self.runs, lo, now) / HOUR
        if region not in REGIONS:
            return Cost(None, None, round(today, 2), round(hours, 2))
        ran, unpriced = _priced(self.runs, lo, now, region)
        price = PRICES.get((region, self.type))
        if present and price is None:
            unpriced.add(self.type)
        if unpriced:
            return Cost(None, None, round(today, 2), round(hours, 2), tuple(sorted(unpriced)))
        month = ran + self._fixed(max(self.created, lo), min(self.last, now))
        projected = month
        if present and price is not None:
            since = max(self.created, now - RECENT)
            span = now - since
            share = _overlap(self.runs, since, now) / span if span >= HOUR else 0.0
            if span < HOUR and self.runs and self.runs[-1][1] >= now:
                share = 1.0  # too young for a share: as it runs now
            projected += (end - now) * (fixed_hour(self.volume_gb) + share * price) / HOUR
        return Cost(round(month, 2), round(projected, 2), round(today, 2), round(hours, 2))

    @property
    def volume_gb(self) -> float:
        """Its volumes' size now, as last read, or `VOLUME_GB`."""
        return self.volumes[-1][1] if self.volumes else VOLUME_GB

    def _fixed(self, lo: float, hi: float) -> float:
        """USD of its volume and address within [lo, hi], each size from its time on."""
        steps = [[-math.inf, VOLUME_GB], *self.volumes, [math.inf, 0]]
        return sum(
            max(0.0, min(end, hi) - max(start, lo)) * fixed_hour(gb) / HOUR
            for (start, gb), (end, _) in itertools.pairwise(steps)
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "workshop": self.workshop,
            "slug": self.slug,
            "type": self.type,
            "created": self.created,
            "last": self.last,
            "runs": self.runs,
            "volumes": self.volumes,
        }

    @classmethod
    def of(cls, data: Any) -> Seen | None:
        """A history entry read back, or None when it doesn't read back exactly."""
        if not isinstance(data, dict):
            return None
        text = [data.get(k) for k in ("workshop", "slug", "type")]
        times = [data.get(k) for k in ("created", "last")]
        runs = data.get("runs")
        if not all(isinstance(v, str) for v in text) or not all(map(_number, times)):
            return None
        if not isinstance(runs, list) or not all(
            isinstance(r, list) and len(r) == 3 and all(map(_number, r[:2]))
            and r[0] <= r[1] and isinstance(r[2], str)
            for r in runs
        ):  # fmt: skip
            return None
        kept = [[float(a), float(b), kind] for a, b, kind in runs]
        volumes = data.get("volumes", [])  # none in a history before #553
        if not isinstance(volumes, list) or not all(
            isinstance(v, list) and len(v) == 2 and _number(v[0]) and _number(v[1]) and v[1] > 0
            for v in volumes
        ) or [v[0] for v in volumes] != sorted(v[0] for v in volumes):  # fmt: skip
            return None
        sizes = [[float(since), gb] for since, gb in volumes]
        return cls(*text, *times, kept, sizes)  # type: ignore[arg-type]


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value >= 0


def _gb(value: Any) -> bool:
    """A volume size as EC2 gives it: a whole number of GB, 1 to 64 TiB."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65536


@dataclass(frozen=True, slots=True)
class Cost:
    """USD: the month to date and projected to its end, None while `unpriced` names a type
    without a price, or in a region outside `REGIONS` (#499); hours: running today and this
    month."""

    month: float | None
    projected: float | None
    hours_today: float
    hours_month: float
    unpriced: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "projected": self.projected,
            "hours_today": self.hours_today,
            "hours_month": self.hours_month,
            "unpriced": list(self.unpriced),
        }


class History:
    """Every instance the collector has seen, by ID: what the estimate is made from."""

    def __init__(self, seen: dict[str, Seen] | None = None) -> None:
        self.seen = seen or {}

    def observe(self, instance: str, workshop: str, slug: str, vm: dict[str, Any], now: float):
        """A sighting of `instance` (`vm` as the collector reads it from EC2)."""
        entry = self.seen.get(instance)
        created = vm.get("created")
        if entry is None or entry.workshop != workshop:
            start = created if _number(created) and created <= now else now
            entry = self.seen[instance] = Seen(workshop, slug, vm["type"], start, now)
        entry.slug, entry.type = slug, vm["type"]  # retagged (`!adopt-spare`), or resized
        size = vm.get("volume_gb")
        entry.observe(vm["state"], vm.get("launched"), now, size if _gb(size) else None)

    def prune(self, now: float) -> None:
        """Forget an instance not seen for `KEEP`, and runs that no estimate needs any more."""
        horizon = min(month_start(now), now - RECENT)
        for instance, entry in list(self.seen.items()):
            if now - entry.last > KEEP:
                del self.seen[instance]
                continue
            entry.runs = [run for run in entry.runs if run[1] >= horizon]
            before = [v for v in entry.volumes if v[0] < horizon]  # the last is still in force
            entry.volumes = before[-1:] + [v for v in entry.volumes if v[0] >= horizon]

    def workshop(self, name: str, present: set[str], now: float, region: str) -> dict[str, Any]:
        """The workshop's totals over every instance seen in it this month, gone ones too:
        `month` and `projected` of those priced, `hours_today` of all, and the types without a
        price, if any. In a region outside `REGIONS`, `month` and `projected` are None and
        `unpriced_region` names it (#499)."""
        month = projected = today = 0.0
        unpriced: set[str] = set()
        lo = month_start(now)
        for instance, entry in self.seen.items():
            if entry.workshop != name or entry.last < lo:
                continue
            cost = entry.cost(now, instance in present, region)
            today += cost.hours_today
            unpriced.update(cost.unpriced)
            if cost.month is not None and cost.projected is not None:
                month, projected = month + cost.month, projected + cost.projected
        priced = region in REGIONS
        return {
            "month": round(month, 2) if priced else None,
            "projected": round(projected, 2) if priced else None,
            "hours_today": round(today, 2),
            "unpriced": sorted(unpriced),
            "unpriced_region": None if priced else region,
        }

    def to_json(self) -> dict[str, Any]:
        return {instance: entry.to_json() for instance, entry in sorted(self.seen.items())}

    @classmethod
    def of(cls, data: Any) -> History:
        """The history read back; an entry that doesn't read back exactly is dropped."""
        seen = {}
        if isinstance(data, dict):
            for instance, value in data.items():
                entry = Seen.of(value)
                if isinstance(instance, str) and entry is not None:
                    seen[instance] = entry
        return cls(seen)


def dollars(value: float) -> str:
    """`$1,050`: whole dollars, as the `!status` line and the console show an estimate."""
    return f"${value:,.0f}"


def spares_said(cost: Any) -> str:
    """The spares of a workshop's cost (#715), as `!status`'s line and the console show them
    after the VMs: ` · spares: 1 of 3 running`; "" for a workshop without spares, or for a
    snapshot written before the collector counted them apart."""
    spares, up = (
        cost.get(k) if isinstance(cost, dict) else None for k in ("spares", "spares_running")
    )
    if type(spares) is not int or type(up) is not int or not 0 <= up <= spares or not spares:
        return ""
    return f" · spares: {up} of {spares} running"
