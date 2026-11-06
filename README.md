# tempo

A single-file command line tool for Tempo on Jira Cloud: see how many hours you logged against how many you owe, and log time in a few keystrokes without opening a browser.

```
$ tempo
Week 44 (26-10 → 01-11-2026)
Logged 25h 30m / required 40h 00m  (-14h 30m)

Per day:
  Mon 26-10  ████████████
  Tue 27-10  ███████▌░░░░   -3h 00m
› Wed 28-10  ████▌░░░░░░░   -5h 00m
  Thu 29-10  ░░░░░░░░░░░░   -8h 00m
Per ticket:
  ABC-2         Feature work                              14h 30m
  ABC-1         Daily standup                             11h 00m
```

Green means that day is complete, red means a past day fell short, and the bold `›` row is today. The bar fills against *that day's* required hours from your Tempo schedule, so a 4-hour contract day fills up at 4 hours. Colors switch off automatically when output is piped, and honor `NO_COLOR`.

## Requirements

- Python 3.7 or newer. macOS and most Linux distributions already ship one; check with `python3 --version`.
- No packages to install. The script only imports from the standard library, so there is no virtualenv, no `pip install`, no lockfile.
- A Jira Cloud account with Tempo, plus two API tokens (see Setup).
- macOS, Linux, or WSL for the arrow-key pickers. Plain Windows works too, but falls back to numbered prompts because it has no `termios`.

## Install

```bash
git clone https://github.com/77vrtynz54-rgb/tempo-logger.git
cd tempo-logger
chmod +x tempo.py
mkdir -p ~/.local/bin
ln -s "$PWD/tempo.py" ~/.local/bin/tempo
```

