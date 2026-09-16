#!/usr/bin/env python3
"""Execute the CI workflow's commands exactly as the workflow file writes them.

`scripts/verify.sh` runs the same gates CI runs, but it is a second copy of
them: nothing stops the two from drifting except a test comparing substrings.
This runner removes the copy. It reads `.github/workflows/ci.yml`, takes each
step's `run:` block verbatim, and executes it with that step's shell, working
directory and environment. What passes here is the file, not a paraphrase of it.

    scripts/run-workflow.py --job backend
    scripts/run-workflow.py --list

What it deliberately does not do: run the `uses:` steps. `actions/checkout`,
`actions/setup-python`, `actions/setup-node` and `actions/upload-artifact` are
GitHub-hosted actions that only exist inside the Actions runtime; they are
listed as skipped rather than silently ignored, so the output states exactly
which part of the workflow this run did not cover.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
EXPRESSION = re.compile(r"\$\{\{")
BASH = shutil.which("bash") or "/bin/bash"

BOLD, GREEN, RED, DIM, OFF = "\033[1m", "\033[1;32m", "\033[1;31m", "\033[2m", "\033[0m"


def load() -> dict:
    if not WORKFLOW.exists():
        sys.exit(f"{WORKFLOW} does not exist.")
    return dict(yaml.safe_load(WORKFLOW.read_text(encoding="utf-8")))


def workdir(job: dict, step: dict) -> Path:
    directory = step.get("working-directory") or (
        job.get("defaults", {}).get("run", {}).get("working-directory")
    )
    return ROOT / directory if directory else ROOT


def run_job(workflow: dict, name: str, dry: bool, skip: list[str]) -> int:
    job = workflow["jobs"][name]
    env = {**os.environ, **{k: str(v) for k, v in (workflow.get("env") or {}).items()}}
    env.update({k: str(v) for k, v in (job.get("env") or {}).items()})
    skipped: list[str] = []
    failures = 0

    print(f"\n{BOLD}{name}: {job.get('name', name)}{OFF}")
    for index, step in enumerate(job["steps"], start=1):
        label = step.get("name") or step.get("uses") or f"step {index}"
        command = step.get("run")
        if command is None:
            skipped.append(f"{label} (uses: {step.get('uses')})")
            continue
        if any(fragment in command for fragment in skip):
            skipped.append(f"{label} (skipped by --skip)")
            continue
        if EXPRESSION.search(command):
            # A ${{ }} expression is evaluated by the Actions runtime, not by a
            # shell. Substituting a guess here would be running something other
            # than the workflow, which is the whole thing this script avoids.
            skipped.append(f"{label} (contains a ${{{{ }}}} expression)")
            continue
        directory = workdir(job, step)
        print(f"\n{BOLD}── {label}{OFF}  {DIM}[{directory.relative_to(ROOT) or '.'}]{OFF}")
        print(f"{DIM}{command.strip()}{OFF}")
        if dry:
            continue
        started = time.monotonic()
        # The same shell GitHub uses for a `run:` block, with the same flags:
        # `-e` so a failing line stops the step, `-o pipefail` so a failure in
        # the middle of a pipe is not masked by the last command's success.
        # Running it under a laxer shell would let a step pass here that fails
        # on a runner, which is the opposite of what this script is for.
        # The input is this repository's own workflow file, not user data.
        result = subprocess.run(  # noqa: S603
            [BASH, "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
            cwd=directory,
            env=env,
            check=False,
        )
        took = time.monotonic() - started
        if result.returncode != 0:
            print(f"{RED}FAILED after {took:.1f}s (exit {result.returncode}){OFF}")
            failures += 1
            if step.get("if") != "always()":
                break
        else:
            print(f"{GREEN}ok{OFF} {DIM}({took:.1f}s){OFF}")

    if skipped:
        print(f"\n{BOLD}Not executed here{OFF}:")
        for item in skipped:
            print(f"  · {item}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", action="append", help="job id; repeatable. Default: all")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="FRAGMENT",
        help=(
            "do not execute a step whose command contains FRAGMENT, and say so "
            "in the summary. For a command this machine cannot run — a download "
            "its network blocks, say — which is worth stating rather than "
            "quietly passing."
        ),
    )
    args = parser.parse_args()

    workflow = load()
    if args.list:
        for key, job in workflow["jobs"].items():
            steps = sum(1 for step in job["steps"] if "run" in step)
            print(f"{key:<12} {job.get('name', key)}  ({steps} run steps)")
        return 0

    failures = sum(
        run_job(workflow, name, args.dry_run, args.skip)
        for name in (args.job or list(workflow["jobs"]))
    )
    if failures:
        print(f"\n{RED}{failures} workflow step(s) failed.{OFF}")
        return 1
    print(f"\n{GREEN}Every command in {WORKFLOW.relative_to(ROOT)} ran and passed.{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
