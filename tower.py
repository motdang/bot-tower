#!/usr/bin/env python3
"""
tower.py — Bot Tower: one small tool that keeps the bot fleet out of each
other's way and times the pushes.

The whole idea, in three sentences:

  Every shared thing a bot can touch (a website, a repo, a database file) is a
  LANE, and before a bot rolls it asks the tower for its lanes. If another bot
  holds a lane, the newcomer waits its turn; some lanes also keep a courtesy
  GAP after each use (Wayback likes 45 quiet minutes). Timed pushes go in a
  QUEUE and roll when their moment comes — through the same lanes, so a push
  can never land on top of a running bot.

Nothing here schedules itself: launchd (or you) is still the clock. The tower
is the traffic bureau — it only decides who has the road.

Run with no arguments for the menu. Subcommands for wiring into scripts:

  tower.py run <bot>              run a fleet bot through its lanes
  tower.py wrap <lane,lane> -- <command...>
                                  run any one-off command through lanes
  tower.py due                    roll any queued pushes whose time has come
  tower.py board                  who is on the road right now
  tower.py timetable              the whole day at a glance
  tower.py ledger                 recent runs
  tower.py release <lane>         clear a hold by hand
  tower.py wiring                 write launchd plists that route through here

Options for run/wrap:  --patience N   minutes to wait for lanes (default 30)
                       --no-wait      if lanes are busy, step back (exit 75)

Everything lives beside this file: fleet.json (lanes, bots, queue),
locks/ (active holds), ledger.jsonl (the record). Pure stdlib.
"""

import json
import os
import subprocess
import sys
import time
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET_PATH = os.path.join(HERE, "fleet.json")
LOCKS_DIR = os.path.join(HERE, "locks")
LEDGER_PATH = os.path.join(HERE, "ledger.jsonl")
WIRING_DIR = os.path.join(HERE, "wiring")

POLL_SECONDS = 5
DEFAULT_PATIENCE_MIN = 30
LINE = "─" * 62


# ---------------------------------------------------------------- fleet file

def load_fleet():
    with open(FLEET_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_fleet(fleet):
    tmp = FLEET_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fleet, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, FLEET_PATH)


def now():
    return dt.datetime.now()


def iso(t):
    return t.strftime("%Y-%m-%d %H:%M:%S")


def ledger_append(entry):
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ledger_lines():
    if not os.path.exists(LEDGER_PATH):
        return []
    with open(LEDGER_PATH, encoding="utf-8") as f:
        out = []
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out


# ---------------------------------------------------------------- lane holds

def lane_file(lane):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in lane)
    return os.path.join(LOCKS_DIR, safe + ".json")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def read_hold(lane):
    """The current hold on a lane, or None. Quietly clears holds whose
    process has already finished (nothing ominous — just tidying up after a
    bot that stopped without releasing, e.g. a power cut mid-run)."""
    path = lane_file(lane)
    try:
        with open(path, encoding="utf-8") as f:
            hold = json.load(f)
    except (OSError, ValueError):
        return None
    if not pid_alive(hold.get("pid")):
        try:
            os.remove(path)
        except OSError:
            pass
        ledger_append({"event": "tidied_hold", "lane": lane,
                       "was": hold.get("bot"), "ts": iso(now())})
        return None
    return hold


def try_hold(lane, bot):
    """Atomically take one lane. True if ours; otherwise the current holder."""
    holder = read_hold(lane)
    if holder:
        return holder
    path = lane_file(lane)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return read_hold(lane) or {"bot": "?", "pid": 0}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"lane": lane, "bot": bot, "pid": os.getpid(),
                   "since": iso(now())}, f)
    return True


def release_hold(lane):
    try:
        os.remove(lane_file(lane))
    except OSError:
        pass


def active_holds(fleet):
    out = []
    for lane in fleet.get("lanes", {}):
        hold = read_hold(lane)
        if hold:
            out.append(hold)
    # Also any wrap-created lanes not in fleet.json
    if os.path.isdir(LOCKS_DIR):
        known = {lane_file(h["lane"]) for h in out}
        for name in os.listdir(LOCKS_DIR):
            path = os.path.join(LOCKS_DIR, name)
            if path in known or not name.endswith(".json"):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    hold = json.load(f)
            except (OSError, ValueError):
                continue
            if pid_alive(hold.get("pid")):
                out.append(hold)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return out


