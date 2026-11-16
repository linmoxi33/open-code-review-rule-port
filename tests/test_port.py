#!/usr/bin/env python3
"""Tests for ocr-rule-port (stdlib unittest only)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HERE), "bin", "ocr_rule_port.py")
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "bin"))
import ocr_rule_port as port  # noqa: E402


def make_tree(files):
    root = tempfile.mkdtemp(prefix="ocrport-")
    for rel in files:
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write("x\n")
    return root


def make_rules(root, entries):
    path = os.path.join(root, "rule.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rules": entries}, fh)
    return path


def run(args, cwd=None):
    proc = subprocess.run([sys.executable, BIN] + args, capture_output=True,
                          text=True, cwd=cwd)
    return proc.returncode, proc.stdout, proc.stderr


class TestGlob(unittest.TestCase):
    def test_star_does_not_cross_slash(self):
        p = port.Pattern("internal/*.go")
        self.assertTrue(p.matches("internal/a.go"))
        self.assertFalse(p.matches("internal/llm/a.go"))

    def test_double_star_crosses_dirs(self):
        p = port.Pattern("internal/**/*.go")
        self.assertTrue(p.matches("internal/a.go"))
        self.assertTrue(p.matches("internal/llm/deep/a.go"))

    def test_brace_expansion(self):
        p = port.Pattern("*.{ts,tsx}")
        self.assertTrue(p.matches("a.ts"))
        self.assertTrue(p.matches("b.tsx"))
        self.assertFalse(p.matches("c.js"))
        self.assertEqual(port.expand_braces("a.{b,c}.{d,e}"),
                         ["a.b.d", "a.b.e", "a.c.d", "a.c.e"])

    def test_single_char_wildcard(self):
        p = port.Pattern("src/api/?.go")
        self.assertTrue(p.matches("src/api/x.go"))
        self.assertFalse(p.matches("src/api/xy.go"))

    def test_char_class(self):
        p = port.Pattern("src/[ab].go")
        self.assertTrue(p.matches("src/a.go"))
        self.assertFalse(p.matches("src/c.go"))

    def test_case_insensitive(self):
        p = port.Pattern("Internal/LLM/*.GO")
        self.assertTrue(p.matches("internal/llm/providers.go"))

    def test_unbalanced_brace_is_input_error(self):
        with self.assertRaises(port.InputError):
            port.Pattern("a/{b,c")


class TestResolution(unittest.TestCase):
    def setUp(self):
        self.root = make_tree([
            "internal/llm/providers.go",
            "internal/agent/run.go",
            "README.md",
            "web/app.tsx",
        ])
        self.entries = port.load_ruleset(make_rules(self.root, [
            {"path": "internal/llm/*.go", "rule": "Rule A"},
            {"path": "internal/**/*.go", "rule": "Rule B"},
            {"path": "**/*.md", "rule": "Rule C"},
            {"path": "**/*.{ts,tsx}", "rule": "Rule D"},
        ]))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_first_match_wins(self):
        files = ["internal/llm/providers.go", "internal/agent/run.go"]
        self.assertEqual(port.resolve(self.entries, files),
                         {"internal/llm/providers.go": 0, "internal/agent/run.go": 1})

    def test_unmatched_file_is_none(self):
        m = port.resolve(self.entries, ["README.md"])
        self.assertEqual(m["README.md"], 2)
        m = port.resolve(self.entries, ["main.py"])
        self.assertIsNone(m["main.py"])

    def test_shadow_detected_on_tree(self):
        files = port.scan_files(self.root)
        mapping = port.resolve(self.entries, files)
        findings = port.findings_for(self.entries, files, mapping)
        codes = [(f["code"], f["rule_index"]) for f in findings]
        # Rule B still fires on internal/agent/run.go, so nothing is shadowed here.
        self.assertEqual(codes, [])

    def test_fully_shadowed_rule_reported(self):
        entries = port.load_ruleset(make_rules(self.root, [
            {"path": "internal/**", "rule": "broad"},
            {"path": "internal/llm/*.go", "rule": "narrow, never wins"},
        ]))
        files = port.scan_files(self.root)
        mapping = port.resolve(entries, files)
        findings = port.findings_for(entries, files, mapping)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["rule_index"], 1)
        self.assertEqual(findings[0]["code"], "shadowed_rule")
        self.assertIn("earlier", findings[0]["message"])

    def test_malformed_ruleset_rejected(self):
        bad = os.path.join(self.root, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"rules": [{"path": "", "rule": "x"}]}, fh)
        with self.assertRaises(port.InputError):
            port.load_ruleset(bad)


class TestCli(unittest.TestCase):
    def setUp(self):
        self.root = make_tree([
            "internal/llm/providers.go",
            "internal/agent/run.go",
            "README.md",
        ])
        self.rules = make_rules(self.root, [
            {"path": "internal/llm/*.go", "rule": "Rule A\n\nSecond line."},
            {"path": "internal/**/*.go", "rule": "Rule B"},
            {"path": "**/*.md", "rule": "Rule C", "merge_system_rule": True},
        ])
        self.out = os.path.join(self.root, "out")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_port_exit_zero_and_json_parsable(self):
        code, out, err = run(["port", "--rules", self.rules, "--root", self.root,
                              "--out", self.out, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertEqual(doc["schema"], port.SCHEMA)
        self.assertEqual(doc["files_scanned"], 3)
        self.assertEqual(doc["files_with_rule"], 3)

    def test_agents_md_puts_shadowed_rule_last(self):
        run(["port", "--rules", self.rules, "--root", self.root, "--out", self.out])
        text = open(os.path.join(self.out, "AGENTS.md"), encoding="utf-8").read()
        self.assertIn("Rule 0", text)
        self.assertIn("Second line.", text)
        # Rule 1 is not shadowed: internal/agent/run.go still needs it.
        self.assertIn("`internal/agent/run.go`", text)
        self.assertIn("resolved-map.json", text)

    def test_resolved_map_assigns_one_rule_per_file(self):
        run(["port", "--rules", self.rules, "--root", self.root, "--out", self.out])
        m = json.load(open(os.path.join(self.out, "resolved-map.json"), encoding="utf-8"))
        self.assertEqual(m["resolved"]["internal/llm/providers.go"], 0)
        self.assertEqual(m["resolved"]["internal/agent/run.go"], 1)
        self.assertEqual(m["resolved"]["README.md"], 2)

    def test_shadowed_ruleset_exits_one(self):
        rules = make_rules(self.root, [
            {"path": "internal/**", "rule": "broad"},
            {"path": "internal/llm/*.go", "rule": "dead"},
        ])
        code, out, _ = run(["port", "--rules", rules, "--root", self.root,
                            "--out", self.out, "--json"])
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertEqual(doc["findings"][0]["code"], "shadowed_rule")

    def test_check_reports_up_to_date_then_drift(self):
        run(["port", "--rules", self.rules, "--root", self.root, "--out", self.out])
        code, out, _ = run(["port", "--rules", self.rules, "--root", self.root,
                            "--out", self.out, "--check", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["up_to_date"])
        with open(os.path.join(self.out, "AGENTS.md"), "a", encoding="utf-8") as fh:
            fh.write("tampered\n")
        code, out, _ = run(["port", "--rules", self.rules, "--root", self.root,
                            "--out", self.out, "--check", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["drift"], ["AGENTS.md"])

    def test_missing_ruleset_exits_two(self):
        code, _, err = run(["port", "--rules", os.path.join(self.root, "nope.json"),
                            "--root", self.root, "--out", self.out])
        self.assertEqual(code, 2)
        self.assertIn("ruleset not found", err)

    def test_no_subcommand_exits_two(self):
        code, _, _ = run([])
        self.assertEqual(code, 2)

    def test_resolve_table(self):
        code, out, _ = run(["resolve", "--rules", self.rules, "--root", self.root])
        self.assertEqual(code, 0)
        self.assertIn("0\tinternal/llm/providers.go", out)

    def test_output_is_deterministic(self):
        run(["port", "--rules", self.rules, "--root", self.root, "--out", self.out])
        first = open(os.path.join(self.out, "AGENTS.md"), encoding="utf-8").read()
        run(["port", "--rules", self.rules, "--root", self.root, "--out", self.out])
        second = open(os.path.join(self.out, "AGENTS.md"), encoding="utf-8").read()
        self.assertEqual(first, second)
        self.assertNotIn(str(self.root), first)

    def test_git_dir_is_not_scanned(self):
        os.makedirs(os.path.join(self.root, ".git", "objects"))
        open(os.path.join(self.root, ".git", "objects", "x.go"), "w").close()
        code, out, _ = run(["port", "--rules", self.rules, "--root", self.root,
                            "--out", self.out, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["files_scanned"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
