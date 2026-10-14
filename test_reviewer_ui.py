import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import reviewer as r
import reviewer_ui as ui
from reviewer_ui import LinkedPRScreen, PRMenuScreen, ReviewerApp
from textual.widgets import DataTable


def pr(n, author="alice", draft=False, sha="s1", reviews=(), requests=(), body="", repo="o/r"):
    return {"number": n, "title": f"t{n}", "author": {"login": author}, "isDraft": draft,
            "url": f"https://github.com/{repo}/pull/{n}", "body": body, "headRefOid": sha,
            "reviews": list(reviews), "reviewRequests": list(requests)}


def run_app_test(tmp_path: Path, key=None, click=None):
    config_path, state_path = tmp_path / "config.json", tmp_path / "state.json"
    config_path.write_text(json.dumps({**r.DEFAULT_CONFIG, "users": ["alice"], "interval": 3600}))
    state_path.write_text("{}")
    old_config, old_state = r.CONFIG, r.STATE
    r.CONFIG, r.STATE = config_path, state_path

    async def exercise():
        app = ReviewerApp(fetch_prs=lambda _repo: [pr(1)], fetch_me=lambda: "me")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            table = app.query_one(DataTable)
            assert table.row_count == 1
            if key:
                await pilot.press(key)
            if click:
                await pilot.click(click)
            return table.row_count, r.load_json(state_path, {})

    try:
        return asyncio.run(exercise())
    finally:
        r.CONFIG, r.STATE = old_config, old_state


def test_keyboard_snoozes_selected_pr(tmp_path):
    row_count, state = run_app_test(tmp_path, key="s")

    assert row_count == 0
    assert state["o/r#1"]["decision"] == "snoozed"


def test_mouse_button_dismisses_selected_pr(tmp_path):
    row_count, state = run_app_test(tmp_path, click="#dismiss")

    assert row_count == 0
    assert state["o/r#1"]["decision"] == "dismissed"


