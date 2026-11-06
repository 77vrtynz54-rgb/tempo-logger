#!/usr/bin/env python3
"""Minimal Tempo (Jira Cloud) CLI: show logged vs required hours, log time, search tickets."""
import argparse
import base64
import calendar
import datetime
import getpass
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    import termios
    import tty
except ImportError:  # Windows: no raw terminal, fall back to numbered prompts
    termios = tty = None

CONFIG_PATH = os.environ.get("TEMPO_LOGGER_CONFIG") or os.path.expanduser(
    "~/.config/tempo-logger/config.json"
)
KEY_RE = r"[A-Z][A-Z0-9_]+-\d+"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- config / http


def read_config():
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_config():
    cfg = read_config()
    if cfg is None:
        die("No config found at %s. Run: tempo init" % CONFIG_PATH)
    cfg["jira_url"] = cfg["jira_url"].rstrip("/")
    cfg["tempo_url"] = cfg.get("tempo_url", "https://api.tempo.io").rstrip("/")
    cfg.setdefault("favorites", [])
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(CONFIG_PATH, 0o600)


def http(method, url, headers, params=None, body=None):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    headers = dict(headers, Accept="application/json")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:300]
        die("Error %s %s %s: %s" % (e.code, method, url, text))
    except urllib.error.URLError as e:
        die("Network error: %s" % e.reason)
    return json.loads(raw) if raw.strip() else {}


def tempo(cfg, method, path, params=None, body=None):
    url = path if path.startswith("http") else cfg["tempo_url"] + path
    headers = {"Authorization": "Bearer " + cfg["tempo_token"]}
    return http(method, url, headers, params, body)


def jira(cfg, method, path, params=None, body=None):
    creds = "%s:%s" % (cfg["jira_email"], cfg["jira_token"])
    headers = {"Authorization": "Basic " + base64.b64encode(creds.encode()).decode()}
    return http(method, cfg["jira_url"] + path, headers, params, body)


def myself(cfg):
    return jira(cfg, "GET", "/rest/api/3/myself")


def account_id(cfg):
    return myself(cfg)["accountId"]


# ---------------------------------------------------------------- helpers


def parse_duration(s):
    t = (s or "").strip().lower().replace(" ", "").replace(",", ".")
    err = ValueError("Invalid duration: %s (use e.g. 2h, 1h30m, 1.5, 1:30)" % s)
    hours, minutes = 0.0, 0
    m = re.fullmatch(r"(\d+):(\d{1,2})", t)
    if m:
        hours, minutes = float(m.group(1)), int(m.group(2))
    elif re.fullmatch(r"\d+(?:\.\d+)?", t):
        hours = float(t)
    else:
        m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+)m)?", t)
        if not m or not (m.group(1) or m.group(2)):
            raise err
        hours = float(m.group(1) or 0)
        minutes = int(m.group(2) or 0)
    secs = round(hours * 3600 + minutes * 60)
    if secs <= 0:
        raise err
    return secs


def nearest_year(day, month, today):
    """The d-m date closest to today, so '28-12' in January means last December."""
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            cand = datetime.date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((cand - today).days) < abs((best - today).days):
            best = cand
    return best


def parse_date(s, today=None):
    today = today or datetime.date.today()
    t = (s or "").strip().lower()
    if t in ("", "today"):
        return today
    if t in ("yesterday", "y"):
        return today - datetime.timedelta(days=1)
    if t in WEEKDAYS:
        delta = (today.weekday() - WEEKDAYS.index(t)) % 7
        return today - datetime.timedelta(days=delta)
    try:
        return datetime.date.fromisoformat(t)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?", t)  # 28-10, 28-10-2026
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(3):
            year = int(m.group(3))
            try:
                return datetime.date(year + 2000 if year < 100 else year, month, day)
            except ValueError:
                raise ValueError("Invalid date: %s" % s)
        date = nearest_year(day, month, today)
        if date:
            return date
    raise ValueError("Invalid date: %s" % s)


def fmt(seconds):
    sign = "-" if seconds < 0 else ""
    h, m = divmod(round(abs(seconds) / 60), 60)
    return "%s%dh %02dm" % (sign, h, m)


def fmt_diff(seconds):
    return ("+" if seconds >= 0 else "") + fmt(seconds)


GREEN, RED, YELLOW, BOLD = "32", "31", "33", "1"


def paint(text, *codes):
    """Wrap text in ANSI codes, unless output is piped or NO_COLOR is set."""
    if not codes or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return text
    if not sys.stdout.isatty():
        return text
    return "\x1b[%sm%s\x1b[0m" % (";".join(codes), text)


