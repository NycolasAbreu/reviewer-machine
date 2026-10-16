"""Interactive UI to follow and decide on PR reviews."""
import json
import os
import shutil
import subprocess
import time
import webbrowser

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

import reviewer as r


class LinkedPRScreen(ModalScreen[list[str] | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    LinkedPRScreen { align: center middle; }
    #dialog { width: 80%; max-height: 80%; border: round $accent; padding: 1 2; background: $surface; }
    #links { height: 1fr; }
    #actions { height: auto; align-horizontal: right; }
    """

    def __init__(self, links: list[str]):
        super().__init__()
        self.links = links

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static("Include related PRs in this review?", markup=False)
            with Vertical(id="links"):
                for index, url in enumerate(self.links):
                    yield Checkbox(url, id=f"linked-{index}")
            with Horizontal(id="actions"):
                yield Button("Cancel", id="cancel-linked")
                yield Button("Open review", variant="primary", id="confirm-linked")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-linked":
            self.dismiss(None)
        elif event.button.id == "confirm-linked":
            selected = [
                url for index, url in enumerate(self.links)
                if self.query_one(f"#linked-{index}", Checkbox).value
            ]
            self.dismiss(selected)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PRMenuScreen(ModalScreen[str | None]):
    """Compact context menu anchored at the mouse position."""
    WIDTH = 28
    BINDINGS = [("escape", "cancel", "Close"), *[(k, f"pick('{a}')", "") for a, k in
                                                  (("review", "r"), ("browser", "o"), ("snooze", "s"), ("dismiss", "d"))]]
    CSS = f"""
    PRMenuScreen {{ align: left top; background: transparent; }}
    #menu {{ width: {WIDTH}; height: auto; border: round $accent; padding: 0; background: $surface; }}
    """
    # (action, label, shortcut); None becomes a separator
    ITEMS = [("review", "Review", "r"), ("browser", "Open in browser", "o"), None,
             ("snooze", "Snooze", "s"), ("dismiss", "Dismiss", "d")]

    def __init__(self, x: int, y: int):
        super().__init__()
        self.x, self.y = x, y

    def compose(self) -> ComposeResult:
        inner = self.WIDTH - 2
        options = [None if item is None else
                   Option(f"{item[1]:<{inner - 2}}{item[2]:>2}", id=item[0]) for item in self.ITEMS]
        yield OptionList(*options, id="menu")

    def on_mount(self) -> None:
        menu = self.query_one("#menu")
        height = len(self.ITEMS) + 2
        menu.styles.offset = (max(0, min(self.x, self.app.size.width - self.WIDTH)),
                              max(0, min(self.y, self.app.size.height - height)))
        menu.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:  # click outside the menu
            self.dismiss(None)

    def action_pick(self, name: str) -> None:
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewerApp(App[None]):
    TITLE = "Reviewer — PR queue"
    BINDINGS = [
        ("r", "review", "Review"),
        ("o", "browser", "Browser"),
        ("s", "snooze", "Snooze"),
        ("d", "dismiss", "Dismiss"),
        ("f5", "refresh", "Refresh"),
        ("enter", "review", "Review"),
        ("q", "quit", "Quit"),
    ]
    CSS = """
    Screen { layout: vertical; }
    #summary { height: 1; padding: 0 1; color: $text-muted; }
    #prs { height: 1fr; margin: 0 1; }
    #actions { height: 3; align-horizontal: center; padding: 0 1; }
    #actions Button { margin: 0 1; min-width: 16; }
    #status { height: 1; padding: 0 1; color: $accent; }
    """

    def __init__(self, fetch_prs=r.fetch_prs, fetch_me=r.fetch_me):
        super().__init__()
        self.fetch_prs = fetch_prs
        self.fetch_me = fetch_me
        self.config = r.load_config()
        self.state = r.load_json(r.STATE, {})
        self.me = None
        self.prs = []
        self.pr_by_key = {}
        self._refreshing = False
        self._seen = None  # keys already shown; None until the first load (does not notify about what already existed)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading PRs...", id="summary")
        yield DataTable(id="prs", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="actions"):
            yield Button("Review [r]", variant="primary", id="review")
            yield Button("Snooze [s]", id="snooze")
            yield Button("Dismiss [d]", id="dismiss")
            yield Button("Refresh [F5]", id="refresh")
        yield Static("↑/↓ navigate · Enter review · q quit", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("#", "Repo", "Title", "Author", "Age", "Reviewers", "CI", "Reason")
        self.refresh_prs()
        self.set_interval(max(1, self.config["interval"]), self.refresh_prs)

    def action_refresh(self) -> None:
        self.refresh_prs()

    def refresh_prs(self) -> None:
        if not self._refreshing:  # one fetch at a time; gh takes a few seconds
            self._refreshing = True
            self._fetch()

    @work(thread=True)
    def _fetch(self) -> None:
        try:
            config = r.load_config()
            me = self.me or self.fetch_me()
            prs = self.fetch_prs(config["repos"])
        except (r.FileError, subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as error:
            self.call_from_thread(self._fetch_failed, (getattr(error, "stderr", "") or str(error)).strip())
            return
        self.call_from_thread(self._apply, config, me, prs)

    def _fetch_failed(self, detail: str) -> None:
        self._refreshing = False
        self.query_one("#summary", Static).update(f"Failed to fetch PRs: {detail}")

    def _apply(self, config, me, prs) -> None:
        self._refreshing = False
        self.config, self.me, self.prs = config, me, prs
        now = time.time()
        # New repo in the config (or first run): whatever is already open becomes "dismissed", silently.
        known = set(self.state.get("_repos", []))
        new = [repo for repo in self.config["repos"] if repo not in known]
        if new:
            self.state.update(r.baseline([pr for pr in self.prs if r.repo_of(pr) in new], self.me, self.config, now))
            self.state["_repos"] = sorted(known | set(new))
            r.save_json(r.STATE, self.state)
        self._render_rows(now)
        if self._seen is not None:
            for key, pr in self.pr_by_key.items():
                if key not in self._seen:
                    self.bell()
                    r.notify(f"{key}: {pr['title']}")
        self._seen = set(self.pr_by_key)
        self.sub_title = ", ".join(self.config["repos"])
        self.query_one("#summary", Static).update(
            f"{len(self.pr_by_key)} pending PR(s) · "
            f"updated at {time.strftime('%H:%M:%S')}"
        )
        status = ("No repositories configured. Use reviewer.py add-repo owner/repo." if not self.config["repos"] else
              "No users watched; only PRs with a review requested from you will be listed. "
              "Use reviewer.py add <login>." if not self.config["users"] else
              "↑/↓ navigate · Enter or r review · o browser · s snooze · d dismiss · F5 refresh · q quit")
        self.query_one("#status", Static).update(status)

    def _render_rows(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        rows = r.candidates(self.prs, self.me, self.config, self.state, now)
        self.pr_by_key = {r.pr_key(pr): pr for pr, _ in rows}
        table = self.query_one(DataTable)
        keep_row = table.cursor_row
        keep_key = table.ordered_rows[keep_row].key.value if table.row_count else None
        table.clear()
        for pr, reason in rows:
            author = (pr.get("author") or {}).get("login", "?")
            table.add_row(str(pr["number"]), r.repo_of(pr).split("/")[1], pr["title"], author, r.age(pr.get("createdAt"), now),
                          r.reviewers_summary(pr, self.config["bots"]), r.ci_summary(pr), reason,
                          key=r.pr_key(pr))
        if table.row_count:  # keep the cursor on the same PR (or the same position, if it left the list)
            table.move_cursor(row=table.get_row_index(keep_key) if keep_key in self.pr_by_key
                              else min(keep_row, table.row_count - 1))

    def _selected_pr(self):
        table = self.query_one(DataTable)
        if table.row_count == 0:
            self._set_status("No PR selected.")
            return None
        return self.pr_by_key.get(table.ordered_rows[table.cursor_row].key.value)

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def action_snooze(self) -> None:
        pr = self._selected_pr()
        if not pr:
            return
        self.state[r.pr_key(pr)] = {
            "sha": pr["headRefOid"], "decision": "snoozed",
            "until": time.time() + self.config["snooze"],
        }
        r.save_json(r.STATE, self.state)
        self._render_rows()
        self._set_status(f"PR #{pr['number']} snoozed for {self.config['snooze'] // 60} min.")

    def action_dismiss(self) -> None:
        pr = self._selected_pr()
        if not pr:
            return
        self.state[r.pr_key(pr)] = {"sha": pr["headRefOid"], "decision": "dismissed"}
        r.save_json(r.STATE, self.state)
        self._render_rows()
        self._set_status(f"PR #{pr['number']} dismissed.")

    def action_review(self) -> None:
        pr = self._selected_pr()
        if not pr:
            return
        links = r.linked_prs(pr.get("body"), pr["url"])
        if links:
            self.push_screen(LinkedPRScreen(links), lambda selected: self._launch_review(pr, selected))
        else:
            self._launch_review(pr, [])

    def _launch_review(self, pr, linked: list[str] | None) -> None:
        if linked is None:
            self._set_status("Review launch cancelled.")
            return
        claude = shutil.which("claude")
        missing = [name for name, path in (("claude", claude), ("gnome-terminal", shutil.which("gnome-terminal")))
                   if not path]
        if missing:
            self._set_status(f"{', '.join(missing)} not found on PATH; PR stays pending.")
            return
        try:
            result = subprocess.run(r.review_cmd(pr, linked, self.config, claude, os.getcwd()))
        except OSError as error:
            self._set_status(f"Could not open the terminal: {error}")
            return
        if result.returncode != 0:
            self._set_status("Could not open the terminal; PR stays pending.")
            return
        self.state[r.pr_key(pr)] = {"sha": pr["headRefOid"], "decision": "reviewed"}
        r.save_json(r.STATE, self.state)
        self._render_rows()
        self._set_status(f"Review of PR #{pr['number']} opened in another window.")

    def action_browser(self) -> None:
        pr = self._selected_pr()
        if not pr:
            return
        webbrowser.open(pr["url"])
        self._set_status(f"PR #{pr['number']} opened in the browser.")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Right/middle buttons: DataTable consumes the row Click (event.stop()), so we use MouseDown.
        row = event.style.meta.get("row", -1)
        if event.button not in (2, 3) or row < 0:
            return
        table = self.query_one(DataTable)
        if row >= table.row_count:
            return
        table.move_cursor(row=row)
        pr = self._selected_pr()
        if event.button == 2:
            self.action_browser()
        elif pr:
            self.push_screen(PRMenuScreen(event.screen_x, event.screen_y),
                             lambda name: self._run_menu_action(name, r.pr_key(pr)))

    def _run_menu_action(self, name: str | None, key: str) -> None:
        if not name:
            return
        table = self.query_one(DataTable)
        if key not in self.pr_by_key:  # the list was refreshed while the menu was open
            self._set_status(f"{key} left the list.")
            return
        table.move_cursor(row=table.get_row_index(key))
        getattr(self, f"action_{name}")()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {"review": self.action_review, "snooze": self.action_snooze,
                   "dismiss": self.action_dismiss, "refresh": self.action_refresh}
        action = actions.get(event.button.id)
        if action:
            action()


def run_ui() -> None:
    ReviewerApp().run()