def cooling_until(fleet, lane):
    """If the lane keeps a courtesy gap and was used recently, when it
    reopens. None means the lane is ready now."""
    gap = fleet.get("lanes", {}).get(lane, {}).get("gap_minutes", 0)
    if not gap:
        return None
    for entry in reversed(ledger_lines()):
        if lane in entry.get("lanes", []) and entry.get("end"):
            end = dt.datetime.strptime(entry["end"], "%Y-%m-%d %H:%M:%S")
            reopen = end + dt.timedelta(minutes=gap)
            return reopen if reopen > now() else None
    return None


def acquire(fleet, bot, lanes, patience_min, wait=True):
    """Take every lane (sorted, so two bots can never deadlock waiting on
    each other). Returns True, or False if patience ran out / --no-wait."""
    lanes = sorted(set(lanes))
    if not lanes:
        return True
    deadline = now() + dt.timedelta(minutes=patience_min)
    said = 0
    while True:
        blockers = []
        for lane in lanes:
            cool = cooling_until(fleet, lane)
            if cool:
                blockers.append(f"{lane} reopens {cool.strftime('%H:%M')}"
                                " (courtesy gap)")
                continue
            hold = read_hold(lane)
            if hold:
                blockers.append(f"{lane} held by {hold['bot']}"
                                f" since {hold['since'][11:16]}")
        if not blockers:
            taken = []
            ok = True
            for lane in lanes:
                if try_hold(lane, bot) is True:
                    taken.append(lane)
                else:
                    ok = False
                    break
            if ok:
                return True
            for lane in taken:        # someone slipped in; step back, retry
                release_hold(lane)
        else:
            if not wait:
                print(f"  {bot}: lanes busy — {'; '.join(blockers)}")
                return False
            if time.time() - said > 30:
                print(f"  {bot} waiting for green light: {'; '.join(blockers)}")
                said = time.time()
        if now() >= deadline:
            print(f"  {bot}: patience ran out after {patience_min} min "
                  f"({'; '.join(blockers) or 'lane race'}). Try again later.")
            return False
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- running

def run_through_lanes(fleet, bot, lanes, command, patience_min, wait=True):
    if not acquire(fleet, bot, lanes, patience_min, wait):
        return 75  # EX_TEMPFAIL — lanes busy, come back later
    start = now()
    try:
        rc = subprocess.run(command).returncode
    except FileNotFoundError as e:
        print(f"  Could not start {bot}: {e}")
        rc = 127
    finally:
        for lane in lanes:
            release_hold(lane)
    ledger_append({"bot": bot, "lanes": sorted(set(lanes)),
                   "start": iso(start), "end": iso(now()), "rc": rc})
    return rc


def run_bot(name, patience_min=DEFAULT_PATIENCE_MIN, wait=True):
    fleet = load_fleet()
    bot = fleet.get("bots", {}).get(name)
    if not bot:
        print(f"No bot named '{name}' in the fleet. tower.py timetable lists them.")
        return 64
    if not bot.get("command"):
        print(f"'{name}' lives in the cloud (a Claude scheduled task) — it is "
              "on the timetable for awareness, but the tower cannot start it.")
        return 64
    print(f"🗼 {name} → lanes: {', '.join(bot.get('lanes', [])) or '(open road)'}")
    rc = run_through_lanes(fleet, name, bot.get("lanes", []),
                           bot["command"], patience_min, wait)
    if rc == 0:
        print(f"  {name} landed clean.")
    elif rc != 75:
        print(f"  {name} finished with rc={rc} — its own log has the story.")
    return rc


def wrap(lane_csv, command, patience_min=DEFAULT_PATIENCE_MIN, wait=True):
    fleet = load_fleet()
    lanes = [l.strip() for l in lane_csv.split(",") if l.strip()]
    name = "wrap:" + os.path.basename(command[0] if command else "?")
    return run_through_lanes(fleet, name, lanes, command, patience_min, wait)


# ---------------------------------------------------------------- the queue

def parse_when(text):
    """'21:30' (today, or tomorrow if already past) or '2026-08-05 21:30'."""
    text = text.strip()
    try:
        t = dt.datetime.strptime(text, "%Y-%m-%d %H:%M")
        return t
    except ValueError:
        pass
    t = dt.datetime.strptime(text, "%H:%M")
    cand = now().replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    if cand <= now():
        cand += dt.timedelta(days=1)
    return cand