BAR_WIDTH = 12


def bar(logged, required, width=BAR_WIDTH):
    """Share of the day's required hours, in half-block steps; ▸ marks overtime."""
    if required <= 0 or logged >= required:
        return "█" * width + ("▸" if logged > required else " ")
    halves = int(logged * width * 2 / required) or (1 if logged > 0 else 0)
    full, half = divmod(halves, 2)
    return "█" * full + "▌" * half + "░" * (width - full - half) + " "


def day_colors(date, today, logged, required):
    """today → bold (green when done, yellow while short); past → green/red; future → plain."""
    done = logged >= required
    if date == today:
        return (BOLD, GREEN) if done else (BOLD, YELLOW)
    if done:
        return (GREEN,)
    return () if date > today else (RED,)


def resolve_ticket(cfg, arg):
    favs = cfg["favorites"]
    a = arg.strip()
    if a.isdigit() and 1 <= int(a) <= len(favs):
        fav = favs[int(a) - 1]
        return fav["key"].upper(), fav
    for fav in favs:
        if a.lower() in (fav.get("alias", "").lower(), fav["key"].lower()):
            return fav["key"].upper(), fav
    if re.fullmatch(KEY_RE, a.upper()):
        return a.upper(), None
    raise ValueError("Unknown ticket or alias: %s" % arg)


def get_issue(cfg, key):
    data = jira(cfg, "GET", "/rest/api/3/issue/%s" % urllib.parse.quote(key), {"fields": "summary"})
    return {"id": int(data["id"]), "key": data["key"], "summary": data["fields"]["summary"]}


def issue_keys(cfg, ids):
    ids = sorted(ids)
    out = {}
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        params = {
            "jql": "id in (%s)" % ",".join(str(x) for x in batch),
            "fields": "summary",
            "maxResults": 100,
        }
        data = jira(cfg, "GET", "/rest/api/3/search/jql", params)
        for issue in data.get("issues", []):
            out[int(issue["id"])] = (issue["key"], issue["fields"]["summary"])
    return out


def search_issues(cfg, text, limit=20):
    text = text.strip()
    if re.fullmatch(KEY_RE, text.upper()):
        jql = "key = %s" % text.upper()
    else:
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        jql = 'text ~ "%s" ORDER BY updated DESC' % escaped
    data = jira(cfg, "GET", "/rest/api/3/search/jql",
                {"jql": jql, "fields": "summary", "maxResults": limit})
    return [{"key": i["key"], "summary": i["fields"]["summary"]} for i in data.get("issues", [])]


def print_issues(issues):
    for i, issue in enumerate(issues, 1):
        print("%2d) %-12s %s" % (i, issue["key"], issue["summary"]))


def print_favorites(cfg):
    favs = cfg["favorites"]
    if not favs:
        print("No favorites. Add one: tempo fav add ABC-123 [alias]")
        return
    for i, fav in enumerate(favs, 1):
        print("%2d) %-12s %-12s %s" % (i, fav["key"], fav.get("alias", ""), fav.get("name", "")))


# ---------------------------------------------------------------- status


PERIODS = ("day", "week", "month")


def resolve_period(period, when, today):
    """'day|week|month' with an optional date, or just a date (which implies 'day')."""
    if period in PERIODS:
        return period, parse_date(when, today) if when else None
    if when is not None:
        raise ValueError("Invalid period: %s (use day, week or month)" % period)
    return "day", parse_date(period, today)


def period_range(period, prev, today, anchor=None):
    base = anchor or today
    if period == "day":
        d = base - datetime.timedelta(days=prev)
        label = {0: " (today)", 1: " (yesterday)"}.get((today - d).days, "")
        return d, d, d.strftime("%a %Y-%m-%d") + label
    if period == "week":
        start = base - datetime.timedelta(days=base.weekday() + 7 * prev)
        end = start + datetime.timedelta(days=6)
        title = "Week %d (%s → %s)" % (
            start.isocalendar()[1], start.strftime("%d-%m"), end.strftime("%d-%m-%Y"))
        return start, end, title
    year, month = base.year, base.month - prev
    while month < 1:
        month += 12
        year -= 1
    start = datetime.date(year, month, 1)
    end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    return start, end, start.strftime("%B %Y")