Make sure `~/.local/bin` is on your `PATH` (add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` if `which tempo` comes up empty).

**Windows:** skip the `chmod` and `ln` lines and run the script directly, e.g. `python tempo.py init` and `python tempo.py status`. Everywhere this README says `tempo`, use `python tempo.py` instead (or make a `tempo.bat` that calls it).

## Setup

```bash
tempo init
```

It asks for four things and then verifies them by calling Jira as you:

1. **Jira URL**, e.g. `https://yourcompany.atlassian.net`
2. **Jira email**, the address you log in with
3. **Jira API token** from https://id.atlassian.com/manage-profile/security/api-tokens
4. **Tempo API token** from Tempo → Settings → Data Access → API Integration. Choose full access, or a custom scope that can read and write worklogs and read user schedules. Tempo tokens expire after 30 days by default, so pick a longer expiry when you create it.

Both tokens are stored in `~/.config/tempo-logger/config.json` with permissions `0600` (readable only by you). Re-running `tempo init` keeps your existing values if you press Enter, and never echoes the tokens back. Point `TEMPO_LOGGER_CONFIG` at another path if you want the config somewhere else.

**Tempo region.** The tool talks to `https://api.tempo.io` by default, which works everywhere. Tempo also offers regional hosts, `https://api.eu.tempo.io` (Europe) and `https://api.us.tempo.io` (Americas). `tempo init` does not ask for this. To use one, or if you get `Error 401` or `Error 404` from Tempo with a fresh token, add a `tempo_url` key to the config file:

```json
{
  "jira_url": "https://yourcompany.atlassian.net",
  "tempo_url": "https://api.eu.tempo.io",
  "...": "other keys unchanged"
}
```

## Daily use

**See where you stand.** `tempo` on its own shows the current week.

```bash
tempo                  # this week
tempo status day       # today, with the description of every worklog
tempo status month     # this month
tempo status week -p   # last week  (-p 3 = three weeks back)
```

**Look up one specific day** by counting back, by weekday, or by date:

```bash
tempo status day -2       # two days ago
tempo status day wed      # the most recent Wednesday
tempo status day 28-10    # 28 October (nearest year); also 28-10-2026 or 2026-10-28
tempo status 28-10        # same thing — a date on its own implies 'day'
tempo status week 28-10   # the week containing that date (works for month too)
```

**Log time in one line** when you know the ticket and the hours:

```bash
tempo log ABC-123 1h30m
tempo log ABC-123 2h -d yesterday -m "Pair review"
tempo log standup 15m          # 'standup' is a favorite alias
tempo log 1 15m                # or the favorite's position in the list
```

Durations accept `2h`, `1h30m`, `90m`, `1.5`, and `1:30`. Dates accept `today`, `yesterday` (or `y`), a weekday name like `mon` (meaning the most recent one), `2026-10-01`, or a day-month like `28-10` / `28-10-2026` (`/` and `.` work as separators too). A bare day-month picks the year closest to today, so `28-12` in January means last December.

**Or run it interactively** with `tempo log` and no arguments, which walks three steps:

1. *Ticket* — arrow keys through your favorites, type to filter them, or press Enter on the "Search Jira" row to search all issues by text.
2. *Duration* — e.g. `1h30m`.
3. *Date* — the last 10 working days, newest first, today selected. Weekends are skipped but you can still type `sat` or a date to log on one.

Esc backs out of a search, Esc again cancels.

**Favorites** are the tickets you book against constantly. Give one an alias and a default description and logging takes seconds:

```bash
tempo fav add ABC-123 standup -m "Daily standup"
tempo fav                       # list them
tempo fav rm standup
```

**Find a ticket** without leaving the terminal: `tempo search login bug`.

`tempo help` prints the full reference for every command, `tempo help log` for one.

## How it works

The whole tool is one ~800-line Python file with no dependencies, so it is easy to read and easy to audit before you trust it with a token.

It talks to two APIs over plain `urllib`:

- **Jira Cloud REST v3**, authenticated with HTTP Basic (your email + Jira API token), for looking up issue keys, summaries, your account id, and for text search via JQL.
- **Tempo REST v4**, authenticated with a bearer token, for reading worklogs (`/4/worklogs/user/{accountId}`), reading your working schedule (`/4/user-schedule`, which is where "required hours" comes from, including part-time days and holidays), and creating worklogs (`POST /4/worklogs`).

Two separate tokens are needed because Tempo worklogs live outside Jira's own issue data, and each product issues its own credential.

A logged entry is posted with a start time of 09:00 and the description you give it, or the favorite's default, or `Working on issue ABC-123`. The status screen pulls your worklogs and your schedule for the period, sums them per day and per ticket, and resolves issue ids to keys in batches of 100 so a busy month stays at a handful of requests.

The interactive pickers are hand-rolled: the terminal is put in cbreak mode, keystrokes are read raw, and the list is redrawn in place with ANSI escapes. That is why there is no `curses`, no `rich`, and nothing to install. If stdin or stdout is not a terminal (a script, a pipe, a CI job), both pickers fall back to numbered prompts automatically.

## Can an AI coding agent log hours with this?

Yes, and it needs no special integration. Any agent that can run shell commands (Claude Code, Cursor, an editor terminal) can call the CLI directly:

```bash
tempo log ABC-123 1h30m -m "Implemented the export endpoint"
tempo status week
```

A few things make this work well in practice:

- **Give the ticket and duration as arguments.** The interactive pickers need a real terminal; agents usually do not have one. With both arguments present no prompt ever appears. If an agent does call bare `tempo log`, it gets the numbered fallback rather than a hang.
- **The agent never sees your tokens.** They stay in `~/.config/tempo-logger/config.json`, read by the script at runtime. Nothing is passed on the command line, so no credential lands in the agent's transcript. Keep it that way: do not ask an agent to print or edit that file.
- **`tempo log` writes to Tempo for real.** It is not reversible from the CLI — there is no `tempo rm`, so a mistaken entry has to be deleted in the Tempo UI. Treat it like `git push`: worth a confirmation before the agent runs it. In Claude Code you can allowlist the safe reads and leave writes prompting, in `.claude/settings.json`:

  ```json
  {
    "permissions": {
      "allow": ["Bash(tempo status:*)", "Bash(tempo search:*)", "Bash(tempo fav)"],
      "ask": ["Bash(tempo log:*)"]
    }
  }
  ```

- **It reads back what it wrote.** `tempo status day` after a log confirms the entry landed, which gives an agent a cheap way to verify its own action.
- **Exit codes are usable.** Non-zero on an API error or a bad argument, 130 on cancel, with the Jira or Tempo error body printed to stderr.

A natural use is at the end of a working session: the agent knows which ticket it worked on and roughly how long it took, and can propose `tempo log ABC-123 2h -m "<what it actually did>"` for you to approve.

## Troubleshooting

**`No config found`** — run `tempo init`.

**`Error 401`** — the token is wrong, expired, or revoked. Jira tokens expire after a year by default, Tempo tokens after 30 days. Re-run `tempo init`. If a brand-new Tempo token still fails, set `tempo_url` to your region (see Setup).

**`Error 404` on a worklog** — your Tempo token belongs to a different Tempo instance than the Jira URL you configured, or it is for another region. Check `tempo_url` (see Setup).

**Required hours are 0 everywhere** — Tempo has no work schedule for your account; ask whoever administers Tempo to assign you one.

**Boxes instead of bars** — your terminal font lacks the block glyphs. `NO_COLOR=1 tempo` keeps the layout but drops the color; the bar characters themselves are in every modern monospace font, so a font change usually fixes it.

## License

Licensed under the GNU General Public License, version 3 (GPL-3.0). See [LICENSE](LICENSE).