def test_review_lets_user_select_linked_prs(tmp_path):
    config_path, state_path = tmp_path / "config.json", tmp_path / "state.json"
    config_path.write_text(json.dumps({**r.DEFAULT_CONFIG, "users": ["alice"], "interval": 3600}))
    state_path.write_text("{}")
    old_config, old_state = r.CONFIG, r.STATE
    r.CONFIG, r.STATE = config_path, state_path
    related = "https://github.com/o/other/pull/2"
    prs = [pr(1, body=f"Relacionado: {related}")]

    async def exercise():
        app = ReviewerApp(fetch_prs=lambda _repo: prs, fetch_me=lambda: "me")
        with patch.object(ui.shutil, "which", return_value="/usr/bin/claude"), \
                patch.object(ui.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as launch:
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                await pilot.press("r")
                assert isinstance(app.screen, LinkedPRScreen)
                await pilot.click("#linked-0")
                await pilot.click("#confirm-linked")

        assert r.load_json(state_path, {})["o/r#1"]["decision"] == "reviewed"
        assert related in launch.call_args.args[0][-1]

    try:
        asyncio.run(exercise())
    finally:
        r.CONFIG, r.STATE = old_config, old_state


def run_browser_test(tmp_path, act):
    config_path, state_path = tmp_path / "config.json", tmp_path / "state.json"
    config_path.write_text(json.dumps({**r.DEFAULT_CONFIG, "users": ["alice"], "interval": 3600}))
    state_path.write_text("{}")
    old_config, old_state = r.CONFIG, r.STATE
    r.CONFIG, r.STATE = config_path, state_path

    async def exercise():
        app = ReviewerApp(fetch_prs=lambda _repo: [pr(1), pr(2)], fetch_me=lambda: "me")
        with patch.object(ui.webbrowser, "open") as opened:
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                await act(app, pilot)
        return opened

    try:
        return asyncio.run(exercise())
    finally:
        r.CONFIG, r.STATE = old_config, old_state


def test_right_click_on_row_opens_menu_and_browser_option_opens_pr(tmp_path):
    async def act(app, pilot):
        await pilot.click(DataTable, offset=(5, 2), button=3)  # linha 2 (y=0 é o cabeçalho)
        assert isinstance(app.screen, PRMenuScreen)
        await pilot.click("#menu", offset=(2, 2))  # 2º item: Abrir no navegador

    run_browser_test(tmp_path, act).assert_called_once_with("https://github.com/o/r/pull/2")


def test_menu_is_compact_and_anchored_at_mouse(tmp_path):
    async def act(app, pilot):
        await pilot.click(DataTable, offset=(5, 2), button=3)
        menu = app.screen.query_one("#menu")
        assert menu.outer_size.width == PRMenuScreen.WIDTH and menu.outer_size.height == len(PRMenuScreen.ITEMS) + 2
        assert menu.region.x == 5 + app.query_one(DataTable).region.x
        await pilot.press("escape")
        assert not isinstance(app.screen, PRMenuScreen)

    run_browser_test(tmp_path, act)


def test_middle_click_on_row_opens_pr_in_browser(tmp_path):
    async def act(app, pilot):
        await pilot.click(DataTable, offset=(5, 2), button=2)

    run_browser_test(tmp_path, act).assert_called_once_with("https://github.com/o/r/pull/2")


def test_o_key_opens_selected_pr_in_browser(tmp_path):
    async def act(app, pilot):
        await pilot.press("o")

    run_browser_test(tmp_path, act).assert_called_once_with("https://github.com/o/r/pull/1")


def test_fetch_runs_off_the_ui_thread(tmp_path):
    threads = []

    def fetch(_repo):
        threads.append(threading.current_thread())
        return [pr(1)]

    async def act(app, pilot):
        app.fetch_prs = fetch
        app.action_refresh()
        await app.workers.wait_for_complete()

    run_browser_test(tmp_path, act)
    assert threads and threads[0] is not threading.main_thread()


def test_new_pr_on_refresh_notifies_once(tmp_path):
    prs = [pr(1)]

    async def act(app, pilot):
        app.fetch_prs = lambda _repo: prs
        app.action_refresh()
        await app.workers.wait_for_complete()
        prs.append(pr(2))
        app.action_refresh()
        await app.workers.wait_for_complete()
        app.action_refresh()  # sem PR novo: não notifica de novo
        await app.workers.wait_for_complete()

    with patch.object(ui.r, "notify") as notify:
        run_browser_test(tmp_path, act)
    notify.assert_called_once()
    assert "#2" in notify.call_args.args[0]


def test_menu_letter_shortcut_and_refresh_while_menu_open(tmp_path):
    async def act(app, pilot):
        await pilot.click(DataTable, offset=(5, 1), button=3)  # linha 1
        assert isinstance(app.screen, PRMenuScreen)
        app.action_refresh()  # a lista é recarregada com o menu aberto
        await app.workers.wait_for_complete()
        await pilot.press("o")

    run_browser_test(tmp_path, act).assert_called_once_with("https://github.com/o/r/pull/1")


def test_refresh_keeps_cursor_on_same_pr(tmp_path):
    async def act(app, pilot):
        table = app.query_one(DataTable)
        table.move_cursor(row=1)
        app.action_refresh()
        await app.workers.wait_for_complete()
        assert app._selected_pr()["number"] == 2

    run_browser_test(tmp_path, act)


def test_new_repo_is_baselined_and_same_number_does_not_collide(tmp_path):
    config_path, state_path = tmp_path / "config.json", tmp_path / "state.json"
    config_path.write_text(json.dumps({**r.DEFAULT_CONFIG, "repos": ["o/r", "o/new"], "users": ["alice"],
                                       "interval": 3600}))
    state_path.write_text(json.dumps({"_repos": ["o/r"]}))
    old_config, old_state = r.CONFIG, r.STATE
    r.CONFIG, r.STATE = config_path, state_path
    prs = [pr(1), pr(1, repo="o/new"), pr(2, repo="o/new")]  # mesmo número 1 nos dois repos

    async def exercise():
        app = ReviewerApp(fetch_prs=lambda _repos: prs, fetch_me=lambda: "me")
        with patch.object(ui.r, "notify") as notify:
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                assert list(app.pr_by_key) == ["o/r#1"]  # PRs já abertos em o/new viram dismissed
                await pilot.press("d")
        notify.assert_not_called()
        state = r.load_json(state_path, {})
        assert state["_repos"] == ["o/new", "o/r"]
        assert state["o/r#1"]["decision"] == "dismissed" and state["o/new#1"]["decision"] == "dismissed"
        assert state["o/new#2"]["decision"] == "dismissed"

    try:
        asyncio.run(exercise())
    finally:
        r.CONFIG, r.STATE = old_config, old_state


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            import tempfile

            with tempfile.TemporaryDirectory() as directory:
                fn(Path(directory))
            print("ok", name)