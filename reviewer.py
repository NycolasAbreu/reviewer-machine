#!/usr/bin/env python3
"""Watches GitHub PRs and opens claude to review them. Repos, people and prompts come from config.json."""
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"
BOT_MARKERS = ["bot", "copilot", "claude", "vela-", "coderabbit", "codex", "gemini", "sonar", "github-actions"]
REPO_NAME = re.compile(r"[\w.-]+/[\w.-]+")
DEFAULT_CONFIG = {"repos": [], "users": [], "interval": 180, "snooze": 2400,
                  "model": "claude-opus-5-5", "effort": "high", "bots": BOT_MARKERS,
                  "prompt": "Review the pull request {url}",
                  "linked_prompt": "Also review these related PRs, considering them together: {linked}"}
JSON_FIELDS = ("number,title,author,isDraft,url,body,headRefOid,reviews,reviewRequests,"
               "createdAt,statusCheckRollup,mergeable")
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


class FileError(Exception):
    pass


def load_json(path, default):
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise FileError(f"{path.name} is corrupt ({e}); fix or delete the file")


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_config():
    cfg = {**DEFAULT_CONFIG, **load_json(CONFIG, {})}
    if not CONFIG.exists():
        save_json(CONFIG, cfg)
    return cfg


def add_user(users, login):
    if login.lower() in {u.lower() for u in users}:
        return users
    return [*users, login]


def remove_user(users, login):
    return [u for u in users if u.lower() != login.lower()]


def linked_prs(body, self_url):
    urls = []
    for u in PR_URL.findall(body or ""):
        if u != self_url and u not in urls:
            urls.append(u)
    return urls


def parse_selection(text, n):
    """'1,3' -> [0, 2]; empty -> []; invalid or out of range -> None."""
    text = text.strip()
    if not text:
        return []
    try:
        idx = sorted({int(x) - 1 for x in text.replace(" ", "").split(",") if x})
    except ValueError:
        return None
    return idx if all(0 <= i < n for i in idx) else None


def review_prompt(url, extra, config):
    prompt = config["prompt"].replace("{url}", url)
    if not extra:
        return prompt
    return prompt + "\n\n" + config["linked_prompt"].replace("{linked}", " ".join(extra))


def review_cmd(pr, extra, config, claude, cwd):
    """Opens the review in its own gnome-terminal window, so several can run in parallel."""
    return ["gnome-terminal", "--title", f"PR #{pr['number']}", f"--working-directory={cwd}", "--",
            claude, "--model", config["model"], "--effort", config["effort"],
            review_prompt(pr["url"], extra, config)]


def repo_of(pr):
    return pr["url"].split("/pull/")[0].removeprefix("https://github.com/")


def pr_key(pr):
    """PR key in state.json; the number alone collides across repos."""
    return f"{repo_of(pr)}#{pr['number']}"


def _login(obj):
    return ((obj or {}).get("login") or "").lower()


def is_bot(login, markers):
    # ponytail: name-based heuristic, the gh API does not expose is_bot on reviews; tune "bots" in config.json
    return any(m.lower() in login.lower() for m in markers)


def reviewers_summary(pr, bots=BOT_MARKERS):
    """Human review summary of a PR: how many already reviewed and how many requests are still pending."""
    author = _login(pr.get("author"))
    done = {_login(rv.get("author")) for rv in pr["reviews"] if rv.get("state") != "PENDING"}
    done = {u for u in done if u and u != author and not is_bot(u, bots)}
    pending = {_login(q) for q in pr["reviewRequests"]} - done - {""}
    pending = {u for u in pending if not is_bot(u, bots)}
    parts = ([f"{len(done)} reviewed"] if done else []) + ([f"{len(pending)} pending"] if pending else [])
    return ", ".join(parts) or "no reviewer"


def ci_summary(pr):
    """CI and conflict status, e.g. 'CI failed · conflict'."""
    fail = pending = False
    checks = pr.get("statusCheckRollup") or []
    for c in checks:
        if c.get("__typename") == "StatusContext":
            bad, good = c.get("state") in ("FAILURE", "ERROR"), c.get("state") == "SUCCESS"
        else:
            done = c.get("status") == "COMPLETED"
            bad = done and c.get("conclusion") not in ("SUCCESS", "NEUTRAL", "SKIPPED")
            good = done and not bad
        fail |= bad
        pending |= not (bad or good)
    ci = "CI failed" if fail else "CI running" if pending else "CI ok" if checks else "no CI"
    return " · ".join([ci, "conflict"] if pr.get("mergeable") == "CONFLICTING" else [ci])


def age(created_at, now):
    if not created_at:
        return "?"
    secs = max(0, now - datetime.fromisoformat(created_at).timestamp())
    for unit, size in (("d", 86400), ("h", 3600)):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return f"{int(secs // 60)}min"


def candidates(prs, me, config, state, now):
    """PRs worth flagging, as [(pr, reason)]."""
    users = {u.lower() for u in config["users"]}
    me = me.lower()
    out = []
    for pr in prs:
        if pr["isDraft"]:
            continue
        sha = pr["headRefOid"]
        mine = [rv for rv in pr["reviews"]
                if _login(rv.get("author")) == me and rv.get("state") not in ("PENDING", "DISMISSED")]
        if any((rv.get("commit") or {}).get("oid") == sha for rv in mine):
            continue
        requested = any(_login(q) == me for q in pr["reviewRequests"])
        if _login(pr.get("author")) not in users and not requested:
            continue
        entry = state.get(pr_key(pr))
        if entry is None:
            why = "review requested" if requested else "new commits" if mine else "new"
            out.append((pr, why))
        elif entry["decision"] == "snoozed" and now >= entry["until"]:
            out.append((pr, "reminder"))
        elif entry["decision"] == "reviewed" and entry["sha"] != sha:
            out.append((pr, "new commits"))
    return out