def queue_add(fleet, when, bot_name):
    fleet.setdefault("queue", []).append(
        {"when": when.strftime("%Y-%m-%d %H:%M"), "bot": bot_name})
    fleet["queue"].sort(key=lambda q: q["when"])
    save_fleet(fleet)


def run_due(quiet=True):
    """Roll every queued push whose time has come. Each item leaves the queue
    BEFORE it runs: if the machine dies mid-push, that push stays un-sent
    rather than ever going out twice."""
    fleet = load_fleet()
    due, later = [], []
    for q in fleet.get("queue", []):
        (due if q["when"] <= now().strftime("%Y-%m-%d %H:%M") else later).append(q)
    if not due:
        if not quiet:
            print("Queue is quiet — nothing due.")
        return 0
    fleet["queue"] = later
    save_fleet(fleet)
    worst = 0
    for q in due:
        print(f"🗼 queued push due ({q['when']}): {q['bot']}")
        rc = run_bot(q["bot"])
        worst = max(worst, rc)
    return worst


# ---------------------------------------------------------------- displays

def show_board():
    fleet = load_fleet()
    print(f"\n🗼 BOT TOWER — {iso(now())}")
    print(LINE)
    holds = active_holds(fleet)
    if holds:
        print("On the road now:")
        for h in holds:
            print(f"   {h['lane']:<18} {h['bot']}  (since {h['since'][11:16]})")
    else:
        print("Clear skies — no lane is held.")
    cooling = []
    for lane in fleet.get("lanes", {}):
        cool = cooling_until(fleet, lane)
        if cool:
            cooling.append(f"   {lane:<18} reopens {cool.strftime('%H:%M')}")
    if cooling:
        print("\nCourtesy gaps in effect:")
        print("\n".join(cooling))
    residents = [n for n, b in fleet.get("bots", {}).items()
                 if b.get("schedule", {}).get("resident")]
    if residents:
        print(f"\nAlways-on residents (outside the tower for now): "
              f"{', '.join(residents)}")
    queue = fleet.get("queue", [])
    if queue:
        print("\nQueued pushes:")
        for q in queue:
            print(f"   {q['when']}  {q['bot']}")
    print(LINE)


def schedule_words(sched):
    if sched.get("resident"):
        return "always on"
    if sched.get("at"):
        return "daily at " + ", ".join(sched["at"])
    if sched.get("every_minutes"):
        m = sched["every_minutes"]
        return f"every {m // 60}h" if m % 60 == 0 else f"every {m}m"
    return "on request"


def show_timetable():
    fleet = load_fleet()
    bots = fleet.get("bots", {})
    print(f"\n🗼 TIMETABLE — the fleet's day")
    print(LINE)

    def sort_key(item):
        s = item[1].get("schedule", {})
        if s.get("at"):
            return (0, s["at"][0])
        if s.get("every_minutes"):
            return (1, f"{s['every_minutes']:06d}")
        return (2, item[0])

    for name, bot in sorted(bots.items(), key=sort_key):
        sched = schedule_words(bot.get("schedule", {}))
        lanes = ", ".join(bot.get("lanes", [])) or "—"
        cloud = "  ☁" if not bot.get("command") else ""
        print(f"   {sched:<18} {name:<20} lanes: {lanes}{cloud}")
    print("\n   ☁ = runs as a Claude scheduled task in the cloud (shown for awareness)")

    shared = {}
    for name, bot in bots.items():
        for lane in bot.get("lanes", []):
            shared.setdefault(lane, []).append(name)
    crowded = {l: n for l, n in shared.items() if len(n) > 1}
    if crowded:
        print("\nShared lanes (where the tower earns its keep):")
        for lane, names in sorted(crowded.items()):
            gap = fleet["lanes"].get(lane, {}).get("gap_minutes", 0)
            gap_note = f", {gap}min courtesy gap" if gap else ""
            print(f"   {lane:<18} {', '.join(sorted(names))}{gap_note}")
    print(LINE)


