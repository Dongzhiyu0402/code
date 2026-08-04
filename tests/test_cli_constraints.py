"""CLI tests for cfgdrift v0.6.0 — constraint subcommand + diff/scan/daemon switches (T03).

Covers:

1. ``constraint add --rule JSON`` (valid / invalid JSON / invalid constraint /
   duplicate id -> exit 2; ``--disable``).
2. ``constraint list`` with ``--source builtin|user|all`` (id / type /
   severity / enabled / source).
3. ``constraint remove`` / ``enable`` / ``disable`` round-trips.
4. ``diff``/``scan`` ``--builtin/--no-builtin`` + ``--constraints`` file.
5. ``daemon start`` argv passthrough + ``build_worker_command`` emission.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
PY = sys.executable

sys.path.insert(0, SRC)

from cfgdrift.daemon.worker import build_worker_command  # noqa: E402
from cfgdrift.rules.constraints import ConstraintConfig, default_path  # noqa: E402

CONF_JSON = '{"server": {"port": 8080}, "tls": {"enabled": false}}\n'


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _run_cli(home, args, store=None):
    env = dict(os.environ)
    env["CFGDRIFT_HOME"] = str(home)
    cmd = [PY, "-m", "cfgdrift.cli"]
    if store:
        cmd += ["--store", str(store)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=180)


def _setup_baseline(tmp_path, content=CONF_JSON):
    home = tmp_path / "home"
    store_path = tmp_path / "db" / "cfgdrift.db"
    conf = tmp_path / "conf"
    _write(str(conf / "app.json"), content)
    r = _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
    assert r.returncode == 0, r.stderr
    return home, store_path, conf


# ---------------------------------------------------------------------------
# constraint add
# ---------------------------------------------------------------------------

class TestConstraintAdd:
    RULE = json.dumps({
        "id": "my_port",
        "type": "range",
        "keys": ["server.port"],
        "min": 1,
        "max": 65535,
        "message": "my port rule",
    })

    def test_add_valid(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        r = _run_cli(home, ["constraint", "add", "--rule", self.RULE],
                     store=store_path)
        assert r.returncode == 0, r.stderr
        assert "constraint 'my_port' added" in r.stdout
        rules = ConstraintConfig.list_rules(str(home / "constraints.yaml"))
        assert [x.id for x in rules] == ["my_port"]
        assert rules[0].source == "user" and rules[0].min == 1

    def test_add_duplicate_id_exit2(self, tmp_path):
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        assert _run_cli(home, ["constraint", "add", "--rule", self.RULE],
                        store=store_path).returncode == 0
        r = _run_cli(home, ["constraint", "add", "--rule", self.RULE],
                     store=store_path)
        assert r.returncode == 2
        assert "already exists" in r.stderr

    def test_add_invalid_json_exit2(self, tmp_path):
        home = tmp_path / "home"
        r = _run_cli(home, ["constraint", "add", "--rule", "{not json"])
        assert r.returncode == 2
        assert "invalid --rule JSON" in r.stderr

    def test_add_invalid_constraint_exit2(self, tmp_path):
        home = tmp_path / "home"
        bad = json.dumps({"id": "x", "type": "range", "message": "m",
                          "keys": [], "min": 1})
        r = _run_cli(home, ["constraint", "add", "--rule", bad])
        assert r.returncode == 2

    def test_add_disable_flag(self, tmp_path):
        home = tmp_path / "home"
        r = _run_cli(home, ["constraint", "add", "--rule", self.RULE, "--disable"])
        assert r.returncode == 0, r.stderr
        rules = ConstraintConfig.list_rules(str(home / "constraints.yaml"))
        assert rules[0].enabled is False


# ---------------------------------------------------------------------------
# constraint list / remove / enable / disable
# ---------------------------------------------------------------------------

class TestConstraintLifecycle:
    RULE = json.dumps({
        "id": "my_port",
        "type": "range",
        "keys": ["server.port"],
        "min": 1,
        "max": 65535,
        "message": "my port rule",
    })

    def test_list_sources(self, tmp_path):
        home = tmp_path / "home"
        _run_cli(home, ["constraint", "add", "--rule", self.RULE])
        # builtin: 20 rules, all source=builtin
        r = _run_cli(home, ["constraint", "list", "--source", "builtin"])
        assert r.returncode == 0
        assert r.stdout.count("source=builtin") == 20
        # user: only the added one
        r = _run_cli(home, ["constraint", "list", "--source", "user"])
        assert "my_port" in r.stdout and "source=user" in r.stdout
        assert "http_port_range" not in r.stdout
        # all (default): effective view includes both
        r = _run_cli(home, ["constraint", "list"])
        assert "my_port" in r.stdout and "http_port_range" in r.stdout
        assert "type=range" in r.stdout and "severity=WARN" in r.stdout
        assert "enabled=yes" in r.stdout

    def test_remove_enable_disable(self, tmp_path):
        home = tmp_path / "home"
        _run_cli(home, ["constraint", "add", "--rule", self.RULE])
        path = str(home / "constraints.yaml")
        assert _run_cli(home, ["constraint", "disable", "my_port"]).returncode == 0
        assert ConstraintConfig.list_rules(path)[0].enabled is False
        assert _run_cli(home, ["constraint", "enable", "my_port"]).returncode == 0
        assert ConstraintConfig.list_rules(path)[0].enabled is True
        assert _run_cli(home, ["constraint", "remove", "my_port"]).returncode == 0
        assert ConstraintConfig.list_rules(path) == []
        # remove missing -> exit 2
        assert _run_cli(home, ["constraint", "remove", "nope"]).returncode == 2
        assert _run_cli(home, ["constraint", "enable", "nope"]).returncode == 2


# ---------------------------------------------------------------------------
# diff / scan switches
# ---------------------------------------------------------------------------

class TestDiffScanSwitches:
    def test_diff_default_builtin_triggers(self, tmp_path):
        home, store_path, conf = _setup_baseline(tmp_path)
        _write(str(conf / "app.json"),
               '{"server": {"port": 99999}, "tls": {"enabled": false}}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod"],
                     store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint http_port_range [range]" in r.stdout
        assert "[CRITICAL]" in r.stdout  # upgraded

    def test_diff_no_builtin_disables(self, tmp_path):
        home, store_path, conf = _setup_baseline(tmp_path)
        _write(str(conf / "app.json"),
               '{"server": {"port": 99999}, "tls": {"enabled": false}}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                            "--no-builtin"], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint" not in r.stdout
        assert "[WARN]" in r.stdout  # built-in severity kept

    def test_scan_builtin_flag(self, tmp_path):
        home, store_path, conf = _setup_baseline(tmp_path)
        _write(str(conf / "app.json"),
               '{"server": {"port": 99999}, "tls": {"enabled": false}}\n')
        r = _run_cli(home, ["scan", str(conf), "--baseline", "prod",
                            "--no-builtin"], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint" not in r.stdout

    def test_constraints_extra_file(self, tmp_path):
        home, store_path, conf = _setup_baseline(tmp_path)
        extra = tmp_path / "extra.yaml"
        _write(str(extra), """
