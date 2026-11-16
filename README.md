# ocr-rule-port

Resolve an Open Code Review `.opencodereview/rule.json` ruleset offline and port it to a
portable `AGENTS.md` — **without changing which rule wins**. Needed now because teams are
already writing path-scoped review rules for one agent harness and then discovering that a
naive export (one glob per rule) silently turns OCR's *first-match-wins* replacement into a
*union*, so rules OCR would never apply start firing on files they were never meant to touch.

Upstream: https://github.com/alibaba/open-code-review

## What the upstream is, and what it does not do

[`alibaba/open-code-review`](https://github.com/alibaba/open-code-review) is a code-review
tool whose review rules are a JSON file at `.opencodereview/rule.json`: an ordered array of
`{path, rule, merge_system_rule}` entries, where `path` is a doublestar glob and `rule` is a
free-text prompt. Its own documentation states the resolution rule and the glob dialect:

- `rules` are evaluated **in declaration order**, "the first `path` whose glob matches the
  file determines the prompt OCR sends to the model for that file"
  ([review-rules.md#L58](https://github.com/alibaba/open-code-review/blob/main/pages/src/content/docs/en/review-rules.md#L58)).
- If two of your own entries match the same file, "the first one still wins outright —
  `merge_system_rule` does not combine them with each other"
  ([review-rules.md#L120](https://github.com/alibaba/open-code-review/blob/main/pages/src/content/docs/en/review-rules.md#L120)).
- Matching uses doublestar v4: `*`, `**`, `{a,b}`, `?`, `[abc]`, and is case-insensitive
  ([review-rules.md#L127](https://github.com/alibaba/open-code-review/blob/main/pages/src/content/docs/en/review-rules.md#L127)).

The rules are resolved **by OCR itself** ("OCR handles file selection and rule resolution",
[README#L168](https://github.com/alibaba/open-code-review/blob/main/README.md#L168)); the
repo ships no exporter that produces an artifact for another harness, and `gh search code
"merge_system_rule"` returns only OCR's own tree. So the rule text is locked to a tool that
must be installed, and any hand-written copy into `AGENTS.md` / Cursor rules loses the
ordering semantics above.

## What this tool does

- Re-implements the documented doublestar subset (`*`, `**`, `{a,b}`, `?`, `[abc]`,
  case-insensitive) and the documented ordering, offline, in one dependency-free Python file.
- Applies that resolution to a real directory tree and emits:
  - `AGENTS.md` — one section per rule that actually fires, listing the **exact resolved
    files** (no globs, so no second matching dialect can reinterpret them);
  - `resolved-map.json` — `file -> rule index`, the machine-checkable ground truth;
  - `manifest.json` — input ruleset sha256 + output sha256s.
- Reports rules that **never fire on the scanned tree** because an earlier entry wins every
  file they would match (exit code `1`). These are deliberately omitted from `AGENTS.md`;
  re-adding them as globs is exactly the bug this tool exists to prevent.
- `--check` re-reads the on-disk artifacts, re-hashes them and recomputes the resolution,
  failing with `1` on drift (edited output, changed ruleset, changed tree).

## What it does not do

- It does not run a review, call a model, or talk to the network.
- It does not claim byte-identical parity with OCR's full internal matcher: OCR's exact
  precedence also involves its system layer and `include`/`exclude`/`secret_exclude`
  filters, which are not in `rule.json`. Only the `rules` array is ported, and that is
  stated in every generated `AGENTS.md`.
- It does not merge `merge_system_rule: true` entries with anything (the system rule is not
  public); such a rule is ported as-is and labelled.

## Install

No dependencies, Python 3.8+:

```bash
git clone git@github-linmoxi33:linmoxi33/open-code-review-rule-port.git
cd open-code-review-rule-port
python3 bin/ocr_rule_port.py port --rules examples/in/rule.json --root examples/in/tree --out /tmp/ported
```

This example exits `1` on purpose: the fixture contains a rule that an earlier entry
shadows, which is the finding the tool exists to surface.

## Usage

```bash
# Port: writes AGENTS.md + resolved-map.json + manifest.json
python3 bin/ocr_rule_port.py port --rules .opencodereview/rule.json --root . --out ported-rules

# Same, machine-readable
python3 bin/ocr_rule_port.py port --rules .opencodereview/rule.json --root . --out ported-rules --json

# Just show the resolution table (rule index, tab, path)
python3 bin/ocr_rule_port.py resolve --rules .opencodereview/rule.json --root .

# Fail CI if the ported artifacts are stale or hand-edited
python3 bin/ocr_rule_port.py port --rules .opencodereview/rule.json --root . --out ported-rules --check
```

`--json` example (abridged, from `examples/in`):

```json
{
  "schema": "ocr-rule-port/1",
  "files_scanned": 5,
  "rules": 4,
  "rules_that_fire": 3,
  "files_with_rule": 4,
  "findings": [
    {"code": "shadowed_rule", "rule_index": 3,
     "message": "never fires on this tree: 1 file(s) match its glob but an earlier entry wins them",
     "matched_files": ["internal/llm/providers_test.go"]}
  ],
  "outputs": {"AGENTS.md": "…", "resolved-map.json": "…"}
}
```

Exit codes:

| code | meaning |
| --- | --- |
| `0` | ported / checked, no findings, no drift |
| `1` | findings (a rule is shadowed on this tree, or an unknown field is present) **or** `--check` found drift |
| `2` | usage error or unusable input (missing/invalid ruleset, bad glob, `--check` without a previous port) |

## Evidence this port is needed (not hypothetical)

`examples/in` reproduces the failure mode with 4 rules / 5 files: rule 0
(`internal/llm/*.go`) matches `providers_test.go`, so rule 3
(`internal/llm/providers_test.go`) can never fire — a glob-by-glob export would attach both
prompts to that file. The tool reports the shadowing (`exit 1`) and emits only rule 0 for it;
`tests/test_port.py::test_fully_shadowed_rule_reported` asserts that behaviour.

Real upstream run (path list from `gh api repos/alibaba/open-code-review/git/trees/HEAD`,
rule file from the same repo, 2026-11-16): **1184 files scanned, 1 rule, 1 resolved file**
(`internal/llm/providers.go`), 0 findings, exit 0 — the upstream ruleset is path-scoped to a
single file, which is exactly why a portable mapping (not a glob copy) is the honest export.

## Limits

- Findings are scoped to the scanned tree: "shadowed" here means "no file in this tree can
  reach it", not a proof about all possible paths.
- Directory scanning skips `.git`, `node_modules`, `__pycache__`, `.venv`, `vendor`, `dist`,
  `build` and dot-directories, and skips the ruleset file itself.
- `[!a]` negated classes are passed through to Python's regex semantics, which match
  doublestar for the ASCII cases covered by tests.
- Only `rules[]` is ported; `include` / `exclude` / `secret_exclude` / system rules are out
  of scope (see above).

## Tests

```bash
python3 tests/test_port.py
```

22 tests, 12 of them behavioural (subprocess: exit codes, `--json` parsing, drift detection,
determinism, `.git` exclusion).

## License

MIT — see `LICENSE`.