def show_ledger(n=14):
    entries = ledger_lines()[-n:]
    print(f"\n🗼 LEDGER — last {len(entries)} entries")
    print(LINE)
    if not entries:
        print("Fresh book — nothing recorded yet.")
    for e in entries:
        if e.get("event") == "tidied_hold":
            print(f"   {e['ts']}  tidied a leftover hold on {e['lane']} "
                  f"(was {e.get('was')})")
            continue
        mark = "✓" if e.get("rc") == 0 else f"rc={e.get('rc')}"
        lanes = ", ".join(e.get("lanes", [])) or "open road"
        print(f"   {e.get('start')} → {e.get('end', '?')[11:16]}  "
              f"{e.get('bot'):<20} [{lanes}]  {mark}")
    print(LINE)


# ---------------------------------------------------------------- wiring

TICK_LABEL = "haus.bottower.tick"

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>{tower}</string>
        {args}
    </array>
    {schedule}
    <!-- launchd hands a job /usr/bin:/bin:/usr/sbin:/sbin and nothing else, so
         anything from Homebrew is simply absent. That gap cost three lanes on
         2026-08-19: portal-publish died on `npx`, lannawiki-publish on
         `pdftoppm`, search-core-sync on `node`. The crawler's own plist had
         always carried this line, which is why it was the one job still
         reaching the web. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StandardOutPath</key><string>/tmp/bottower-{short}.log</string>
    <key>StandardErrorPath</key><string>/tmp/bottower-{short}.log</string>
</dict>
</plist>
"""


def plist_schedule(sched):
    if sched.get("at"):
        h, m = sched["at"][0].split(":")
        return ("<key>StartCalendarInterval</key>\n    <dict>\n"
                f"        <key>Hour</key><integer>{int(h)}</integer>\n"
                f"        <key>Minute</key><integer>{int(m)}</integer>\n"
                "    </dict>")
    if sched.get("every_minutes"):
        return (f"<key>StartInterval</key>"
                f"<integer>{sched['every_minutes'] * 60}</integer>")
    return ""


def write_wiring():
    fleet = load_fleet()
    tower = os.path.abspath(__file__)
    written = []
    rewire = {  # bot name -> the launchd label it replaces
        "lannawiki-publish": "com.nanobotco.lannawiki.refresh",
        "wichaa-archive-drip": "com.wichaa.archive-drip",
        "defiant-nightly": "haus.defiant.nightly",
        "defiant-wayback": "haus.defiant.wayback",
    }
    for name, label in rewire.items():
        bot = fleet["bots"].get(name)
        if not bot:
            continue
        args = (f"<string>run</string>\n        <string>{name}</string>\n"
                "        <string>--patience</string>\n        <string>90</string>")
        body = PLIST_TEMPLATE.format(
            label=label, tower=tower, args=args,
            schedule=plist_schedule(bot.get("schedule", {})),
            short=name)
        path = os.path.join(WIRING_DIR, label + ".plist")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        written.append((label, path))

    tick = PLIST_TEMPLATE.format(
        label=TICK_LABEL, tower=tower,
        args="<string>due</string>",
        schedule="<key>StartInterval</key><integer>300</integer>",
        short="tick")
    tick_path = os.path.join(WIRING_DIR, TICK_LABEL + ".plist")
    with open(tick_path, "w", encoding="utf-8") as f:
        f.write(tick)
    written.append((TICK_LABEL, tick_path))

    print(f"\n🗼 WIRING — {len(written)} plists written to wiring/")
    print(LINE)
    print("These route the same schedules through the tower. Same labels as")
    print("today's LaunchAgents, so installing them REPLACES the old wiring")
    print("(the tick one is new — it rolls queued pushes every 5 minutes).")
    print("\nTo install (one bot at a time is fine — start with one):\n")
    for label, path in written:
        print(f'  launchctl unload ~/Library/LaunchAgents/{label}.plist 2>/dev/null')
        print(f'  cp "{path}" ~/Library/LaunchAgents/')
        print(f'  launchctl load ~/Library/LaunchAgents/{label}.plist\n')
    print("The always-on crawler needs no plist change. To bring its publish")
    print("step into the github-push lane, edit forever.sh step [8/8] to:\n")
    print(f'  /usr/bin/python3 "{tower}" wrap github-push,catalog.db -- \\')
    print('      bash publishing/publish_site.sh')
    print(LINE)


# ---------------------------------------------------------------- the menu

def ask(prompt="  > "):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "0"


def menu_run():
    fleet = load_fleet()
    runnable = [n for n, b in fleet["bots"].items()
                if b.get("command") and not b.get("schedule", {}).get("resident")]
    print("\nWhich bot rolls now?\n")
    for i, name in enumerate(runnable, 1):
        lanes = ", ".join(fleet["bots"][name].get("lanes", [])) or "open road"
        print(f"   {i}  {name:<22} [{lanes}]")
    print("   0  back")
    choice = ask()
    if choice.isdigit() and 1 <= int(choice) <= len(runnable):
        run_bot(runnable[int(choice) - 1])


def menu_queue():
    fleet = load_fleet()
    named = [n for n, b in fleet["bots"].items()
             if b.get("command") and not b.get("schedule", {}).get("resident")]
    print("\nSchedule a push — which bot?\n")
    for i, name in enumerate(named, 1):
        print(f"   {i}  {name}")
    print("   0  back")
    choice = ask()
    if not (choice.isdigit() and 1 <= int(choice) <= len(named)):
        return
    bot = named[int(choice) - 1]
    print("\nWhen? (like 21:30 for tonight, or 2026-08-05 09:00)")
    text = ask()
    try:
        when = parse_when(text)
    except ValueError:
        print("  Could not read that time — nothing scheduled.")
        return
    queue_add(fleet, when, bot)
    print(f"  ✓ {bot} will roll at {when.strftime('%Y-%m-%d %H:%M')} "
          "(the 5-minute tick carries it out — see wiring).")


def menu_release():
    fleet = load_fleet()
    holds = active_holds(fleet)
    if not holds:
        print("\nNo holds to clear — the road is open.")
        return
    print("\nClear which hold? (only if you are sure the bot is truly done)\n")
    for i, h in enumerate(holds, 1):
        print(f"   {i}  {h['lane']:<18} held by {h['bot']} since {h['since']}")
    print("   0  back")
    choice = ask()
    if choice.isdigit() and 1 <= int(choice) <= len(holds):
        release_hold(holds[int(choice) - 1]["lane"])
        print("  ✓ cleared.")


def menu():
    while True:
        print("""
