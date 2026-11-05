"""Shared contract between the zab core and the agent backends (Phase 1).

This module is the contract between the core and the backends (SPEC §3.2, DECISIONS D17). Keep
a change backward compatible within a minor release where you can, and check every
implementation of an interface it changes.

Rules for this module:
- Plain dataclasses, enums and Protocols only. No I/O, and no imports outside the standard library.
- Every event names the backend's *native* session id (a Codex thread id or a Claude session UUID).
  The core maps native ids to Zulip topics.
- Section references ("§8.4") are to docs/SPEC.md.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

BackendKind = Literal["codex", "claude"]
ApprovalKind = Literal["command", "file_change", "network", "other"]


class ItemKind(enum.StrEnum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    REASONING = "reasoning"
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    TOOL_CALL = "tool_call"
    WEB_SEARCH = "web_search"
    PLAN = "plan"
    SUBAGENT = "subagent"
    COMPACTION = "compaction"
    OTHER = "other"


class ItemStatus(enum.StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"


class TurnStatus(enum.StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ApprovalDecision(enum.StrEnum):
    ACCEPT = "accept"
    ACCEPT_FOR_SESSION = "accept_for_session"
    DECLINE = "decline"
    CANCEL = "cancel"


class ErrorKind(enum.StrEnum):
    USAGE_LIMIT = "usage_limit"
    CONTEXT_WINDOW = "context_window"
    AUTH = "auth"
    NETWORK = "network"
    SANDBOX = "sandbox"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    OTHER = "other"


# -------------------------------------------------------------------------- sessions and inputs


@dataclass(frozen=True, slots=True, kw_only=True)
class Capabilities:
    steer: bool  # can inject input into a running turn (Codex: yes; Claude: unless steer=false)
    approvals: bool  # emits ApprovalRequested and accepts respond() (Codex: yes; Claude: no)
    live_attach: bool  # can attach to a session another client is driving (Codex daemon: yes)
    rename: bool  # supports rename() (Codex: yes)
    fork: bool = False  # supports fork_session(); opt in only after native verification
    side_question: bool = False  # supports side_question() (`!btw`, #716); likewise


@dataclass(slots=True, kw_only=True)
class SessionSettings:
    """Per-session settings. None means: use the agent's own default (§5.2)."""

    model: str | None = None
    effort: str | None = None
    approval: str | None = None  # Codex approvalPolicy, or Claude --permission-mode
    sandbox: str | None = None  # Codex sandbox or permission profile; unused by Claude
    account: str | None = None  # Claude account name (a key of backends.claude.accounts)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GitIdentity:
    """Who an agent's commits are made as, author and committer alike (issue #214)."""

    name: str  # e.g. the bot's Zulip full name, "Claude · alice"
    email: str  # e.g. the bot's Zulip e-mail: it receives no mail, so no GitHub account has it


@dataclass(slots=True, kw_only=True)
class SessionSpec:
    cwd: str  # absolute path of the session's workspace (§10.1)
    name: str  # topic name without the "✔ " prefix; used as the thread name where supported
    settings: SessionSettings = field(default_factory=SessionSettings)
    context_prompt: str = ""  # Appendix A.1; the backend decides how to inject it (§8.4, §9.1)
    # Issue #214: set in every process the agent's tools run in, for every turn of the session,
    # as GIT_AUTHOR_NAME/EMAIL and GIT_COMMITTER_NAME/EMAIL (§5.5). None: the backend sets
    # none of them and removes inherited ones, so the user's own git config applies. The core
    # resolves it per bot from `[backends.<kind>] git_identity` and passes it on every spec
    # (create, open and fork).
    git_identity: GitIdentity | None = None
    # Issue #248: the name of the bot (`[[bots]] name`) the session belongs to. Backends set it
    # as ZAB_BOT in every process the agent's tools run in, so `zab history` reads as this bot.
    # "": the backend sets nothing and removes an inherited ZAB_BOT. The core passes it on
    # every spec (create, open and fork), like git_identity.
    bot: str = ""


