#!/usr/bin/env python3
"""A sustained soak against a running DevLens, with the numbers written down.

The suite's load tests answer "does it hold under a burst?". They do not answer
"does it still hold an hour later?" — which is the question a leaked file
descriptor, an unbounded in-memory notifier or a growing WAL actually answers.
This driver runs real jobs continuously and samples the process while it does.

    scripts/soak.py --minutes 60 --concurrency 8

It fails loudly rather than printing a graph: a descriptor count or an RSS that
climbs monotonically across the whole run is reported as a leak, and the exit
code is non-zero.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

QUESTIONS = [
    "what does idempotency mean for a retry?",
    "how should a consumer handle a poison message?",
    "when is at-least-once delivery not enough?",
    "what breaks when a retry is not idempotent?",
]


def one_run(client: httpx.Client, question: str, timeout: float) -> float:
    started = time.monotonic()
    created = client.post("/jobs", json={"command": "ask", "question": question})
    created.raise_for_status()
    job_id = created.json()["id"]
    deadline = started + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").raise_for_status().json()
        if job["status"] == "completed":
            return time.monotonic() - started
        if job["status"] == "failed":
            raise RuntimeError(f"job {job_id} failed: {job.get('error')}")
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} never completed")


def sample(pid: int, db: Path) -> dict:
    status = Path(f"/proc/{pid}/status").read_text()
    fields = dict(
        line.split(":", 1) for line in status.splitlines() if ":" in line
    )
    wal = db.with_name(db.name + "-wal")
    return {
        "rss_kb": int(fields["VmRSS"].split()[0]),
        "threads": int(fields["Threads"]),
        "fds": len(list(Path(f"/proc/{pid}/fd").iterdir())),
        "db_kb": db.stat().st_size // 1024 if db.exists() else 0,
        "wal_kb": wal.stat().st_size // 1024 if wal.exists() else 0,
    }


def climbs_monotonically(series: list[int]) -> bool:
    """True when every later window is larger than the one before it.

    Growth that levels off is a cache warming up. Growth that never stops is a
    leak, and only the shape over the whole run can tell them apart.
    """
    if len(series) < 6:
        return False
    size = len(series) // 3
    windows = [
        statistics.mean(series[:size]),
        statistics.mean(series[size : 2 * size]),
        statistics.mean(series[2 * size :]),
    ]
    return windows[0] < windows[1] < windows[2] and windows[2] > windows[0] * 1.25


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8121")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    deadline = time.monotonic() + args.minutes * 60
    latencies: list[float] = []
    samples: list[dict] = []
    failures: list[str] = []
    completed = 0
    started_at = time.time()
    next_sample = time.monotonic()

    client = httpx.Client(
        base_url=args.base,
        timeout=args.timeout,
        headers={"origin": args.base},
        limits=httpx.Limits(max_connections=args.concurrency * 2),
    )
    with client, ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        while time.monotonic() < deadline:
            batch = [
                pool.submit(one_run, client, QUESTIONS[i % len(QUESTIONS)], args.timeout)
                for i in range(args.concurrency)
            ]
            for future in batch:
                try:
                    latencies.append(future.result())
                    completed += 1
                except Exception as exc:  # every failure is data, whatever it is
                    failures.append(f"{type(exc).__name__}: {exc}")
            if time.monotonic() >= next_sample:
                samples.append({"t": round(time.monotonic() - (deadline - args.minutes * 60), 1), **sample(args.pid, args.db)})
                next_sample += 15
                elapsed = (deadline - time.monotonic()) / 60
                print(
                    f"\r{completed:>7} runs | {len(failures)} failures | "
                    f"rss {samples[-1]['rss_kb'] // 1024} MB | fds {samples[-1]['fds']} | "
                    f"{elapsed:5.1f} min left",
                    end="",
                    flush=True,
                )
    print()

    ordered = sorted(latencies)
    def pct(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 3) if ordered else 0.0

    leaks = {
        name: climbs_monotonically([s[name] for s in samples])
        for name in ("rss_kb", "fds", "threads")
    }
    result = {
        "started": started_at,
        "minutes": args.minutes,
        "concurrency": args.concurrency,
        "completed": completed,
        "failures": failures[:20],
        "failure_count": len(failures),
        "latency_s": {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99), "max": round(max(ordered), 3) if ordered else 0.0},
        "first_sample": samples[0] if samples else {},
        "last_sample": samples[-1] if samples else {},
        "monotonic_growth": leaks,
        "samples": samples,
    }
    if args.report:
        args.report.write_text(json.dumps(result, indent=2))

    print(json.dumps({k: v for k, v in result.items() if k != "samples"}, indent=2))
    if failures:
        print(f"\nFAILED: {len(failures)} runs did not complete", file=sys.stderr)
        return 1
    if any(leaks.values()):
        print(f"\nFAILED: unbounded growth in {[k for k, v in leaks.items() if v]}", file=sys.stderr)
        return 1
    print("\nThe process held for the whole run: no failure, no unbounded growth.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