def fetch_worklogs(cfg, acc, start, end):
    results = []
    path = "/4/worklogs/user/%s" % acc
    params = {"from": start.isoformat(), "to": end.isoformat(), "limit": 1000}
    while path:
        data = tempo(cfg, "GET", path, params)
        results.extend(data.get("results", []))
        path = (data.get("metadata") or {}).get("next")
        params = None  # next URL already carries the query
    return results


def cmd_status(args):
    cfg = load_config()
    today = datetime.date.today()
    period, anchor = resolve_period(args.period, args.when, today)
    start, end, title = period_range(period, args.prev, today, anchor)
    acc = account_id(cfg)
    worklogs = fetch_worklogs(cfg, acc, start, end)
    sched = tempo(cfg, "GET", "/4/user-schedule",
                  {"from": start.isoformat(), "to": end.isoformat()})
    required = {r["date"]: r.get("requiredSeconds", 0) for r in sched.get("results", [])}

    per_day, per_issue, descs = {}, {}, {}
    for w in worklogs:
        secs = w.get("timeSpentSeconds", 0)
        iid = int(w["issue"]["id"])
        per_day[w["startDate"]] = per_day.get(w["startDate"], 0) + secs
        per_issue[iid] = per_issue.get(iid, 0) + secs
        descs.setdefault(iid, []).append(w.get("description") or "")
    total = sum(per_issue.values())
    total_req = sum(required.values())

    print(paint(title, BOLD))
    print("Logged %s / required %s  (%s)" % (
        fmt(total), fmt(total_req),
        paint(fmt_diff(total - total_req), GREEN if total >= total_req else RED)))
    if not worklogs:
        print("No worklogs.")
        return
    print()

    if period != "day":
        print("Per day:")
        d = start
        while d <= end:
            iso = d.isoformat()
            logged, req = per_day.get(iso, 0), required.get(iso, 0)
            if logged or req:
                line = "%s%s  %s  %s" % (
                    "› " if d == today else "  ", d.strftime("%a %d-%m"),
                    bar(logged, req), fmt_diff(logged - req) if logged != req else "")
                print(paint(line.rstrip(), *day_colors(d, today, logged, req)))
            d += datetime.timedelta(days=1)

    keys = issue_keys(cfg, set(per_issue))
    print("Per ticket:")
    for iid, secs in sorted(per_issue.items(), key=lambda kv: -kv[1]):
        key, summary = keys.get(iid, ("#%d" % iid, ""))
        print("  %-12s  %-40s  %7s" % (key, summary[:40], fmt(secs)))
        if period == "day":
            for desc in descs[iid]:
                print("      - %s" % desc)


# ---------------------------------------------------------------- log


def ask(prompt, parse):
    while True:
        try:
            return parse(input(prompt))
        except ValueError as e:
            print(e)


def pick_interactive(cfg):
    print_favorites(cfg)
    while True:
        choice = input("Pick number/alias/key, or 's' to search: ").strip()
        if choice.lower() != "s":
            try:
                return resolve_ticket(cfg, choice)
            except ValueError as e:
                print(e)
                continue
        issues = search_issues(cfg, input("Search: "))
        if not issues:
            die("No matches.")
        print_issues(issues)

        def parse_pick(s):
            s = s.strip()
            if s.isdigit() and 1 <= int(s) <= len(issues):
                return issues[int(s) - 1]["key"]
            raise ValueError("Pick 1-%d" % len(issues))

        return ask("Pick number: ", parse_pick), None