🗼  BOT TOWER — clear lanes, good timing
════════════════════════════════════════

   1   board        who is on the road right now
   2   run a bot    through its lanes, green light or wait
   3   timetable    the fleet's whole day, shared lanes shown
   4   time a push  queue a bot for a chosen moment
   5   ledger       recent runs
   6   clear a hold
   7   wiring       launchd plists that route through the tower

   0   leave the tower
""")
        choice = ask("  choose > ")
        if choice == "1":
            show_board()
        elif choice == "2":
            menu_run()
        elif choice == "3":
            show_timetable()
        elif choice == "4":
            menu_queue()
        elif choice == "5":
            show_ledger()
        elif choice == "6":
            menu_release()
        elif choice == "7":
            write_wiring()
        elif choice == "0":
            print("Tower out. Safe travels. 🗼\n")
            return
        if choice != "0":
            ask("\n  (Enter to continue) ")


# ---------------------------------------------------------------- cli

def main(argv):
    os.makedirs(LOCKS_DIR, exist_ok=True)
    if not argv:
        menu()
        return 0
    cmd, rest = argv[0], argv[1:]
    patience = DEFAULT_PATIENCE_MIN
    wait = True
    if "--patience" in rest:
        i = rest.index("--patience")
        patience = int(rest[i + 1])
        rest = rest[:i] + rest[i + 2:]
    if "--no-wait" in rest:
        wait = False
        rest.remove("--no-wait")

    if cmd == "run" and rest:
        return run_bot(rest[0], patience, wait)
    if cmd == "wrap" and "--" in rest:
        i = rest.index("--")
        return wrap(",".join(rest[:i]), rest[i + 1:], patience, wait)
    if cmd == "due":
        return run_due(quiet=True)
    if cmd == "board":
        show_board()
        return 0
    if cmd == "timetable":
        show_timetable()
        return 0
    if cmd == "ledger":
        show_ledger()
        return 0
    if cmd == "release" and rest:
        release_hold(rest[0])
        print(f"✓ cleared any hold on {rest[0]}.")
        return 0
    if cmd == "wiring":
        write_wiring()
        return 0
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
