#!/usr/bin/env python3
"""push_walk — push every repo that already has a remote and is ahead of it.

  1. Only a repo that already has a remote is considered. It never adds one.
  2. Before every push, the pending diff is grepped for the patterns in the
     private list; a hit aborts that repo's push and is logged.

The deny names, deny paths and patterns live in ~/.claude/state/push_walk_private.json,
on this Mac only. Without that file the walk pushes nothing.

Run: python3 push_walk.py [--dry-run]
"""
import os, re, json, subprocess, sys, datetime, pathlib

HOME = pathlib.Path.home()
ROOTS = [HOME / "Developer", HOME / "Desktop"]

PRIVATE = HOME / ".claude" / "state" / "push_walk_private.json"
try:
    _p = json.loads(PRIVATE.read_text())
except Exception:
    sys.exit(f"push_walk: {PRIVATE} missing or unreadable; pushing nothing")
DENY_NAMES = set(_p["deny_names"])
DENY_PATHS = [HOME / x for x in _p["deny_paths"]]
CASE_IDS = re.compile(_p["case_ids"], re.I)
GOOD_EMAIL = re.compile(r"(NaNoBotCo@users\.noreply|nanobotco@users\.noreply|privaterelay\.appleid)", re.I)

LOG = HOME / "Developer" / "claude code projects" / "bot-tower" / "push-walk.log"


# --- UPTAKE cadence trial: sequestered control group (2026-09-19 .. 2026-10-03) ---
# These seven are the trial's holdout. They must receive NO push for the duration,
# or the control arm stops being a control. The guard EXPIRES BY ITSELF on the end
# date so it cannot be forgotten and quietly strand seven repos: after that date the
# set is empty and the walk treats them normally again. Assignment and reasoning:
# ~/Desktop/claude code projects/uptake/experiments/cadence-ab-assignment.json
SEQUESTER_UNTIL = datetime.date(2026, 10, 3)
SEQUESTERED = {
    "blinky-fidget", "amulet-essentials", "moat-stardew", "thai-answers",
    "amulet-reference", "moat-game", "taoist-oracle",
}


def sequestered():
    return SEQUESTERED if datetime.date.today() <= SEQUESTER_UNTIL else set()


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def log(line):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"{stamp}  {line}"
    print(msg, flush=True)
    with open(LOG, "a") as fh:
        fh.write(msg + "\n")


def discover():
    for root in ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(root):
            depth = len(pathlib.Path(dirpath).relative_to(root).parts)
            if depth > 3:
                dirnames[:] = []
                continue
            if ".git" in dirnames:
                yield pathlib.Path(dirpath)
                dirnames[:] = []


def eligible(repo):
    if repo.name in DENY_NAMES:
        return None
    if repo.name in sequestered():
        return ("SEQUESTERED", None)
    for bad in DENY_PATHS:
        try:
            repo.relative_to(bad)
            return None
        except ValueError:
            pass
    if not git(repo, "remote").stdout.strip():
        return None                      # no remote: not this job's business, ever
    up = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if up.returncode != 0:
        return ("NO-UPSTREAM", None)
    ahead = git(repo, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
    return (int(ahead or 0), up.stdout.strip())


def main():
    dry = "--dry-run" in sys.argv
    pushed = skipped = held = 0
    for repo in sorted(discover()):
        state = eligible(repo)
        if state is None:
            continue
        ahead, _ = state
        if ahead == "SEQUESTERED":
            log(f"HOLD   {repo.name}: sequestered as UPTAKE trial control until "
                f"{SEQUESTER_UNTIL} — not pushed")
            held += 1
            continue
        if ahead == "NO-UPSTREAM":
            log(f"HOLD   {repo.name}: has a remote but no upstream branch set — needs a hand")
            held += 1
            continue
        if ahead == 0:
            continue

        diff = git(repo, "diff", "@{u}..HEAD").stdout
        hits = sorted(set(m.group(0) for m in CASE_IDS.finditer(diff)))
        if hits:
            log(f"BLOCKED {repo.name}: case identifiers in pending diff -> {hits}. NOT PUSHED.")
            held += 1
            continue

        emails = [e for e in git(repo, "log", "@{u}..HEAD", "--format=%ae").stdout.split()
                  if not GOOD_EMAIL.search(e)]
        if emails:
            log(f"WARN   {repo.name}: {len(emails)} pending commits authored to an unlinked "  # stylecheck: allow — operator log line
                f"address {sorted(set(emails))} — these will not appear on the profile")

        if dry:
            log(f"DRY    {repo.name}: would push {ahead}")
            continue
        res = git(repo, "push", "origin", "HEAD")
        if res.returncode == 0:
            log(f"PUSHED {repo.name}: {ahead} commits")
            pushed += 1
        else:
            log(f"FAIL   {repo.name}: {res.stderr.strip().splitlines()[-1:] or ['?']}")
            skipped += 1

    log(f"walk done — pushed {pushed}, failed {skipped}, held {held}")


if __name__ == "__main__":
    main()