def baseline(prs, me, config, now):
    """First run: whatever is already open becomes 'dismissed', except reviews requested directly from me."""
    return {pr_key(pr): {"sha": pr["headRefOid"], "decision": "dismissed"}
            for pr, why in candidates(prs, me, config, {}, now) if why != "review requested"}


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def fetch_me():
    return gh("api", "user", "--jq", ".login").strip()


def fetch_prs(repos):
    def one(repo):
        return json.loads(gh("pr", "list", "-R", repo, "--state", "open", "--limit", "100",
                             "--json", JSON_FIELDS))
    with ThreadPoolExecutor(max(1, len(repos))) as pool:  # each gh call takes a few seconds; run them in parallel
        return [pr for prs in pool.map(one, repos) for pr in prs]


def notify(msg):
    print("\a", end="", flush=True)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "reviewer", msg], check=False)


def read_line(prompt, timeout=None):
    """Reads a line from stdin; None if `timeout` seconds pass without Enter.

    Reads byte by byte straight from the fd: with sys.stdin buffering, select would not see lines already read.
    """
    print(prompt, end="", flush=True)
    fd = sys.stdin.fileno()
    deadline = None if timeout is None else time.time() + timeout
    buf = b""
    while True:
        if deadline is not None and not select.select([fd], [], [], max(0, deadline - time.time()))[0]:
            print()
            return None
        ch = os.read(fd, 1)
        if not ch:
            raise EOFError
        if ch == b"\n":
            return buf.decode(errors="replace")
        buf += ch


def ask(question, valid, timeout):
    """A valid answer, or None if nobody answers within `timeout` seconds."""
    while True:
        answer = read_line(question, timeout)
        if answer is None or answer.strip().lower() in valid:
            return answer and answer.strip().lower()


def pick_linked(urls):
    if not urls:
        return []
    print("Linked PRs:")
    for i, u in enumerate(urls, 1):
        print(f"  {i}. {u}")
    while True:
        idx = parse_selection(read_line("Include which? (e.g. 1,2; Enter = none): "), len(urls))
        if idx is not None:
            return [urls[i] for i in idx]
        print("Invalid selection.")


def handle(pr, why, state, config):
    key, sha = pr_key(pr), pr["headRefOid"]
    print(f"\n#{pr['number']} {pr['title']} ({(pr.get('author') or {}).get('login', '?')}) [{why}]\n{pr['url']}")
    answer = ask("Review? [y]es / [l]ater / [n]o: ", ("y", "l", "n"), config["interval"])
    if answer is None:
        print("No answer.")
        answer = "l"
    if answer == "y":
        claude = shutil.which("claude")
        missing = [name for name, path in (("claude", claude), ("gnome-terminal", shutil.which("gnome-terminal")))
                   if not path]
        if missing:
            print(f"{', '.join(missing)} not found on PATH; the PR stays pending.")
            return
        extra = pick_linked(linked_prs(pr["body"], pr["url"]))
        if subprocess.run(review_cmd(pr, extra, config, claude, os.getcwd())).returncode != 0:
            print("Could not open gnome-terminal; the PR stays pending.")
            return
        print(f"Review of #{pr['number']} opened in another window.")
        state[key] = {"sha": sha, "decision": "reviewed"}
    elif answer == "l":
        state[key] = {"sha": sha, "decision": "snoozed", "until": time.time() + config["snooze"]}
        print(f"Asking again in {config['snooze'] // 60} min.")
    else:
        state[key] = {"sha": sha, "decision": "dismissed"}
    save_json(STATE, state)


def watch():
    from reviewer_ui import run_ui
    run_ui()


def main(argv):
    cmd, *args = argv or ["watch"]
    try:
        if cmd in ("watch", "ui") and not args:
            watch()
        elif cmd in ("add", "remove") and len(args) == 1:
            cfg = load_config()
            login = args[0].lstrip("@")
            fn = add_user if cmd == "add" else remove_user
            cfg["users"] = fn(cfg["users"], login)
            save_json(CONFIG, cfg)
            print("Watching:", ", ".join(cfg["users"]) or "(nobody)")
        elif cmd in ("add-repo", "remove-repo") and len(args) == 1:
            if not REPO_NAME.fullmatch(args[0]):
                sys.exit("repo must be in owner/name format, e.g. octocat/hello-world")
            cfg = load_config()
            fn = add_user if cmd == "add-repo" else remove_user  # same logic: list of strings, no duplicates
            cfg["repos"] = fn(cfg["repos"], args[0])
            save_json(CONFIG, cfg)
            print("Repositories:", ", ".join(cfg["repos"]) or "(none)")
        elif cmd == "list" and not args:
            print("\n".join(load_config()["users"]) or "(no users watched)")
        else:
            sys.exit("usage: reviewer.py [ui | watch | add <login> | remove <login> | add-repo <owner/repo> | remove-repo <owner/repo> | list]")
    except FileError as e:
        sys.exit(str(e))
    except (KeyboardInterrupt, EOFError):
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
