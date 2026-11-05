# Rehearsal latency and rate-budget evidence (SPEC §14, §17)

Use the JSON logs under `<state_dir>/logs/zab.log` (including retained rotations)
from the rehearsal interval. They contain metadata only. Each `event="ack"` has
`latency_s` (local event-batch arrival to successful 👀 posting) and
`latency_e2e_s` (Zulip message `timestamp` to successful posting). **Use the
end-to-end sample for the ≤5-second rehearsal gate:** it includes stalls before
an event poll, including shared-budget waits. Server timestamps have 1-second
resolution, so these samples have up to roughly one second of quantization
uncertainty. Run `zab doctor` first and resolve clock-skew warnings; the end-to-end
measurement uses the bridge's wall clock and assumes it remains synchronized.
Missing/invalid/future timestamps yield null, never a substitute local sample.
Do not average per-bot percentiles. This reads each retained sample once:

```bash
python3 - /path/to/rehearsal/logs/zab.log* <<'PY'
import json, math, sys
samples = []
unavailable = 0
for path in sys.argv[1:]:
    with open(path) as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("event") == "ack":
                value = row.get("latency_e2e_s")
                if value is None:
                    unavailable += 1
                else:
                    samples.append(value)
samples.sort()
if not samples:
    print("NO DATA: no end-to-end acknowledgement samples in these logs")
else:
    p50 = samples[math.ceil(len(samples) * .50) - 1]
    p95 = samples[math.ceil(len(samples) * .95) - 1]
    verdict = "INCOMPLETE" if unavailable else "PASS" if p95 <= 5 else "FAIL"
    print(f"n={len(samples)} unavailable={unavailable} "
          f"e2e p50={p50:.3f}s p95={p95:.3f}s {verdict}")
PY
```

Select logs covering only the intended rehearsal and preserve them before rotation
removes older evidence. The output describes retained successful acknowledgements;
check `ack_untracked`, `arrival_dropped`, missed messages and failures separately.
`ack_e2e_unavailable` counts acknowledged messages without a usable server time;
legacy logs also lack the new field. Resolve missing evidence before declaring a
pass. Every ten minutes, `event="metrics"` records interval counters and p50/p95 by bot
and backend: `ack_p50_s`/`ack_p95_s` are local;
`ack_e2e_p50_s`/`ack_e2e_p95_s` are end-to-end. It also reports retained/dropped
sample counts (10,000-sample cap).
`ack_e2e_samples` and `ack_e2e_samples_dropped` track the end-to-end sample cap
separately from unavailable timestamps. Shutdown writes the remaining partial
interval. Empty intervals use null percentiles.

`zulip_requests` counts every authenticated API attempt: sends, reads, edits,
reactions, uploads, queue registration and event polling, including failures and
retries. They share a rolling limit of 150 per 60 seconds, leaving headroom below
the SPEC's 200/minute server budget. Server reset headers and `retry_after` can
extend the wait. A pending long poll does not block posting. `zulip_429` records
carry numeric `retry_after`; response bodies and owner login replies are not logged.

`turns_started`, `turns_completed` (successful), `turns_failed` and
`turns_interrupted` count observed native lifecycles; presentation retries do not
increment them again. A turn can start in one interval/process and finish in
another. `reconnects` covers backend down→up and successful queue replacement;
it does not count every low-level TCP reconnect performed by HTTPX.