def menu_loop(label, rows, st, hint, empty="", on_enter=None):
    """Arrow-key picker. rows() -> [(payload, text)]; returns the chosen payload.

    A row with payload None calls on_enter(redraw) instead of returning.
    Esc clears st["results"] when set, otherwise cancels.
    """
    fd = sys.stdin.fileno()
    out = sys.stdout

    def render(items):
        size = shutil.get_terminal_size()
        width, visible = size.columns - 1, max(3, min(10, size.lines - 4))
        cur = st["cursor"]
        top = min(max(0, cur - visible + 1), max(0, len(items) - visible))
        lines = []
        for i in range(top, min(len(items), top + visible)):
            text = (("› " if i == cur else "  ") + items[i][1])[:width]
            lines.append("\x1b[7m%s\x1b[0m" % text if i == cur else text)
        if not items:
            lines.append("  " + empty)
        lines.append("\x1b[2m%s\x1b[0m" % (st["status"] or hint)[:width])
        prompt = ("%s: %s" % (label, st["query"]))[:width]
        out.write("\r\x1b[J%s\n%s\x1b[%dA\r\x1b[%dC" % (
            prompt, "\n".join(lines), len(lines), len(prompt)))
        out.flush()

    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            items = rows()
            st["cursor"] = min(st["cursor"], max(0, len(items) - 1))
            render(items)
            key = os.read(fd, 64).decode("utf-8", "ignore")
            st["status"] = ""
            if key in ("\x1b[A", "\x1bOA", "\x10"):  # up, Ctrl-P
                st["cursor"] = max(0, st["cursor"] - 1)
            elif key in ("\x1b[B", "\x1bOB", "\x0e"):  # down, Ctrl-N
                st["cursor"] = min(max(0, len(items) - 1), st["cursor"] + 1)
            elif key in ("\r", "\n"):
                if not items:
                    continue
                payload = items[st["cursor"]][0]
                if payload is not None:
                    return payload
                if on_enter:
                    on_enter(lambda: render(items))
            elif key == "\x1b":  # Esc: leave search results, else cancel
                if st["results"] is None:
                    raise KeyboardInterrupt
                st["results"], st["cursor"] = None, 0
            elif key == "\x04":  # Ctrl-D
                raise EOFError
            elif key in ("\x7f", "\x08", "\x15"):  # backspace, Ctrl-U
                st["query"] = "" if key == "\x15" else st["query"][:-1]
                st["results"], st["cursor"] = None, 0
            elif key.isprintable():
                st["query"] += key
                st["results"], st["cursor"] = None, 0
    finally:
        out.write("\r\x1b[J")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def pick_menu(cfg):
    """Arrow-key picker: type to filter favorites, or pick the row that searches Jira."""
    favs = cfg["favorites"]
    st = {"query": "", "cursor": 0, "results": None, "status": ""}

    def rows():
        if st["results"] is not None:
            return [((i["key"], i["summary"]), "%-12s %s" % (i["key"], i["summary"]))
                    for i in st["results"]]
        q = st["query"].strip()
        words = q.lower().split()
        found = []
        for n, f in enumerate(favs, 1):
            row = ((f["key"].upper(), f.get("name", "")), "%2d) %-12s %-10s %s" % (
                n, f["key"], f.get("alias", ""), f.get("name", "")))
            hay = " ".join((f["key"], f.get("alias", ""), f.get("name", ""))).lower()
            if q == str(n):
                found.insert(0, row)
            elif all(w in hay for w in words):
                found.append(row)
        if q:
            found.append((None, 'Search Jira for "%s"' % q))
        return found

    def on_enter(redraw):
        st["status"] = "Searching Jira…"
        redraw()
        st["results"] = search_issues(cfg, st["query"]) or None
        st["cursor"] = 0
        st["status"] = "" if st["results"] else "No Jira matches."

    picked, summary = menu_loop(
        "Ticket", rows, st,
        hint="↑↓ move · type to filter · Enter select · Esc back/cancel",
        empty="No favorites. Type to search Jira.", on_enter=on_enter)
    print("Ticket: %s %s" % (picked, summary))
    return picked, next((f for f in favs if f["key"].upper() == picked), None)


# ---------------------------------------------------------------- date picking


def recent_days(today, n=10):
    """Today, then the previous working days (weekends only show up as today)."""
    days, d = [today], today
    while len(days) < n:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days


def date_label(d, today):
    name = {0: "today", 1: "yesterday"}.get((today - d).days, "")
    return "%-10s %s" % (name, d.strftime("%a %d-%m-%Y"))


def date_rows(query, today):
    """Recent days (newest first), filtered by query; a typed date is offered on top."""
    q = query.strip().lower()
    rows = [(d, date_label(d, today)) for d in recent_days(today)
            if not q or q in (date_label(d, today) + " " + d.isoformat()).lower()]
    if q:
        try:
            d = parse_date(q, today)
        except ValueError:
            pass
        else:
            rows = [r for r in rows if r[0] != d]
            rows.insert(0, (d, date_label(d, today)))
    return rows


def pick_date_menu(today=None):
    today = today or datetime.date.today()
    st = {"query": "", "cursor": 0, "results": None, "status": ""}
    date = menu_loop(
        "Date", lambda: date_rows(st["query"], today), st,
        hint="↑↓ move · type a date (yesterday, mon, 2026-10-01) · Enter select · Esc cancel",
        empty="No match. Type a date like 2026-10-01.")
    print("Date: %s" % date.strftime("%a %d-%m-%Y"))
    return date


