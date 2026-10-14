#!/usr/bin/env python3
"""Observa PRs do GitHub e abre o claude para revisá-los. Repos, pessoas e prompts vêm do config.json."""
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
                  "prompt": "Revise o pull request {url}",
                  "linked_prompt": "Revise também, considerando o conjunto, os PRs relacionados: {linked}"}
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
        raise FileError(f"{path.name} corrompido ({e}); corrija ou apague o arquivo")


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
    """'1,3' -> [0, 2]; vazio -> []; inválido ou fora do intervalo -> None."""
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
    """Abre o review numa janela própria do gnome-terminal, para rodar vários em paralelo."""
    return ["gnome-terminal", "--title", f"PR #{pr['number']}", f"--working-directory={cwd}", "--",
            claude, "--model", config["model"], "--effort", config["effort"],
            review_prompt(pr["url"], extra, config)]


def repo_of(pr):
    return pr["url"].split("/pull/")[0].removeprefix("https://github.com/")


def pr_key(pr):
    """Chave do PR no state.json; o número sozinho colide entre repos."""
    return f"{repo_of(pr)}#{pr['number']}"


def _login(obj):
    return ((obj or {}).get("login") or "").lower()


def is_bot(login, markers):
    # ponytail: heurística por nome, a API do gh não expõe is_bot em reviews; ajuste "bots" no config.json
    return any(m.lower() in login.lower() for m in markers)


def reviewers_summary(pr, bots=BOT_MARKERS):
    """Resumo humano de revisão do PR: quantos já revisaram e quantos pedidos seguem pendentes."""
    author = _login(pr.get("author"))
    done = {_login(rv.get("author")) for rv in pr["reviews"] if rv.get("state") != "PENDING"}
    done = {u for u in done if u and u != author and not is_bot(u, bots)}
    pending = {_login(q) for q in pr["reviewRequests"]} - done - {""}
    pending = {u for u in pending if not is_bot(u, bots)}
    parts = ([f"{len(done)} revisaram"] if done else []) + ([f"{len(pending)} pendente(s)"] if pending else [])
    return ", ".join(parts) or "sem revisor"


def ci_summary(pr):
    """Estado do CI e de conflito, ex.: 'CI falhou · conflito'."""
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
    ci = "CI falhou" if fail else "CI rodando" if pending else "CI ok" if checks else "sem CI"
    return " · ".join([ci, "conflito"] if pr.get("mergeable") == "CONFLICTING" else [ci])


def age(created_at, now):
    if not created_at:
        return "?"
    secs = max(0, now - datetime.fromisoformat(created_at).timestamp())
    for unit, size in (("d", 86400), ("h", 3600)):
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return f"{int(secs // 60)}min"


def candidates(prs, me, config, state, now):
    """PRs que merecem aviso, como [(pr, motivo)]. Regras na spec, seção 'Regras de candidato'."""
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
            why = "pedido a você" if requested else "novos commits" if mine else "novo"
            out.append((pr, why))
        elif entry["decision"] == "snoozed" and now >= entry["until"]:
            out.append((pr, "lembrete"))
        elif entry["decision"] == "reviewed" and entry["sha"] != sha:
            out.append((pr, "novos commits"))
    return out


def baseline(prs, me, config, now):
    """Primeira execução: o que já está aberto vira 'dismissed', exceto o pedido diretamente a mim."""
    return {pr_key(pr): {"sha": pr["headRefOid"], "decision": "dismissed"}
            for pr, why in candidates(prs, me, config, {}, now) if why != "pedido a você"}


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def fetch_me():
    return gh("api", "user", "--jq", ".login").strip()


def fetch_prs(repos):
    def one(repo):
        return json.loads(gh("pr", "list", "-R", repo, "--state", "open", "--limit", "100",
                             "--json", JSON_FIELDS))
    with ThreadPoolExecutor(max(1, len(repos))) as pool:  # cada gh leva alguns segundos; em paralelo
        return [pr for prs in pool.map(one, repos) for pr in prs]


def notify(msg):
    print("\a", end="", flush=True)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "reviewer", msg], check=False)


def read_line(prompt, timeout=None):
    """Lê uma linha do stdin; None se passar `timeout` segundos sem Enter.

    Lê byte a byte direto do fd: com o buffer do sys.stdin, o select não veria linhas já lidas.
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
    """Resposta válida, ou None se ninguém responder em `timeout` segundos."""
    while True:
        answer = read_line(question, timeout)
        if answer is None or answer.strip().lower() in valid:
            return answer and answer.strip().lower()


def pick_linked(urls):
    if not urls:
        return []
    print("PRs linkados:")
    for i, u in enumerate(urls, 1):
        print(f"  {i}. {u}")
    while True:
        idx = parse_selection(read_line("Incluir quais? (ex.: 1,2; Enter = nenhum): "), len(urls))
        if idx is not None:
            return [urls[i] for i in idx]
        print("Seleção inválida.")


def handle(pr, why, state, config):
    key, sha = pr_key(pr), pr["headRefOid"]
    print(f"\n#{pr['number']} {pr['title']} ({(pr.get('author') or {}).get('login', '?')}) [{why}]\n{pr['url']}")
    answer = ask("Revisar? [s]im / [d]epois / [n]ão: ", ("s", "d", "n"), config["interval"])
    if answer is None:
        print("Sem resposta.")
        answer = "d"
    if answer == "s":
        claude = shutil.which("claude")
        missing = [name for name, path in (("claude", claude), ("gnome-terminal", shutil.which("gnome-terminal")))
                   if not path]
        if missing:
            print(f"{', '.join(missing)} não encontrado no PATH; o PR segue pendente.")
            return
        extra = pick_linked(linked_prs(pr["body"], pr["url"]))
        if subprocess.run(review_cmd(pr, extra, config, claude, os.getcwd())).returncode != 0:
            print("Não consegui abrir o gnome-terminal; o PR segue pendente.")
            return
        print(f"Review do #{pr['number']} aberto em outra janela.")
        state[key] = {"sha": sha, "decision": "reviewed"}
    elif answer == "d":
        state[key] = {"sha": sha, "decision": "snoozed", "until": time.time() + config["snooze"]}
        print(f"Pergunto de novo em {config['snooze'] // 60} min.")
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
            print("Observando:", ", ".join(cfg["users"]) or "(ninguém)")
        elif cmd in ("add-repo", "remove-repo") and len(args) == 1:
            if not REPO_NAME.fullmatch(args[0]):
                sys.exit("repo no formato dono/nome, ex.: octocat/hello-world")
            cfg = load_config()
            fn = add_user if cmd == "add-repo" else remove_user  # mesma lógica: lista de strings, sem repetir
            cfg["repos"] = fn(cfg["repos"], args[0])
            save_json(CONFIG, cfg)
            print("Repositórios:", ", ".join(cfg["repos"]) or "(nenhum)")
        elif cmd == "list" and not args:
            print("\n".join(load_config()["users"]) or "(nenhum usuário observado)")
        else:
            sys.exit("uso: reviewer.py [ui | watch | add <login> | remove <login> | add-repo <dono/repo> | remove-repo <dono/repo> | list]")
    except FileError as e:
        sys.exit(str(e))
    except (KeyboardInterrupt, EOFError):
        print()


if __name__ == "__main__":
    main(sys.argv[1:])
