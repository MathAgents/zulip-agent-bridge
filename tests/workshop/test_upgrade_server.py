"""`workshop/admin/upgrade_server.py`, the bot's side of `!upgrade-server` (docs/ADMIN.md §1.6;
#569), against a stand-in for the upgrade's root helpers: a thread that claims each request by
a rename, as `confighelper.take` does, and writes its outcome as `upgradehelper.run` does."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import threading

import pytest
import upgrade_server as us

SHA = "cd" * 32
COMMIT = "1" * 40
BASE = ["--plan", "P-4", "--release", "v0.3.1", "--sha256", SHA]


@pytest.fixture
def state(tmp_path, monkeypatch):
    (tmp_path / "requests").mkdir(mode=0o700)
    (tmp_path / "results").mkdir()
    for name, value in (("POLL", 0.01), ("SLOT", 2.0), ("LATE", 0.3), ("PLAN_WAIT", 5.0),
                        ("APPLY_WAIT", 5.0)):  # fmt: skip
        monkeypatch.setattr(us, name, value)
    return tmp_path


class Helper(threading.Thread):
    """Claims `requests/<action>.json` by a rename and answers it with `answer(request)`, a
    dict, or nothing (None: took it and said nothing; "unread": as for a request whose plan
    can't be read). `seen` keeps every request it took, with its file's mode."""

    def __init__(self, state: pathlib.Path, action: str, answer, delay: float = 0.0) -> None:
        super().__init__(daemon=True)
        self.state, self.action, self.answer, self.delay = state, action, answer, delay
        self.seen: list[tuple[dict, int]] = []
        self.stop = threading.Event()

    def run(self) -> None:
        target = self.state / "requests" / f"{self.action}.json"
        while not self.stop.wait(0.01):  # never time.sleep, which another test may replace
            claimed = target.with_name(f"{self.action}.helper.taken")
            try:
                os.rename(target, claimed)
            except FileNotFoundError:
                continue
            body = json.loads(claimed.read_text())
            self.seen.append((body, stat.S_IMODE(claimed.stat().st_mode)))
            claimed.unlink()
            if self.stop.wait(self.delay):
                return
            said = self.answer(body)
            if said is None:
                continue
            if said == "unread":
                where, said = "unread", {"ok": False, "message": "the request is not JSON"}
                body = {"plan": None, "nonce": None}
            else:
                where = body["plan"]
            out = {"version": 1, "action": self.action, "plan": body["plan"],
                   "nonce": body["nonce"], **said}  # fmt: skip
            path = self.state / "results" / f"{where}.{self.action}.json"
            path.with_suffix(".new").write_text(json.dumps(out))
            path.with_suffix(".new").replace(path)


#: Every helper a test started, stopped and joined when the test ends (#901's re-review, B1):
#: one left running would go on into later tests, and act on their files with their stand-ins.
_STARTED: list[Helper] = []


@pytest.fixture(autouse=True)
def _stop_helpers():
    yield
    for found in _STARTED:
        found.stop.set()
    for found in _STARTED:
        found.join(5)
    alive = [found for found in _STARTED if found.is_alive()]
    _STARTED.clear()
    assert not alive, f"{len(alive)} helper threads did not stop"


def helper(state, action, answer, delay=0.0) -> Helper:
    found = Helper(state, action, answer, delay)
    _STARTED.append(found)
    found.start()
    return found


def run(state, *extra) -> int:
    return us.main([*BASE, "--state", str(state), *extra])


def test_the_plan_prints_the_helpers_text(state, capsys):
    h = helper(state, "plan", lambda body: {"ok": True, "text": "the plan's text\nline 2\n"})
    assert run(state, "--dry-run") == 0
    assert capsys.readouterr().out == "the plan's text\nline 2\n"
    ((body, mode),) = h.seen
    assert mode == 0o600 and set(body) == {"version", "plan", "nonce", "release", "sha256"}
    assert (body["plan"], body["release"], body["sha256"]) == ("P-4", "v0.3.1", SHA)
    assert len(body["nonce"]) == 32 and int(body["nonce"], 16) >= 0


def test_a_refused_plan_says_the_helpers_reason(state, capsys):
    helper(state, "plan", lambda body: {"ok": False, "message": 'git fetch said "fatal: x"'})
    assert run(state, "--dry-run") == 1
    assert capsys.readouterr().out == 'the plan helper refused it: git fetch said "fatal: x"\n'


def test_an_earlier_runs_outcome_is_never_taken_for_this_one(state, capsys, monkeypatch):
    """The nonce: an outcome for this plan but another request (the plan's own dry run, before
    the apply's recheck) is not this request's."""
    old = {"version": 1, "plan": "P-4", "nonce": "0" * 32, "ok": True, "text": "stale"}
    (state / "results" / "P-4.plan.json").write_text(json.dumps(old))
    helper(state, "plan", lambda body: {"ok": True, "text": "fresh"}, delay=0.2)
    assert run(state, "--dry-run") == 0
    assert capsys.readouterr().out == "fresh\n"


def test_the_apply_sends_the_commit_its_plan_names(state, capsys):
    planned = {"ok": True, "release": "v0.3.1", "commit": COMMIT, "nonce": "0" * 32}
    (state / "results" / "P-4.plan.json").write_text(json.dumps(planned))
    answer = {"ok": True, "message": "v0.3.1 is installed and current", "current": "v0.3.1",
              "switched": True, "log": ["deploy.sh: done"]}  # fmt: skip
    h = helper(state, "apply", lambda body: answer)
    assert run(state) == 0
    ((body, _),) = h.seen
    assert body["commit"] == COMMIT and body["sha256"] == SHA
    assert capsys.readouterr().out == (
        "the apply helper: v0.3.1 is installed and current\ndeploy.sh's last lines:\n"
        "  deploy.sh: done\nThe bots restart into v0.3.1 now: `!version` answers from it.\n"
    )


def test_a_failed_apply_says_so_and_whether_current_moved(state, capsys):
    planned = {"ok": True, "release": "v0.3.1", "commit": COMMIT}
    (state / "results" / "P-4.plan.json").write_text(json.dumps(planned))
    helper(state, "apply", lambda body: {"ok": False, "message": "failed before the switch",
                                         "current": "v0.3.0"})  # fmt: skip
    assert run(state) == 1
    out = capsys.readouterr().out
    assert out == "the apply helper: failed before the switch\n" and "restart" not in out


@pytest.mark.parametrize(
    "planned",
    [None, {"ok": False, "release": "v0.3.1", "commit": COMMIT},
     {"ok": True, "release": "v0.3.2", "commit": COMMIT}, {"ok": True, "release": "v0.3.1"}],
)  # fmt: skip
def test_an_apply_without_its_plans_good_check_sends_nothing(state, capsys, planned):
    if planned is not None:
        (state / "results" / "P-4.plan.json").write_text(json.dumps(planned))
    assert run(state) == 1
    assert capsys.readouterr().out.startswith("upgrade_server.py: P-4's check on record")
    assert os.listdir(state / "requests") == []


def test_with_no_helper_the_request_is_withdrawn(state, capsys, monkeypatch):
    monkeypatch.setattr(us, "PLAN_WAIT", 0.2)
    assert run(state, "--dry-run") == 1
    out = capsys.readouterr().out
    assert (
        out == "upgrade_server.py: the helper said nothing within 0 s: the request is withdrawn\n"
    )
    assert os.listdir(state / "requests") == []


def test_a_helper_that_took_it_and_said_nothing_is_said(state, capsys, monkeypatch):
    monkeypatch.setattr(us, "PLAN_WAIT", 0.2)
    helper(state, "plan", lambda body: None)
    assert run(state, "--dry-run") == 1
    out = capsys.readouterr().out
    assert "the helper took the request but said nothing within 0 s" in out
    assert "journalctl -u zab-admin-upgrade-plan" in out


def test_a_request_the_helper_couldnt_read_is_answered(state, capsys):
    helper(state, "plan", lambda body: "unread")
    assert run(state, "--dry-run") == 1
    assert capsys.readouterr().out == "the plan helper refused it: the request is not JSON\n"


def test_a_request_never_replaces_another(state, capsys):
    """One request of a kind at a time: an earlier one still waiting is waited for, then this
    one goes, never over it."""
    earlier = state / "requests" / "plan.json"
    earlier.write_text('{"plan": "P-3"}')
    h = helper(
        state, "plan", lambda body: {"ok": True, "text": "mine"} if "nonce" in body else None
    )
    assert run(state, "--dry-run") == 0
    assert [body.get("plan") for body, _ in h.seen] == ["P-3", "P-4"]


def test_a_request_has_one_link_from_the_moment_it_is_there(state, monkeypatch):
    """Codex round 1 on #898: the helper refuses a request with two links (`confighelper.take`),
    so the request is renamed into place in one step, never linked and then unlinked. A
    link-then-unlink would also leave one link once done, so `os.link` fails here (review of
    #898, N1): renameat2 never calls it."""
    seen = []
    real = us.rename_noreplace

    def watched(source, target):
        real(source, target)
        seen.append(os.lstat(target).st_nlink)  # before anything else runs

    def no_link(*args, **kwargs):
        raise AssertionError(f"os.link{args}: a second link, however brief")

    monkeypatch.setattr(us, "rename_noreplace", watched)
    monkeypatch.setattr(os, "link", no_link)
    target = us.send(state, "plan", {"plan": "P-4"})
    assert seen == [1] and os.lstat(target).st_nlink == 1
    assert os.listdir(state / "requests") == ["plan.json"]  # the temporary name is gone


def test_the_rename_never_replaces_a_file(tmp_path):
    source, target = tmp_path / "new", tmp_path / "there"
    source.write_text("mine")
    target.write_text("theirs")
    with pytest.raises(FileExistsError):
        us.rename_noreplace(source, target)
    assert (source.read_text(), target.read_text()) == ("mine", "theirs")
    us.rename_noreplace(source, tmp_path / "free")
    assert (tmp_path / "free").read_text() == "mine" and not source.exists()


def test_a_slot_never_freed_is_said(state, capsys, monkeypatch):
    monkeypatch.setattr(us, "SLOT", 0.1)
    (state / "requests" / "plan.json").write_text('{"plan": "P-3"}')
    assert run(state, "--dry-run") == 1
    assert "is still there: the helper hasn't taken an earlier request" in capsys.readouterr().out
    assert os.listdir(state / "requests") == ["plan.json"]  # the earlier one, untouched


@pytest.mark.parametrize(
    "bad",
    [["--plan", "P-0"], ["--release", "main"], ["--sha256", SHA[:-1]], ["--sha256", SHA.upper()]],
)
def test_malformed_values_are_refused(state, bad):
    args = dict(zip(BASE[::2], BASE[1::2], strict=True)) | dict([bad])
    argv = [part for pair in args.items() for part in pair]
    assert us.main([*argv, "--state", str(state), "--dry-run"]) == 2
    assert os.listdir(state / "requests") == []