def pick_date_interactive(today=None):
    today = today or datetime.date.today()
    days = recent_days(today)
    for i, d in enumerate(days, 1):
        print("%2d) %s" % (i, date_label(d, today)))

    def parse_pick(s):
        s = s.strip()
        if s.isdigit() and 1 <= int(s) <= len(days):
            return days[int(s) - 1]
        return parse_date(s, today)

    return ask("Date [today]: ", parse_pick)


def cmd_log(args):
    cfg = load_config()
    if (args.ticket is None) != (args.duration is None):
        die("Give both ticket and duration, or neither for interactive mode.")
    date = parse_date(args.date) if args.date else None
    if args.ticket is not None:
        key, fav = resolve_ticket(cfg, args.ticket)
        secs = parse_duration(args.duration)
        date = date or datetime.date.today()
        desc = args.message or (fav or {}).get("description") or "Working on issue %s" % key
    else:
        menu = bool(termios and sys.stdin.isatty() and sys.stdout.isatty())
        key, fav = pick_menu(cfg) if menu else pick_interactive(cfg)
        secs = ask("Duration (e.g. 1h30m): ", parse_duration)
        if date is None:
            date = pick_date_menu() if menu else pick_date_interactive()
        desc = args.message
        if desc is None:
            default = (fav or {}).get("description") or "Working on issue %s" % key
            desc = input("Description [%s]: " % default).strip() or default

    issue = get_issue(cfg, key)
    tempo(cfg, "POST", "/4/worklogs", body={
        "authorAccountId": account_id(cfg),
        "issueId": issue["id"],
        "startDate": date.isoformat(),
        "startTime": "09:00:00",
        "timeSpentSeconds": secs,
        "description": desc,
    })
    print('Logged %s on %s "%s" for %s (%s)' % (
        fmt(secs), issue["key"], issue["summary"], date.isoformat(), date.strftime("%a")))


# ---------------------------------------------------------------- search / fav / init


def cmd_search(args):
    cfg = load_config()
    issues = search_issues(cfg, " ".join(args.words))
    if not issues:
        print("No matches.")
        return
    print_issues(issues)


def cmd_fav(args):
    cfg = load_config()
    favs = cfg["favorites"]
    if args.action == "add":
        issue = get_issue(cfg, args.key.upper())
        key = issue["key"]
        alias = args.alias
        if alias is not None:
            if alias.isdigit():
                die("Alias must not be a number (numbers pick favorites by position)")
            for fav in favs:
                if fav.get("alias", "").lower() == alias.lower() and fav["key"].upper() != key:
                    die("Alias '%s' already used by %s" % (alias, fav["key"]))
        entry = {"key": key}
        if alias:
            entry["alias"] = alias
        entry["name"] = issue["summary"]
        if args.message:
            entry["description"] = args.message
        for i, fav in enumerate(favs):
            if fav["key"].upper() == key:
                favs[i] = entry
                break
        else:
            favs.append(entry)
        save_config(cfg)
    elif args.action == "rm":
        a = args.target.lower()
        keep = [f for f in favs if a not in (f["key"].lower(), f.get("alias", "").lower())]
        if len(keep) == len(favs):
            die("No favorite: %s" % args.target)
        cfg["favorites"] = keep
        save_config(cfg)
    print_favorites(cfg)


def cmd_init(args):
    cfg = read_config() or {}

    def prompt(label, field, secret=False):
        cur = cfg.get(field, "")
        if secret:
            hint = " [keep current]" if cur else ""
            val = getpass.getpass("%s%s: " % (label, hint)).strip()
        else:
            hint = " [%s]" % cur if cur else ""
            val = input("%s%s: " % (label, hint)).strip()
        return val or cur

    cfg["jira_url"] = prompt("Jira URL (https://xxx.atlassian.net)", "jira_url").rstrip("/")
    cfg["jira_email"] = prompt("Jira email", "jira_email")
    cfg["jira_token"] = prompt(
        "Jira API token (https://id.atlassian.com/manage-profile/security/api-tokens)",
        "jira_token", secret=True)
    cfg["tempo_token"] = prompt(
        "Tempo API token (Tempo → Settings → API integration)", "tempo_token", secret=True)
    cfg.setdefault("favorites", [])
    save_config(cfg)
    me = myself(load_config())
    print("OK, authenticated as %s (%s)" % (me.get("displayName"), me["accountId"]))


def cmd_help(args):
    if args.command:
        name = " ".join(args.command)
        if name not in args.parsers:
            raise ValueError("Unknown command: %s (try: tempo help)" % name)
        args.parsers[name].print_help()
        return
    args.root.print_help()
    seen = set()
    for name, parser in args.parsers.items():
        if id(parser) in seen:
            continue
        seen.add(id(parser))
        print("\n=== tempo %s ===" % name)
        parser.print_help()


