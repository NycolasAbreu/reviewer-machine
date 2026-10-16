import json
import os
import sys
import tempfile
from pathlib import Path

import reviewer as r


def pr(n, author="alice", draft=False, sha="s1", reviews=(), requests=(), body=""):
    return {"number": n, "title": f"t{n}", "author": {"login": author}, "isDraft": draft,
            "url": f"https://github.com/o/r/pull/{n}", "body": body, "headRefOid": sha,
            "reviews": list(reviews), "reviewRequests": list(requests)}


def rev(sha, login="me", state="COMMENTED"):
    return {"author": {"login": login}, "commit": {"oid": sha}, "state": state}


CFG = {"users": ["Alice"]}


def test_add_remove_user():
    assert r.add_user([], "Bob") == ["Bob"]
    assert r.add_user(["Bob"], "bob") == ["Bob"]
    assert r.remove_user(["Bob", "alice"], "BOB") == ["alice"]
    assert r.remove_user(["alice"], "ghost") == ["alice"]


def test_linked_prs():
    body = ("Related: https://github.com/acme/lib-a/pull/187\n"
            "and again https://github.com/acme/lib-a/pull/187/files\n"
            "self https://github.com/o/r/pull/1\n"
            "other https://github.com/acme/lib-b/pull/176")
    assert r.linked_prs(body, "https://github.com/o/r/pull/1") == [
        "https://github.com/acme/lib-a/pull/187",
        "https://github.com/acme/lib-b/pull/176"]
    assert r.linked_prs(None, "x") == []
    assert r.linked_prs("", "x") == []


def test_reviewers_summary_ignores_bots_author_and_teams():
    pr = {"author": {"login": "alice"},
          "reviews": [{"author": {"login": "bob"}, "state": "APPROVED"},
                      {"author": {"login": "bob"}, "state": "COMMENTED"},
                      {"author": {"login": "alice"}, "state": "COMMENTED"},
                      {"author": {"login": "copilot-pull-request-reviewer"}, "state": "COMMENTED"},
                      {"author": {"login": "vela-security-reviewer"}, "state": "COMMENTED"}],
          "reviewRequests": [{"login": "carol"}, {"name": "time", "slug": "o/time"}, {"login": "bob"}]}
    assert r.reviewers_summary(pr) == "1 reviewed, 1 pending"
    assert r.reviewers_summary({**pr, "reviews": [], "reviewRequests": []}) == "no reviewer"


def test_reviewers_summary_uses_configured_bots():
    pr = {"author": {"login": "alice"}, "reviews": [{"author": {"login": "bob"}, "state": "APPROVED"}],
          "reviewRequests": []}
    assert r.reviewers_summary(pr, ["bo"]) == "no reviewer"
    assert r.reviewers_summary(pr, []) == "1 reviewed"


def test_ci_summary():
    def check(status, conclusion=None):
        return {"__typename": "CheckRun", "status": status, "conclusion": conclusion}

    ctx = lambda state: {"__typename": "StatusContext", "state": state}
    assert r.ci_summary({"statusCheckRollup": []}) == "no CI"
    assert r.ci_summary({"statusCheckRollup": [check("COMPLETED", "SUCCESS"), check("COMPLETED", "SKIPPED"),
                                               ctx("SUCCESS")]}) == "CI ok"
    assert r.ci_summary({"statusCheckRollup": [check("COMPLETED", "SUCCESS"), check("IN_PROGRESS", "")]}) == "CI running"
    assert r.ci_summary({"statusCheckRollup": [check("IN_PROGRESS", ""), ctx("ERROR")],
                         "mergeable": "CONFLICTING"}) == "CI failed · conflict"
    assert r.ci_summary({"statusCheckRollup": None, "mergeable": "MERGEABLE"}) == "no CI"


def test_age():
    now = 1_000_000_000
    iso = lambda secs: __import__("datetime").datetime.fromtimestamp(
        now - secs, __import__("datetime").timezone.utc).isoformat()
    assert r.age(iso(90), now) == "1min"
    assert r.age(iso(3 * 3600 + 5), now) == "3h"
    assert r.age(iso(2 * 86400), now) == "2d"
    assert r.age(None, now) == "?"


def test_pr_key_is_per_repo():
    assert r.pr_key(pr(7)) == "o/r#7"
    other = {**pr(7), "url": "https://github.com/o/other/pull/7"}
    assert r.pr_key(other) == "o/other#7"
    done = {"o/r#7": {"sha": "s1", "decision": "dismissed"}}
    assert cand([pr(7), other], done) == [(7, "new")]  # the one in the other repo stays pending


def test_parse_selection():
    assert r.parse_selection("", 3) == []
    assert r.parse_selection(" 1, 3 ", 3) == [0, 2]
    assert r.parse_selection("3,1,1", 3) == [0, 2]
    assert r.parse_selection("4", 3) is None
    assert r.parse_selection("0", 3) is None
    assert r.parse_selection("a", 3) is None