version: 1
rules:
  - id: custom_worker
    type: range
    keys: [worker_processes]
    min: 2
    max: 64
    message: "custom worker range"
""")
        _write(str(conf / "app.json"),
               '{"server": {"port": 8080}, "worker_processes": 1}\n')
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                            "--constraints", str(extra)], store=store_path)
        assert r.returncode == 1, r.stderr
        assert "constraint custom_worker [range]" in r.stdout

    def test_constraints_missing_file_exit2(self, tmp_path):
        home, store_path, conf = _setup_baseline(tmp_path)
        r = _run_cli(home, ["diff", str(conf), "--baseline", "prod",
                            "--constraints", str(tmp_path / "nope.yaml")],
                     store=store_path)
        # click's Path(exists=True) validation rejects it with exit 2
        assert r.returncode == 2
        assert "does not exist" in r.stderr or "not found" in r.stderr


# ---------------------------------------------------------------------------
# daemon passthrough
# ---------------------------------------------------------------------------

class TestDaemonPassthrough:
    def test_build_worker_command_emits_flags(self):
        cmd = build_worker_command(
            "C:/home", {
                "store": "s.db", "baseline": "b", "targets": ["/x"],
                "builtin": False,
                "constraint_paths": ["C:/home/extra1.yaml", "C:/home/extra2.yaml"],
            },
        )
        assert "--no-builtin" in cmd
        assert "--constraints" in cmd
        i1 = cmd.index("--constraints")
        assert cmd[i1 + 1] == "C:/home/extra1.yaml"
        assert cmd.count("--constraints") == 2

    def test_build_worker_command_default_no_flags(self):
        cmd = build_worker_command(
            "C:/home", {"store": "s.db", "baseline": "b", "targets": ["/x"]},
        )
        assert "--no-builtin" not in cmd
        assert "--constraints" not in cmd

    def test_daemon_start_foreground_argv(self, tmp_path):
        # --foreground runs the worker in-process with the parsed argv; use an
        # interval-less smoke by checking the help/parse path instead: the
        # worker parser accepts --no-builtin and --constraints.
        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), CONF_JSON)
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        # worker main with unknown paths would loop; just verify the worker
        # argparse accepts the flags via --help (parse path).
        r = subprocess.run(
            [PY, "-m", "cfgdrift.daemon.worker", "--help"],
            capture_output=True, text=True, env=dict(os.environ), timeout=60,
        )
        assert r.returncode == 0
        assert "--no-builtin" in r.stdout
        assert "--constraints" in r.stdout

    def test_worker_argv_parses_builtin_flag_pair(self, tmp_path):
        """Regression (BUG-1): argparse must parse --builtin/--no-builtin as a
        real boolean flag pair, NOT as a value-taking option.

        The old ``--builtin/--no-builtin`` single-argument spelling made
        argparse treat it as one long option: ``--no-builtin`` -> "unrecognized
        arguments" (SystemExit 2) and ``--builtin`` -> "expected one argument"
        (SystemExit 2), so ``daemon start --no-builtin`` could never start.
        This test parses real argv through ``worker.main`` (run_with_opts is
        patched so no scan loop runs) and asserts the resolved opts.
        """
        import unittest.mock

        import cfgdrift.daemon.worker as worker_mod

        base = ["--home", str(tmp_path / "home"), "--store", "s.db",
                "--baseline", "prod", "--path", str(tmp_path)]

        def _run(argv):
            with unittest.mock.patch.object(
                worker_mod, "run_with_opts", return_value=0
            ) as rwo:
                rc = worker_mod.main(list(base) + list(argv))
            assert rc == 0
            return rwo.call_args[0][0]

        # --no-builtin -> builtin False
        assert _run(["--no-builtin"])["builtin"] is False
        # --builtin (explicit) -> builtin True
        assert _run(["--builtin"])["builtin"] is True
        # default (no flag) -> builtin True
        assert _run([])["builtin"] is True
        # --no-builtin --constraints f.yaml combination
        extra = tmp_path / "extra.yaml"
        _write(str(extra), "version: 1\nrules: []\n")
        opts = _run(["--no-builtin", "--constraints", str(extra)])
        assert opts["builtin"] is False
        assert opts["constraint_paths"] == [str(extra)]

    def test_daemon_start_foreground_no_builtin_runs(self, tmp_path):
        """BUG-1 end-to-end: ``daemon start --no-builtin --foreground`` must
        enter the worker loop (rc != SystemExit-2).  We spawn the worker main
        with run_with_opts patched to a no-op so the scan loop never starts.
        """
        import unittest.mock

        import cfgdrift.daemon.worker as worker_mod

        home = tmp_path / "home"
        store_path = tmp_path / "db" / "cfgdrift.db"
        conf = tmp_path / "conf"
        _write(str(conf / "app.json"), CONF_JSON)
        _run_cli(home, ["baseline", "create", "prod", "--scan-root", str(conf)],
                 store=store_path)
        captured = {}

        def fake_run_with_opts(opts):
            captured["opts"] = opts
            return 0

        with unittest.mock.patch.object(
            worker_mod, "run_with_opts", side_effect=fake_run_with_opts
        ):
            rc = worker_mod.main([
                "--home", str(home), "--store", str(store_path),
                "--baseline", "prod", "--format", "auto",
                "--interval", "1", "--path", str(conf),
                "--no-builtin", "--foreground",
            ])
        assert rc == 0  # would be 2 (SystemExit) if argparse rejected --no-builtin
        assert captured["opts"]["builtin"] is False