# ---------------------------------------------------------------- main


def build_parser():
    here = os.path.realpath(__file__)
    p = argparse.ArgumentParser(
        prog="tempo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Tempo (Jira Cloud) CLI: hours overview, quick logging, ticket search.",
        epilog=(
            "examples:\n"
            "  tempo                      this week's status\n"
            "  tempo status month -p      previous month\n"
            "  tempo status day -2        two days ago\n"
            "  tempo status day wed       last Wednesday\n"
            "  tempo status 28-10         that day (a date implies 'day')\n"
            "  tempo status week 28-10    the week containing 28 October\n"
            "  tempo log                  interactive pick from favorites/search\n"
            "  tempo log standup 15m      log 15 minutes on favorite alias 'standup'\n"
            "  tempo log ABC-123 1h30m -d yesterday -m \"Review\"\n"
            "  tempo search login bug\n"
            "  tempo fav add ABC-123 standup -m \"Daily standup\"\n"
            "  tempo help                 help for every command and argument\n"
            "  tempo help log             help for one command (or: tempo log -h)\n"
            "\n"
            "install:\n"
            "  chmod +x \"%s\" && ln -s \"%s\" ~/.local/bin/tempo\n"
            "  tempo init\n"
            "config: %s" % (here, here, CONFIG_PATH)
        ),
    )
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("status", aliases=["s"], help="logged vs required hours")
    s.add_argument("period", nargs="?", default="week", metavar="PERIOD",
                   help="day, week or month (default: week), or a date (implies day)")
    s.add_argument("when", nargs="?", metavar="DATE",
                   help="day to show: today, yesterday/y, mon..sun, 28-10, 2026-10-28")
    s.add_argument("-p", "--prev", type=int, nargs="?", const=1, default=0,
                   help="go back N periods, also as a bare -N (default 1)")
    s.set_defaults(func=cmd_status)

    lg = sub.add_parser("log", aliases=["l"], help="log time (interactive without args)")
    lg.add_argument("ticket", nargs="?", help="favorite number, alias or issue key")
    lg.add_argument("duration", nargs="?", help="e.g. 2h, 1h30m, 1.5, 1:30")
    lg.add_argument("-m", "--message", help="worklog description")
    lg.add_argument("-d", "--date", help="today, yesterday/y, mon..sun, YYYY-MM-DD")
    lg.set_defaults(func=cmd_log)

    se = sub.add_parser("search", help="search Jira issues")
    se.add_argument("words", nargs="+", help="search text, or an issue key")
    se.set_defaults(func=cmd_search)

    fv = sub.add_parser("fav", help="list/add/remove favorites")
    fsub = fv.add_subparsers(dest="action")
    fa = fsub.add_parser("add", help="add or update a favorite")
    fa.add_argument("key", help="Jira issue key, e.g. ABC-123")
    fa.add_argument("alias", nargs="?", help="short name to use with 'tempo log'")
    fa.add_argument("-m", "--message", help="default worklog description")
    fr = fsub.add_parser("rm", help="remove a favorite")
    fr.add_argument("target", metavar="KEY_OR_ALIAS", help="issue key or alias to remove")
    fv.set_defaults(func=cmd_fav)

    it = sub.add_parser("init", help="set up credentials")
    it.set_defaults(func=cmd_init)

    hp = sub.add_parser("help", help="show help for all commands, or for one")
    hp.add_argument("command", nargs="*", help="command to show, e.g. 'log' or 'fav add'")
    parsers = {"status": s, "s": s, "log": lg, "l": lg, "search": se, "fav": fv,
               "fav add": fa, "fav rm": fr, "init": it, "help": hp}
    hp.set_defaults(func=cmd_help, parsers=parsers, root=p)
    return p


def normalize_argv(argv):
    """Let 'tempo status day -2' stand in for '--prev 2' (argparse sees -2 as a flag)."""
    if argv and argv[0] in ("status", "s"):
        return argv[:1] + [("--prev=" + a[1:]) if re.fullmatch(r"-\d+", a) else a
                           for a in argv[1:]]
    return argv


def main():
    args = build_parser().parse_args(normalize_argv(sys.argv[1:]))
    if args.cmd is None:
        args = argparse.Namespace(func=cmd_status, period="week", when=None, prev=0)
    try:
        args.func(args)
    except ValueError as e:
        die(str(e))
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
