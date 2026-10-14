"""Interface interativa para acompanhar e decidir reviews de PRs."""
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
    BINDINGS = [("escape", "cancel", "Cancelar")]
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
            yield Static("Incluir PRs relacionados nesta revisão?", markup=False)
            with Vertical(id="links"):
                for index, url in enumerate(self.links):
                    yield Checkbox(url, id=f"linked-{index}")
            with Horizontal(id="actions"):
                yield Button("Cancelar", id="cancel-linked")
                yield Button("Abrir review", variant="primary", id="confirm-linked")

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
    """Menu de contexto compacto, ancorado na posição do mouse."""
    WIDTH = 28
    BINDINGS = [("escape", "cancel", "Fechar"), *[(k, f"pick('{a}')", "") for a, k in
                                                  (("review", "r"), ("browser", "o"), ("snooze", "s"), ("dismiss", "d"))]]
    CSS = f"""
    PRMenuScreen {{ align: left top; background: transparent; }}
    #menu {{ width: {WIDTH}; height: auto; border: round $accent; padding: 0; background: $surface; }}
    """
    # (ação, rótulo, atalho); None vira separador
    ITEMS = [("review", "Revisar", "r"), ("browser", "Abrir no navegador", "o"), None,
             ("snooze", "Adiar", "s"), ("dismiss", "Dispensar", "d")]

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
        if event.widget is self:  # clique fora do menu
            self.dismiss(None)

    def action_pick(self, name: str) -> None:
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewerApp(App[None]):
    TITLE = "Reviewer — fila de PRs"
    BINDINGS = [
        ("r", "review", "Revisar"),
        ("o", "browser", "Navegador"),
        ("s", "snooze", "Adiar"),
        ("d", "dismiss", "Dispensar"),
        ("f5", "refresh", "Atualizar"),
        ("enter", "review", "Revisar"),
        ("q", "quit", "Sair"),
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
        self._seen = None  # números já exibidos; None até a primeira carga (não notifica o que já existia)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Carregando PRs...", id="summary")
        yield DataTable(id="prs", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="actions"):
            yield Button("Revisar [r]", variant="primary", id="review")
            yield Button("Adiar [s]", id="snooze")
            yield Button("Dispensar [d]", id="dismiss")
            yield Button("Atualizar [F5]", id="refresh")
        yield Static("↑/↓ navegar · Enter revisar · q sair", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("#", "Repo", "Título", "Autor", "Idade", "Revisores", "CI", "Motivo")
        self.refresh_prs()
        self.set_interval(max(1, self.config["interval"]), self.refresh_prs)

    def action_refresh(self) -> None:
        self.refresh_prs()

    def refresh_prs(self) -> None:
        if not self._refreshing:  # uma busca por vez; o gh leva alguns segundos
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
        self.query_one("#summary", Static).update(f"Falha ao buscar PRs: {detail}")

    def _apply(self, config, me, prs) -> None:
        self._refreshing = False
        self.config, self.me, self.prs = config, me, prs
        now = time.time()
        # Repo novo na config (ou primeira execução): o que já está aberto vira "dismissed", sem avisar.
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
            f"{len(self.pr_by_key)} PR(s) pendente(s) · "
            f"atualizado às {time.strftime('%H:%M:%S')}"
        )
        status = ("Nenhum repositório configurado. Use reviewer.py add-repo dono/repo." if not self.config["repos"] else
              "Nenhum usuário observado; só PRs com review solicitado a você serão listados. "
              "Use reviewer.py add <login>." if not self.config["users"] else
              "↑/↓ navegar · Enter ou r revisar · o navegador · s adiar · d dispensar · F5 atualizar · q sair")
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
        if table.row_count:  # mantém o cursor no mesmo PR (ou na mesma posição, se ele saiu da lista)
            table.move_cursor(row=table.get_row_index(keep_key) if keep_key in self.pr_by_key
                              else min(keep_row, table.row_count - 1))

    def _selected_pr(self):
        table = self.query_one(DataTable)
        if table.row_count == 0:
            self._set_status("Nenhum PR selecionado.")
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
        self._set_status(f"PR #{pr['number']} adiado por {self.config['snooze'] // 60} min.")

    def action_dismiss(self) -> None:
        pr = self._selected_pr()
        if not pr:
            return
        self.state[r.pr_key(pr)] = {"sha": pr["headRefOid"], "decision": "dismissed"}
        r.save_json(r.STATE, self.state)
        self._render_rows()
        self._set_status(f"PR #{pr['number']} dispensado.")

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
            self._set_status("Abertura do review cancelada.")
            return
        claude = shutil.which("claude")
        missing = [name for name, path in (("claude", claude), ("gnome-terminal", shutil.which("gnome-terminal")))
                   if not path]
        if missing:
            self._set_status(f"{', '.join(missing)} não encontrado no PATH; PR segue pendente.")
            return
        try:
            result = subprocess.run(r.review_cmd(pr, linked, self.config, claude, os.getcwd()))
        except OSError as error:
            self._set_status(f"Não foi possível abrir o terminal: {error}")
            return
        if result.returncode != 0:
            self._set_status("Não consegui abrir o terminal; PR segue pendente.")
            return
        self.state[r.pr_key(pr)] = {"sha": pr["headRefOid"], "decision": "reviewed"}
        r.save_json(r.STATE, self.state)
        self._render_rows()
        self._set_status(f"Review do PR #{pr['number']} aberto em outra janela.")

    def action_browser(self) -> None:
        pr = self._selected_pr()
        if not pr:
            return
        webbrowser.open(pr["url"])
        self._set_status(f"PR #{pr['number']} aberto no navegador.")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Botões direito/central: o DataTable consome o Click da linha (event.stop()), então usamos MouseDown.
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
        if key not in self.pr_by_key:  # a lista foi atualizada com o menu aberto
            self._set_status(f"{key} saiu da lista.")
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