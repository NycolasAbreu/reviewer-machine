# reviewer-machine

Terminal UI that watches open GitHub pull requests and launches Claude Code to review them.
It shows a queue of PRs (from watched authors, or where a review was requested from you), with age, human reviewers, CI status and merge conflicts.

- Right-click a row: menu (Review, Open in browser, Snooze, Dismiss). Middle-click or `o`: open the PR in the browser.
- Alerts (bell + `notify-send`) when a new PR enters the queue.

## Requirements

- Python 3.12+, and `gh` authenticated (`gh auth login`) with access to the watched repos
- Claude Code (`claude`) and `gnome-terminal` on the PATH; `notify-send` is optional

## Usage

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json      # or let the app create an empty config
.venv/bin/python reviewer.py add-repo owner/repo
.venv/bin/python reviewer.py add github-login
.venv/bin/python reviewer.py            # opens the UI
```

Other commands: `remove-repo`, `remove`, `list`. Tests: `python test_reviewer.py && python test_reviewer_ui.py`.

## Configuration (`config.json`, not versioned)

| key | meaning |
|---|---|
| `repos` | watched repositories, as `owner/name` |
| `users` | logins whose PRs enter the queue |
| `interval` / `snooze` | seconds between fetches / how long "Snooze" lasts |
| `model` / `effort` | passed to `claude` |
| `prompt` | what Claude receives; `{url}` becomes the PR URL (e.g. `/your-command {url}`) |
| `linked_prompt` | appended when you include related PRs; `{linked}` becomes the list |
| `bots` | login fragments treated as bots/AIs and ignored in the reviewer count |

The decision history lives in `state.json` (also not versioned).
