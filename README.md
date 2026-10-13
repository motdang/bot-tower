# 🗼 Bot Tower

One small tool, one job: keep the bot fleet out of each other's way, and time
the pushes. Pure stdlib Python, everything in this folder.

**Open it:** double-click `Tower.command`, or:

```bash
python3 "/Users/annikapeacock/Developer/claude code projects/bot-tower/tower.py"
```

## The idea

Every shared thing a bot can touch is a **lane**: `github-push` (all pushes go
out as NaNoBotCo), `web.archive.org` (Wayback SPN, rate-limited per account),
`catalog.db` (SQLite gives its writer an exclusive lock), and so on. Before a
bot rolls, it asks the tower for its lanes:

- Lane free → green light, roll now.
- Lane held → wait your turn (or step back cleanly with `--no-wait`, exit 75).
- Some lanes keep a **courtesy gap** after each use — Wayback gets 45 quiet
  minutes, so the archive-drip and defiant-wayback can never crowd it.

Timed pushes go in a **queue** ("run lannawiki-publish at 21:30") and roll at
their moment, through the same lanes — a push can never land on top of a
running bot. The tower never schedules itself; launchd is still the clock,
the tower only decides who has the road.

## Where the fleet was colliding (why each lane exists)

- `github-push` — the 03:00 wiki publish, the 03:30 defiant nightly, **and**
  the always-on crawler's step 8/8 all push to GitHub as NaNoBotCo.
- `web.archive.org` — archive-drip fires every 3 h; defiant-wayback at 04:15.
  Same rate-limited SPN account.
- `catalog.db` — the crawler writes it continuously; the wiki publish reads it.

## Files

- `tower.py` — the whole tool (menu + subcommands, see `python3 tower.py help`)
- `fleet.json` — lanes, bots, and the push queue. Add a bot = add an entry.
- `ledger.jsonl` — the record of every run (also how courtesy gaps are kept)
- `locks/` — active holds, one small file per held lane
- `wiring/` — the launchd plists that route the existing schedules through
  the tower. **All installed 2026-08-03** (both Wayback bots, both nightly
  publishes, and the 5-minute tick that carries out queued pushes). The
  crawler's forever.sh publish step wraps `github-push,catalog.db` too
  (original kept at `forever.sh.pre-tower-backup`). Menu option 7 regenerates
  the plists and prints the copy-and-load commands if anything changes.

## Wiring in a script (one line)

Any script can borrow a lane without being a registered bot:

```bash
python3 ".../bot-tower/tower.py" wrap github-push -- git push origin main
```

For the always-on crawler, the suggested edit (shown by menu 7) wraps its
publish step in `github-push,catalog.db` so the nightly publish and the loop
take turns instead of overlapping.

## What the tower does not do

It cannot see bots that don't come through it (the two ☁ cloud tasks are on
the timetable for awareness only, and the crawler's crawl steps still run
outside it — only its publish step takes lanes). And it never decides to
start anything by itself: launchd fires the schedules, the 5-minute tick
only carries out pushes you queued.