@dataclass(slots=True, kw_only=True)
class SessionInfo:
    native_id: str
    cwd: str
    settings: SessionSettings
    resume_hint: str  # shell command shown on the card (Appendix B.1)
    title: str | None = None
    preview: str | None = None  # first or last user prompt, for !attach listings
    updated_at: float | None = None  # unix seconds
    active_turn_id: str | None = None  # set by open_session() if a turn is running (§7.8.1)
    last_turn: TurnSummary | None = None  # the last *completed* turn, for reconciliation (§7.8.1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Attachment:
    path: str  # absolute path inside the workspace (.zab/inbox/..., §10.3)
    mime_type: str
    size: int


@dataclass(slots=True, kw_only=True)
class UserInput:
    text: str
    client_id: str  # uuid4 chosen by the core; Codex echoes it as clientId (§8.6)
    attachments: list[Attachment] = field(default_factory=list)
    sender: str = ""  # display name of the Zulip sender


# ---------------------------------------------------------------------------- items and results


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemInfo:
    item_id: str
    kind: ItemKind
    status: ItemStatus
    title: str  # one line for the status message, e.g. "pytest -q" (§8.5, §9.4)
    text: str | None = None  # full text of agent/user messages and reasoning summaries
    detail: str | None = None  # e.g. the diff for FILE_CHANGE, an output tail for COMMAND
    exit_code: int | None = None
    duration_ms: int | None = None
    client_id: str | None = None  # USER_MESSAGE only: echoed client_id; None if external


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorInfo:
    kind: ErrorKind
    message: str
    retry_after_s: float | None = None
    raw: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage:
    """Token counts as reported by the agent; not comparable across backends.

    TurnCompleted.usage covers that turn only. UsageUpdated.usage is the session's running total.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    context_window: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageWindow:
    label: str
    used_fraction: float | None = None  # 0..1; None is unknown, not zero
    resets_at: float | None = None  # Unix seconds


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageTokens:
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageCredit:
    label: str
    balance: str  # Decimal balance; only real, nonzero credits, never list-price cost


@dataclass(frozen=True, slots=True, kw_only=True)
class Usage:
    """Account limits plus session usage; missing data must not become invented zeros.

    models contains cumulative session totals, one row per reported model (or a
    single model=None aggregate for Codex). Claude input excludes cache reads and
    writes; Codex input includes cached tokens. as_of dates the session data, and
    limits_as_of separately dates account limits (fresh on-demand for Codex).
    All timestamps are Unix seconds. No cost field: CLI subscription list-price
    estimates are not money spent.
    """

    context_tokens: int | None = None
    context_window: int | None = None
    windows: tuple[UsageWindow, ...] = ()
    models: tuple[UsageTokens, ...] = ()
    as_of: float | None = None
    limits_as_of: float | None = None
    credits: tuple[UsageCredit, ...] = ()
    unavailable: tuple[str, ...] = ()  # Safe explanations, never raw exceptions


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnSummary:
    """A completed turn, as reported by open_session() (§7.8.1)."""

    turn_id: str
    status: TurnStatus
    final_text: str | None = None
    error: ErrorInfo | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    """One line of `zab doctor` output."""

    name: str
    ok: bool
    detail: str = ""
    warning: bool = False  # ok, but worth showing (e.g. an unpinned version, §8.3)


# --------------------------------------------------------------------- events (backend -> core)


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendEvent:
    session_id: str  # native id; "" for events about the whole backend


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnStarted(BackendEvent):
    turn_id: str
    pgid: int | None = None  # Claude: process group of the turn; the core stores it (§9.6, §7.8.1)
    # Actual per-turn values; never update an old turn from next-turn overrides.
    # May be re-emitted for this turn when CLI initialization resolves the model.
    model: str | None = None
    effort: str | None = None  # None = unknown default, not an inferred numeric level
    effort_supported: bool | None = None  # False omits effort; None means unknown


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemStarted(BackendEvent):
    turn_id: str
    item: ItemInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemCompleted(BackendEvent):
    turn_id: str | None  # None for items that belong to no turn (rare)
    item: ItemInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanUpdated(BackendEvent):
    turn_id: str
    steps: tuple[tuple[str, Literal["pending", "in_progress", "completed"]], ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalRequested(BackendEvent):
    turn_id: str
    request_id: str  # opaque; pass back to respond()
    kind: ApprovalKind
    summary: str  # the command, or "edit 3 files"
    detail: str = ""  # cwd and reason, or a diff
    decisions: tuple[ApprovalDecision, ...] = (
        ApprovalDecision.ACCEPT,
        ApprovalDecision.ACCEPT_FOR_SESSION,
        ApprovalDecision.DECLINE,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ApprovalResolved(BackendEvent):
    request_id: str  # answered elsewhere (e.g. the TUI) or cleared by the backend


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageUpdated(BackendEvent):
    usage: TokenUsage  # the session's running total (§8.5, §9.4)


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnCompleted(BackendEvent):
    turn_id: str
    status: TurnStatus
    final_text: str | None = None  # the text of the last agent message of the turn (§8.5, §9.4)
    error: ErrorInfo | None = None
    native_diff: str | None = None  # Codex turn/diff/updated; used only outside git repos (§10.5)
    usage: TokenUsage | None = None  # this turn only


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionChanged(BackendEvent):
    new_session_id: str | None = None  # e.g. Claude reported a different session id (§9.3)
    title: str | None = None
    settings: SessionSettings | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Notice(BackendEvent):
    level: Literal["info", "warning", "error"]
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendStatus(BackendEvent):
    """Backend availability (§7.1, §7.8.3); session_id is "".

    A backend emits up=False and retries by itself (with backoff) whenever its agent becomes
    unreachable. While it is down, its methods raise BackendUnavailable. When it emits up=True,
    the core reopens that backend's active sessions.
    """

    up: bool
    message: str = ""


# ------------------------------------------------------------------------------ workspace (§10)


@dataclass(frozen=True, slots=True, kw_only=True)
class FileStat:
    size: int
    is_dir: bool
    mtime: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class Workspace(Protocol):
    """File and command access to a session's workspace.

    Phase 1 uses a local implementation. The indirection exists so that cross-user isolation
    (§12.3) can route these operations through the Codex app-server later.
    Paths may be absolute, or relative to `cwd`.
    """

    @property
    def root(self) -> str:
        """Repository or worktree root: the boundary for all path checks (§10, §11.3)."""
        ...

    @property
    def cwd(self) -> str:
        """Where the agent runs: root plus the optional subdir (§10.1)."""
        ...

    async def realpath(self, path: str) -> str:
        """Canonical absolute path with symlinks resolved; the path need not exist."""
        ...

    async def stat(self, path: str) -> FileStat | None: ...

    async def read_bytes(self, path: str, max_bytes: int) -> bytes: ...

    async def write_bytes(self, path: str, data: bytes) -> None: ...

    async def run(
        self, argv: Sequence[str], *, cwd: str | None = None, timeout_s: float = 10.0
    ) -> CommandResult:
        """Run a fixed argument vector. Never a shell string (§10.6)."""
        ...


# ----------------------------------------------------------------------------- backend (§8, §9)


@runtime_checkable
class Backend(Protocol):
    @property
    def name(self) -> str: ...  # the [backends.<name>] key

    @property
    def kind(self) -> BackendKind: ...

    @property
    def capabilities(self) -> Capabilities: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def doctor(self) -> list[Check]: ...

    async def login(
        self,
        account: str | None,
        send: Callable[[str], Awaitable[None]],
        receive: Callable[[float], Awaitable[str | None]],
    ) -> bool:
        """Run the CLI's own login (D24); None selects the default account.

        send posts to the owner's DM; receive returns their next DM or None on
        timeout. Never echo, persist or log received text. Return True when signed
        in, refreshing cached availability. Clean up on cancellation: core imposes
        a ten-minute deadline and cancels on !login cancel or bridge shutdown.
        """
        ...

    def events(self) -> AsyncIterator[BackendEvent]:
        """A single stream of events for all sessions of this backend. Called once by the core."""
        ...

    async def create_session(self, spec: SessionSpec) -> SessionInfo: ...

    async def fork_session(self, native_id: str, spec: SessionSpec) -> SessionInfo:
        """Create an independent child through the parent's last completed turn (#202).

        Called only when capabilities.fork is true. spec supplies the child's separate
        workspace, name, inherited effective settings (including approval/sandbox/account)
        and context prompt. Explicitly apply those settings; native defaults may differ.
        Select a completed native turn boundary, excluding any unfinished turn. Never
        interrupt, resume with overrides, rename or otherwise mutate the parent.
        Return the child without starting its first prompt; core admits it through the
        normal turn semaphore. Native creation may be deferred until start_turn where
        needed, using the existing SessionChanged mechanism if its native id changes.
        Freeze the completed boundary at this call, even if native creation is deferred.
        Refuse with ValueError and a safe user-facing reason if there is no completed
        boundary, or if this backend cannot fork safely while the parent is running.
        Older backends keep fork=False; core must not call an absent hook.
        """
        raise NotImplementedError("This backend does not support session forks")

    async def side_question(self, native_id: str, question: str, *, timeout_s: float) -> str:
        """`!btw` (#716, SPEC §7.13): the agent's answer to `question`, from the conversation.

        Called only when capabilities.side_question is true, with the user's raw question; the
        backend frames it. The answer sees at least the session through its last ended turn,
        and the running turn's progress where the backend can include it safely. Nothing
        enters any history: no native turn or thread is kept, no event reaches events(), and
        the session's settings, any running turn and the workspace are left as they were. No
        tools where the backend can drop them, else the narrowest sandbox; never an approval
        (one that arrives is declined). It takes no turn slot. Within `timeout_s` the backend
        stops what it started and raises TimeoutError. Refuse with ValueError and a safe
        user-facing reason (no completed turn yet, the session isn't open); other failures
        raise. Older backends keep side_question=False; core must not call an absent hook.
        """
        raise NotImplementedError("This backend does not answer side questions")

    async def open_session(self, native_id: str, spec: SessionSpec) -> SessionInfo:
        """Reattach after a restart, or for !attach. Reports any active turn (§7.8.1).

        Lenient on purpose: a session the core persisted may have no native record yet (its
        first turn never started), and recovery must still reconstruct it. `!attach ID` with
        a user-supplied id goes through `lookup_session` first (§8.7, §9.8).

        Raise BackendUnavailable only when the whole backend is down, and SessionRefused
        when it is up but cannot open this one session.
        """
        ...

    async def lookup_session(self, native_id: str) -> SessionInfo | None:
        """Find a native session in the backend's own records (rollouts, transcripts) without
        opening or creating anything: its real cwd and account, plus preview and last turn
        when known. None when no record exists, so `!attach ID` can refuse an unknown id
        instead of binding a fresh session to the topic (§8.7, §9.8).
        """
        ...

    async def list_sessions(self, cwd: str | None, limit: int = 20) -> list[SessionInfo]: ...

    async def start_turn(self, session_id: str, turn_input: UserInput) -> str:
        """Start a turn; return its id. TurnStarted may arrive before or after this returns."""
        ...

    async def steer(self, session_id: str, turn_id: str, turn_input: UserInput) -> None:
        """Only if capabilities.steer. Raise SteerRejected if the turn cannot take input.

        When the agent takes the input in, which may be well after this returns (after the
        tool call that runs), emit ItemCompleted(USER_MESSAGE) with its client_id; the core
        then marks the message read (#654). A backend that cannot tell emits nothing.
        """
        ...

    async def interrupt(self, session_id: str) -> None: ...

    async def respond(self, request_id: str, decision: ApprovalDecision) -> None: ...

    async def update_settings(self, session_id: str, settings: SessionSettings) -> SessionSettings:
        """Validate before mutation; apply from the next turn (SPEC §7.9).

        Raise InvalidSetting for unusable values without changing backend settings.
        Return accepted overrides to persist, keeping None for agent defaults.
        On a model change, clear an inherited unsupported effort to None instead
        of rejecting the model; core notes the reset. An explicit invalid effort
        raises InvalidSetting. Do not mutate the caller's settings object.
        """
        ...

    def effective_settings(self, session_id: str) -> SessionSettings | None:
        """Return known resolved settings for display only, without I/O or mutation.

        Unknown fields remain None; return None if nothing is known. Defaults and
        explicit overrides remain distinct in persisted SessionSettings. Core
        falls back to those overrides when this optional display hook is absent
        during rollout. Never infer an effort from the agent's prose.
        """
        return None

    async def usage(self, session_id: str) -> Usage | None:
        """Read usage without starting a turn or reading agent credential files.

        Codex may query account limits; Claude returns cached CLI observations.
        Preserve unknown fields and explain failed reads via Usage.unavailable.
        Return None when unsupported; core tolerates an absent hook during rollout.
        """
        return None

    async def rename(self, session_id: str, name: str) -> None: ...

    def workspace(self, root: str, cwd: str) -> Workspace:
        """A Workspace for this backend's sessions (Phase 1: local; §12.3 later)."""
        ...


class InvalidSetting(Exception):
    """An unusable setting, with choices suitable for a core-rendered reply (§7.9)."""

    def __init__(self, field: str, value: str | None, valid: list[str], message: str = "") -> None:
        super().__init__(message or f"Invalid {field}")
        self.field = field
        self.value = value
        self.valid = list(valid)
        self.message = message


class SteerRejected(Exception):
    """Raised by Backend.steer(); the core then holds the message instead (§7.3)."""


class BackendUnavailable(Exception):
    """Raised by any Backend method while the backend is down (§7.1, §7.8.3)."""


class SessionRefused(Exception):
    """Raised by open_session() when the backend is up but refuses this one session (§7.8.1).

    Its message is a fixed reason for the user, without request contents. The core puts only
    that session in `error`, reopens the others, and `!status` shows the reason and retries.
    """


# ----------------------------------------------------------------- workspace manager (§10; B)


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedWorkspace:
    repo_root: str  # the worktree root in worktree mode
    cwd: str  # repo_root plus subdir
    branch: str | None  # e.g. "zab/fix-parser-3fa2c1"; None if HEAD is detached
    base: str | None  # for the card, e.g. "main@4e1d2aa" (Appendix B.1)


@dataclass(frozen=True, slots=True, kw_only=True)
class Snapshot:
    """Workspace state at session or turn start (§10.5). The core stores it JSON-encoded."""

    ref: str  # commit id from `git stash create`, or the HEAD sha
    untracked: tuple[str, ...] = ()  # repo-relative paths untracked at snapshot time


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotRef:
    """A turn snapshot pushed to the workspace's GitHub repository (rc6, issue #36, §10.7)."""

    sha: str  # the commit, kept alive by refs/zab/turns/<name> here and on GitHub
    repo: str  # the name of origin's GitHub repository (its owner was checked)


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishedSnapshot:
    """A turn's snapshot as the central file server serves it (rc6, issue #36, §10.7).

    `url` is the base of its files: `f"{url}/{path in the snapshot}"`, i.e.
    `{[render] refserve_url}/{repo}/{sha}`; the server fetches `refs/zab/turns/*` from the
    repository on GitHub. Built by the core from a `SnapshotRef`.
    """

    sha: str
    url: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DiffFile:
    """One file a turn touched, for the diff message's file list (§11.8, rc6).

    `status` is "new" (added since the start ref, committed or not, or a new untracked
    file), "deleted" or "modified". `insertions`/`deletions` are None when unknown: a
    binary file (`binary`), a hidden one, or a new untracked file too large or not a
    regular file to count. `hidden`: a denylisted name (§11.4), shown without counts or
    link as "changed (contents hidden)".
    """

    path: str  # repository-relative
    status: str = "modified"
    insertions: int | None = None
    deletions: int | None = None
    binary: bool = False
    hidden: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DiffResult:
    files_changed: int
    insertions: int
    deletions: int
    stat: str  # `git diff --stat` output
    patch: str  # unified diff of tracked changes
    new_untracked: tuple[str, ...] = ()
    # repo-relative paths of the changed tracked files, hidden (denylisted) ones included;
    # with `new_untracked`, the files the turn touched (§11.3)
    changed: tuple[str, ...] = ()
    # every file the turn touched, tracked changes then new untracked files, with status and
    # line counts (§11.8, rc6); empty from an older workspace, and the renderer then falls
    # back to `changed`, `new_untracked` and the hidden lines of `stat`
    files: tuple[DiffFile, ...] = ()

    @property
    def empty(self) -> bool:
        return self.files_changed == 0 and not self.new_untracked


class UploadRefused(Exception):
    """Raised by WorkspaceManager.check_upload(); str(exc) is shown to the user (§11.4)."""


@runtime_checkable
class WorkspaceManager(Protocol):
    async def prepare(
        self,
        project: Workspace,  # Workspace whose root and cwd are the project path
        mode: Literal["shared", "worktree"],
        slug: str,
        subdir: str = "",
        *,
        base_commit: str | None = None,
    ) -> PreparedWorkspace:
        """Create the worktree and exclude .zab/ (§10.1, §10.2; #202).

        None preserves the existing HEAD/shared behavior. A supplied base_commit is a
        validated full commit id in this repository, allowed only in worktree mode;
        create the new branch from it without modifying the parent's index or files.
        The caller supplies a local snapshot including permitted uncommitted changes.
        Fork snapshots must preserve parent ancestry so the child can be merged back;
        published §10.7 snapshots are deliberately parentless and need a local wrapper
        commit before use here. Existing callers omit this keyword until the concrete
        WorkspaceManager implementation supports it. Cleanup remains discard().
        """
        ...

    async def fork_base(self, ws: Workspace, name: str) -> str | None:
        """The local wrapper commit prepare(base_commit=...) wants for a fork (#202).

        The parent workspace as it stands, selected as for publish_snapshot (tracked changes,
        staged and untracked files within the caps), committed **with the parent's HEAD as
        parent** under refs/zab/forks/<name> and never pushed. A tracked file a snapshot would
        drop (denylisted, a second hard link) keeps HEAD's version, so merging the fork back
        never deletes it; new denylisted or oversized files stay out. Created only if absent:
        the same name returns the same commit. name is "<parent session id>/<n>". Branch,
        index and working tree untouched. None outside git, without a HEAD, or on failure.
        """
        ...

    async def snapshot(self, ws: Workspace) -> Snapshot | None:
        """None if ws is not inside a git repository."""
        ...

    async def diff(self, ws: Workspace, since: Snapshot) -> DiffResult | None: ...

    async def save_attachment(
        self, ws: Workspace, message_id: int, filename: str, data: bytes, mime_type: str
    ) -> Attachment:
        """Write to <root>/.zab/inbox/<message_id>-<sanitized name> (§10.3)."""
        ...

    async def check_upload(self, ws: Workspace, path: str, *, explicit: bool) -> Upload:
        """Root check, denylist and size limit (§11.4). Raises UploadRefused."""
        ...

    async def publish_snapshot(
        self, ws: Workspace, name: str, *, github_owner: str
    ) -> SnapshotRef | None:
        """Commit the workspace as it stands and push it to GitHub (§10.7, rc6).

        Tracked changes plus untracked files (respecting .gitignore), minus every denylisted
        path even when tracked (§11.4), minus untracked files over the size caps; built with a
        temporary index (`GIT_INDEX_FILE`) from HEAD's tree, `write-tree` and `commit-tree`
        without a parent (a push carries no unpublished history, whose commits may hold
        denylisted files), under the hardening of §10.6. The branch, the real index and the
        working tree are never touched. Kept under `refs/zab/turns/<name>`, created only if
        absent, then pushed to `origin` as that same ref
        with the checkout's deploy key and a timeout of about 20 s; no branch is ever pushed.
        `name` is `<session id>/<turn row>`: the session's UUID (letters, digits and `-`), `/`,
        digits. Once per name: an existing ref's commit is reused, and pushed again (which is
        idempotent), so a retried completion keeps linking the snapshot it already posted.
        None unless `origin` is a GitHub repository owned by `github_owner`, outside a git
        repository, and on any failure, including a failed push: rendering then falls back to
        §11.3 without snapshot links. Never raises.
        """
        ...

    async def discard(self, prepared: PreparedWorkspace) -> None:
        """Remove a worktree that prepare() created, after session creation failed (§7.2).

        A no-op for shared workspaces (the project checkout is never touched) and for a
        worktree that is already gone. Session worktrees are otherwise never deleted (§10.1).
        Errors: raises WorkspaceError when git refuses (a worktree with uncommitted changes
        it will not remove, a lock); the caller logs it and continues, since the session
        creation it cleans up after has already failed and nothing else depends on it.
        """
        ...


# ------------------------------------------------------------------------------ rendering (§11)


@dataclass(frozen=True, slots=True, kw_only=True)
class Upload:
    path: str  # absolute path inside the workspace, already checked against the denylist
    name: str  # file name shown in Zulip
    # The exact bytes to send, when the caller already read and validated them (text uploads,
    # issue #36); None = the upload callback reads the file itself.
    data: bytes | None = None


@dataclass(slots=True, kw_only=True)
class RenderContext:
    workspace: Workspace
    max_message_length: int  # from the Zulip /register response (§4.10)
    max_upload_bytes: int  # min(render.max_upload_mb, server limit) (§11.4)
    upload: Callable[[Upload], Awaitable[str]] | None  # -> /user_uploads URL; None = no uploads
    auto_upload_ext: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "svg", "pdf"})
    # The turn's snapshot on the central file server, for the final answer and its diff (rc6,
    # §10.7): links to files in it go there instead of *(local)* or text uploads. A provider,
    # awaited only by a message that would link at least one file, so that a turn linking no
    # file publishes and pushes nothing; the core memoizes it per turn, so the answer and its
    # diff share one snapshot and one push. It returns None when there is no snapshot and never
    # raises. None = snapshot links off.
    snapshot: Callable[[], Awaitable[PublishedSnapshot | None]] | None = None
    turn_number: int | None = None  # used in the names of overflow uploads (§11.7)
    # Repo-relative paths the turn being answered created or modified (its diff against the
    # start snapshot, §10.5); None = no turn information. A reference that would resolve to
    # an untouched file prefers the one touched file ending with the written path (§11.3).
    turn_files: frozenset[str] | None = None
    # Text uploads (issue #36): a final answer's references to text files with these
    # extensions are uploaded as `<name>.<ext>.txt` when they would otherwise be *(local)*.
    # Empty = off. Only when `final_answer` is set, at most `max_text_uploads` per answer.
    text_upload_ext: frozenset[str] = frozenset()
    max_text_upload_bytes: int = 512 * 1024
    max_text_uploads: int = 10
    final_answer: bool = False


@runtime_checkable
class Renderer(Protocol):
    async def render_agent_text(self, text: str, ctx: RenderContext) -> list[str]:
        """Agent markdown -> Zulip message parts, ready to post (§11 steps 1-7)."""
        ...

    def render_inline(self, text: str) -> str:
        """One line of prose (commentary, reasoning headings): redaction, math, mentions.

        No links, uploads or splitting. Never pass command lines or paths; the core puts those
        in code spans itself (§11).
        """
        ...

    async def render_diff(self, diff: DiffResult, ctx: RenderContext) -> str:
        """The diff message: inline spoiler if it fits, else the stat plus an upload (§11.8)."""
        ...
