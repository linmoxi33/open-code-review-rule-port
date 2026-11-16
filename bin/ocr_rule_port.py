#!/usr/bin/env python3
"""ocr-rule-port: resolve an Open Code Review ruleset offline and port it to
portable agent instructions without changing which rule wins.

Zero third-party dependencies. Python 3.8+.
"""
import argparse
import json
import os
import re
import sys

SCHEMA = "ocr-rule-port/1"
OUT_AGENTS = "AGENTS.md"
OUT_MAP = "resolved-map.json"
OUT_MANIFEST = "manifest.json"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "vendor", "dist", "build"}


class InputError(Exception):
    """Bad usage or unusable input -> exit code 2."""


# ---------------------------------------------------------------- glob -> regex
def expand_braces(pattern):
    """Expand {a,b} alternatives (doublestar semantics), recursively."""
    out = [""]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "{":
            depth, j = 0, i
            while j < len(pattern):
                if pattern[j] == "{":
                    depth += 1
                elif pattern[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(pattern):
                raise InputError("unbalanced '{' in glob: %r" % pattern)
            body = pattern[i + 1:j]
            parts, depth, cur = [], 0, ""
            for c in body:
                if c == "," and depth == 0:
                    parts.append(cur)
                    cur = ""
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                cur += c
            parts.append(cur)
            out = [p + alt + pattern[j + 1:] for p in out for alt in parts]
            return [e for p in out for e in expand_braces(p)]
        out = [p + ch for p in out]
        i += 1
    return out


def compile_glob(pattern):
    """Compile one brace-free doublestar glob to an anchored regex string.

    Matching is case-insensitive: callers lowercase both sides.
    """
    i, n = 0, len(pattern)
    out = []
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                raise InputError("unbalanced '[' in glob: %r" % pattern)
            body = pattern[i + 1:j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = j + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "^" + "".join(out) + "$"


class Pattern:
    def __init__(self, raw):
        self.raw = raw
        self.variants = expand_braces(raw)
        self.regexes = [re.compile(compile_glob(v.lower())) for v in self.variants]

    def matches(self, path_lower):
        return any(r.match(path_lower) for r in self.regexes)


# ---------------------------------------------------------------- ruleset
def load_ruleset(path):
    if not os.path.isfile(path):
        raise InputError("ruleset not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise InputError("cannot parse ruleset %s: %s" % (path, exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        raise InputError("ruleset must be an object with a 'rules' array")
    if not doc["rules"]:
        raise InputError("ruleset has no rules")
    entries = []
    for idx, raw in enumerate(doc["rules"]):
        if not isinstance(raw, dict):
            raise InputError("rules[%d] is not an object" % idx)
        pat = raw.get("path")
        body = raw.get("rule")
        if not isinstance(pat, str) or not pat.strip():
            raise InputError("rules[%d].path must be a non-empty string" % idx)
        if not isinstance(body, str) or not body.strip():
            raise InputError("rules[%d].rule must be a non-empty string" % idx)
        merge = raw.get("merge_system_rule", False)
        if not isinstance(merge, bool):
            raise InputError("rules[%d].merge_system_rule must be a boolean" % idx)
        extra = sorted(set(raw) - {"path", "rule", "merge_system_rule"})
        entries.append({
            "index": idx,
            "path": pat,
            "rule": body,
            "merge_system_rule": merge,
            "unknown_keys": extra,
            "glob": Pattern(pat),
        })
    return entries


def scan_files(root, max_files=20000, exclude=(), skip_files=()):
    if not os.path.isdir(root):
        raise InputError("root is not a directory: %s" % root)
    excluded = [os.path.abspath(p) for p in exclude]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        abs_dir = os.path.abspath(dirpath)
        if any(abs_dir == e or abs_dir.startswith(e + os.sep) for e in excluded):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(filenames):
            rel = name if rel_dir == "." else os.path.join(rel_dir, name).replace(os.sep, "/")
            if rel in skip_files:
                continue
            found.append(rel)
            if len(found) >= max_files:
                return sorted(found)
    return sorted(found)


def resolve(entries, files):
    """First declared entry whose glob matches wins; None means built-in default."""
    mapping = {}
    for f in files:
        low = f.lower()
        hit = None
        for e in entries:
            if e["glob"].matches(low):
                hit = e["index"]
                break
        mapping[f] = hit
    return mapping


def findings_for(entries, files, mapping):
    out = []
    for e in entries:
        if e["unknown_keys"]:
            out.append({
                "code": "unknown_key",
                "rule_index": e["index"],
                "message": "unknown field(s) %s are ignored by OCR and by this port"
                           % ", ".join(e["unknown_keys"]),
            })
    fired = {i for i in mapping.values() if i is not None}
    for e in entries:
        if e["index"] in fired:
            continue
        matched = [f for f in files if e["glob"].matches(f.lower())]
        out.append({
            "code": "shadowed_rule",
            "rule_index": e["index"],
            "message": ("never fires on this tree: %d file(s) match its glob but an earlier "
                        "entry wins them" % len(matched)) if matched else
                       "never fires: its glob matches 0 file(s) in the scanned tree",
            "matched_files": matched[:20],
        })
    return out


# ---------------------------------------------------------------- rendering
def _fence(body):
    ticks = "```"
    while ticks in body:
        ticks += "`"
    return ticks


def render_agents(entries, mapping, files, ruleset_sha):
    fired = {}
    for f, idx in mapping.items():
        if idx is not None:
            fired.setdefault(idx, []).append(f)
    lines = [
        "# Review instructions (ported from Open Code Review `.opencodereview/rule.json`)",
        "",
        "Generated by `ocr-rule-port` from ruleset sha256 `%s`." % ruleset_sha,
        "Every rule below already had OCR's **first-match-wins** resolution applied:",
        "a file appears under exactly one rule, so a later entry that OCR would have",
        "shadowed is never applied here either.",
        "",
        "Scanned files: %d. Files governed by a custom rule: %d."
        % (len(files), sum(len(v) for v in fired.values())),
        "",
    ]
    for e in entries:
        if e["index"] not in fired:
            continue
        matched = sorted(fired[e["index"]])
        lines.append("## Rule %d — `%s`" % (e["index"], e["path"]))
        lines.append("")
        lines.append("Applies to %d resolved file(s)%s:" % (
            len(matched), "" if not e["merge_system_rule"]
            else " (merged with OCR's built-in system rule in the source tool)"))
        lines.append("")
        for f in matched[:25]:
            lines.append("- `%s`" % f)
        if len(matched) > 25:
            lines.append("- ... and %d more (see `resolved-map.json`)" % (len(matched) - 25))
        lines.append("")
        fence = _fence(e["rule"].rstrip())
        lines.append(fence + "text")
        lines.append(e["rule"].rstrip())
        lines.append(fence)
        lines.append("")
    dead = [e for e in entries if e["index"] not in fired]
    if dead:
        lines.append("## Rules that never fire on this tree")
        lines.append("")
        lines.append("OCR would also never apply these to the scanned files, because an")
        lines.append("earlier entry matches every file they would match. They are left out")
        lines.append("on purpose: a naive glob-by-glob export would make them fire again.")
        lines.append("")
        for e in dead:
            lines.append("- Rule %d `%s`" % (e["index"], e["path"]))
        lines.append("")
    lines.append("Machine-readable map: `resolved-map.json` (file -> rule index).")
    return "\n".join(lines).rstrip() + "\n"


def sha256_text(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(entries, files, ruleset_sha):
    mapping = resolve(entries, files)
    findings = findings_for(entries, files, mapping)
    agents = render_agents(entries, mapping, files, ruleset_sha)
    map_doc = {
        "schema": SCHEMA,
        "ruleset_sha256": ruleset_sha,
        "files_scanned": len(files),
        "rules": [{"index": e["index"], "path": e["path"],
                   "merge_system_rule": e["merge_system_rule"]} for e in entries],
        "resolved": {f: mapping[f] for f in files},
    }
    map_text = json.dumps(map_doc, indent=2, sort_keys=True) + "\n"
    manifest = {
        "schema": SCHEMA,
        "ruleset_sha256": ruleset_sha,
        "files_scanned": len(files),
        "rules": len(entries),
        "rules_that_fire": len({i for i in mapping.values() if i is not None}),
        "files_with_rule": sum(1 for v in mapping.values() if v is not None),
        "findings": findings,
        "outputs": {
            OUT_AGENTS: sha256_text(agents),
            OUT_MAP: sha256_text(map_text),
        },
    }
    return mapping, findings, {OUT_AGENTS: agents, OUT_MAP: map_text}, manifest


# ---------------------------------------------------------------- commands
def cmd_port(args):
    entries = load_ruleset(args.rules)
    rel_rules = os.path.relpath(os.path.abspath(args.rules), os.path.abspath(args.root))
    skip = {rel_rules.replace(os.sep, "/")} if not rel_rules.startswith("..") else set()
    files = scan_files(args.root, args.max_files, exclude=[args.out], skip_files=skip)
    with open(args.rules, "rb") as fh:
        import hashlib
        ruleset_sha = hashlib.sha256(fh.read()).hexdigest()
    mapping, findings, outputs, manifest = build(entries, files, ruleset_sha)

    if args.check:
        existing = os.path.join(args.out, OUT_MANIFEST)
        if not os.path.isfile(existing):
            raise InputError("--check requires a previous port at %s" % existing)
        with open(existing, "r", encoding="utf-8") as fh:
            old = json.load(fh)
        drift = []
        for name, text in outputs.items():
            expected = old.get("outputs", {}).get(name)
            on_disk = None
            disk_path = os.path.join(args.out, name)
            if os.path.isfile(disk_path):
                with open(disk_path, "r", encoding="utf-8") as fh:
                    on_disk = sha256_text(fh.read())
            if expected != on_disk:
                drift.append(name)
            elif expected != sha256_text(text):
                drift.append(name)
        if old.get("ruleset_sha256") != ruleset_sha:
            drift.append("ruleset")
        result = {
            "schema": SCHEMA, "command": "check", "drift": sorted(drift),
            "up_to_date": not drift,
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print("up to date" if not drift else "drifted: " + ", ".join(sorted(drift)))
        return 0 if not drift else 1

    os.makedirs(args.out, exist_ok=True)
    for name, text in outputs.items():
        with open(os.path.join(args.out, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    with open(os.path.join(args.out, OUT_MANIFEST), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    if args.json:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    else:
        print("scanned %d file(s), %d custom rule(s), %d file(s) with a rule, %d finding(s)"
              % (manifest["files_scanned"], manifest["rules"],
                 manifest["files_with_rule"], len(findings)))
        for f in findings:
            print("  %s rule %d: %s" % (f["code"], f["rule_index"], f["message"]))
        print("wrote %s, %s, %s" % (OUT_AGENTS, OUT_MAP, OUT_MANIFEST))
    return 1 if findings else 0


def cmd_resolve(args):
    entries = load_ruleset(args.rules)
    files = scan_files(args.root, args.max_files)
    mapping = resolve(entries, files)
    if args.json:
        print(json.dumps({f: mapping[f] for f in files}, indent=2, sort_keys=True))
    else:
        for f in files:
            print("%s\t%s" % (mapping[f] if mapping[f] is not None else "-", f))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ocr-rule-port",
        description="Resolve an Open Code Review .opencodereview/rule.json ruleset "
                    "offline and port it to AGENTS.md without changing first-match-wins.")
    sub = parser.add_subparsers(dest="command")
    for name in ("port", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("--rules", default=".opencodereview/rule.json")
        p.add_argument("--root", default=".")
        p.add_argument("--json", action="store_true")
        p.add_argument("--max-files", type=int, default=20000)
        if name == "port":
            p.add_argument("--out", default="ported-rules")
            p.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        if args.command == "port":
            return cmd_port(args)
        return cmd_resolve(args)
    except InputError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