def test_review_prompt_comes_from_config():
    cfg = {"prompt": "/rv {url}", "linked_prompt": "junto com: {linked}"}
    assert r.review_prompt("U", [], cfg) == "/rv U"
    assert r.review_prompt("U", ["A", "B"], cfg) == "/rv U\n\njunto com: A B"
    assert "{url}" in r.DEFAULT_CONFIG["prompt"] and "{linked}" in r.DEFAULT_CONFIG["linked_prompt"]


def test_review_cmd():
    cfg = {"model": "claude-opus-5-5", "effort": "high", "prompt": "/rv {url}", "linked_prompt": "+ {linked}"}
    cmd = r.review_cmd(pr(7), ["A"], cfg, "/bin/claude", "/w")
    assert cmd[:4] == ["gnome-terminal", "--title", "PR #7", "--working-directory=/w"]
    assert cmd[4:9] == ["--", "/bin/claude", "--model", "claude-opus-5-5", "--effort"]
    assert cmd[9] == "high" and cmd[10] == r.review_prompt("https://github.com/o/r/pull/7", ["A"], cfg)


def test_json_roundtrip_and_corrupt():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.json"
        assert r.load_json(f, {"a": 1}) == {"a": 1}
        r.save_json(f, {"b": 2})
        assert r.load_json(f, {}) == {"b": 2}
        assert not (Path(d) / "x.tmp").exists()
        f.write_text("{nope")
        try:
            r.load_json(f, {})
            assert False, "should raise FileError"
        except r.FileError as e:
            assert "x.json" in str(e)


def test_load_config_creates_defaults():
    with tempfile.TemporaryDirectory() as d:
        r.CONFIG = Path(d) / "config.json"
        cfg = r.load_config()
        assert cfg["interval"] == 180 and cfg["snooze"] == 2400 and cfg["users"] == []
        assert r.CONFIG.exists()
        r.CONFIG.write_text(json.dumps({"users": ["x"], "interval": 60}))
        cfg = r.load_config()
        assert cfg["users"] == ["x"] and cfg["interval"] == 60 and cfg["snooze"] == 2400


def test_ask_retry_timeout_and_eof():
    rfd, wfd = os.pipe()
    old = sys.stdin
    sys.stdin = os.fdopen(rfd)
    try:
        os.write(wfd, b"x\nD\n")
        assert r.ask("? ", ("s", "d", "n"), 1) == "d"
        assert r.ask("? ", ("s", "d", "n"), 0.2) is None
        os.close(wfd)
        try:
            r.ask("? ", ("s", "d", "n"), 1)
            assert False, "should raise EOFError"
        except EOFError:
            pass
    finally:
        sys.stdin.close()
        sys.stdin = old


def cand(prs, state=None, cfg=CFG, now=1000):
    return [(p["number"], why) for p, why in r.candidates(prs, "Me", cfg, state or {}, now)]


def test_candidates_basic_filters():
    assert cand([pr(1)]) == [(1, "new")]
    assert cand([pr(1, author="ALICE")]) == [(1, "new")]
    assert cand([pr(1, author="bob")]) == []
    assert cand([pr(1, draft=True)]) == []


def test_candidates_my_review():
    assert cand([pr(1, reviews=[rev("s1")])]) == []
    assert cand([pr(1, reviews=[rev("s1", login="other")])]) == [(1, "new")]
    assert cand([pr(1, reviews=[rev("s1", state="PENDING")])]) == [(1, "new")]
    assert cand([pr(1, reviews=[rev("s1", state="DISMISSED")])]) == [(1, "new")]
    assert cand([pr(1, sha="s2", reviews=[rev("s1")])]) == [(1, "new commits")]


def test_candidates_review_requested():
    direct = [{"__typename": "User", "login": "me"}]
    team = [{"__typename": "Team", "name": "devs"}]
    assert cand([pr(1, author="bob", requests=direct)]) == [(1, "review requested")]
    assert cand([pr(1, author="bob", requests=team)]) == []
    assert cand([pr(1, author="bob", draft=True, requests=direct)]) == []


def test_candidates_state():
    due = {"o/r#1": {"sha": "s1", "decision": "snoozed", "until": 500}}
    wait = {"o/r#1": {"sha": "s1", "decision": "snoozed", "until": 2000}}
    assert cand([pr(1)], due) == [(1, "reminder")]
    assert cand([pr(1)], wait) == []
    done = {"o/r#1": {"sha": "s1", "decision": "reviewed"}}
    assert cand([pr(1)], done) == []
    assert cand([pr(1, sha="s2")], done) == [(1, "new commits")]
    dismissed = {"o/r#1": {"sha": "s1", "decision": "dismissed"}}
    assert cand([pr(1, sha="s2")], dismissed) == []


def test_candidates_null_author_does_not_crash():
    ghost = pr(1)
    ghost["author"] = None
    assert cand([ghost]) == []


def test_baseline():
    direct = [{"__typename": "User", "login": "me"}]
    prs = [pr(1), pr(2, author="bob"), pr(3, author="bob", requests=direct), pr(4, draft=True)]
    state = r.baseline(prs, "Me", CFG, 1000)
    assert state == {"o/r#1": {"sha": "s1", "decision": "dismissed"}}
    assert cand(prs, state) == [(3, "review requested")]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
