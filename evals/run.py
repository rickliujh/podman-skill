#!/usr/bin/env python3
"""Tool-agnostic eval runner for the podman skill.

Nothing here is tied to a particular CLI or vendor. Three modes:

  run     pipe each prompt to $EVAL_CMD, grade the reply
  emit    write prompt files you can paste into any assistant
  grade   grade replies you saved yourself

Graders are plain regexes, so scoring is deterministic, free and reproducible:
the same transcript always yields the same score, with no judge model involved.
"""
import argparse, json, os, re, shlex, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent


# ---------------------------------------------------------------- case loading
def load_yaml(text):
    try:
        import yaml
        return yaml.safe_load(text)
    except ImportError:
        return _mini_yaml(text)


def _mini_yaml(text):
    """Parser for the restricted subset the case files use, so the runner works
    on a bare Python with no third-party packages."""
    root = {}
    stack = [(-1, root)]
    lines, i = text.splitlines(), 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            parent.setdefault("__list__", []).append(_scalar(line[2:]))
            i += 1
            continue
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if rest == "|":
            block, i = [], i + 1
            base = None
            while i < len(lines):
                if lines[i].strip():
                    ind = len(lines[i]) - len(lines[i].lstrip())
                    if base is None:
                        base = ind
                    if ind <= indent:
                        break
                    block.append(lines[i][base:])
                else:
                    block.append("")
                i += 1
            parent[key] = "\n".join(block).rstrip() + "\n"
            continue
        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            parent[key] = [_scalar(x.strip()) for x in inner.split(",")] if inner else []
        elif rest == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(rest)
        i += 1
    return _fix_lists(root)


def _fix_lists(node):
    if isinstance(node, dict):
        if set(node.keys()) == {"__list__"}:
            return node["__list__"]
        out = {}
        for k, v in node.items():
            if k == "__list__":
                return v
            out[k] = _fix_lists(v)
        return out
    return node


_DQ = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t",
       "r": "\r", "0": "\0", "b": "\b", "f": "\f"}


def _unescape_dq(s):
    """YAML double-quoted escapes, so regexes survive the round trip."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in _DQ:
                out.append(_DQ[nxt])
                i += 2
                continue
            out.append(s[i])          # unknown escape: pass through verbatim
            i += 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] == '"':
        return _unescape_dq(s[1:-1])
    if len(s) >= 2 and s[0] == s[-1] and s[0] == "'":
        return s[1:-1].replace("''", "'")
    return {"true": True, "false": False}.get(s.lower(), s)


def load_cases(pattern=None, tags=None):
    cases = []
    for f in sorted((HERE / "cases").glob("*.yaml")):
        c = load_yaml(f.read_text())
        c["file"] = f.name
        c.setdefault("id", f.stem)
        c.setdefault("tags", [])
        c.setdefault("skill_should_fire", True)
        if pattern and pattern not in c["id"]:
            continue
        if tags and not (set(tags) & set(c["tags"])):
            continue
        cases.append(c)
    return cases


# ------------------------------------------------------------------- prompting
def skill_context():
    """The 'with skill' arm: what a harness would put in front of the model."""
    parts = [f"<skill name=\"podman\">\n{(SKILL / 'SKILL.md').read_text()}\n"]
    for ref in sorted((SKILL / "references").glob("*.md")):
        parts.append(f"\n<reference path=\"references/{ref.name}\">\n{ref.read_text()}\n</reference>\n")
    parts.append("</skill>\n")
    return "".join(parts)


def build_prompt(case, arm):
    if arm == "without":
        return case["prompt"]
    return (
        "You have access to the following skill. Follow it.\n\n"
        + skill_context()
        + "\n---\n\n"
        + case["prompt"]
    )


# --------------------------------------------------------------------- grading
def grade(case, reply):
    exp = case.get("expect", {}) or {}
    res = {"must": [], "must_not": [], "should": []}
    for pat in exp.get("must_mention", []) or []:
        res["must"].append((pat, bool(re.search(pat, reply))))
    for pat in exp.get("must_not_mention", []) or []:
        res["must_not"].append((pat, not re.search(pat, reply)))
    for pat in exp.get("should_mention", []) or []:
        res["should"].append((pat, bool(re.search(pat, reply))))
    hard = res["must"] + res["must_not"]
    hard_ok = all(ok for _, ok in hard)
    soft = res["should"]
    soft_score = (sum(ok for _, ok in soft) / len(soft)) if soft else 1.0
    score = 0.0 if not hard_ok else 0.7 + 0.3 * soft_score
    return score, hard_ok, res


def failures(res):
    out = []
    for pat, ok in res["must"]:
        if not ok:
            out.append(f"missing: {pat}")
    for pat, ok in res["must_not"]:
        if not ok:
            out.append(f"FORBIDDEN present: {pat}")
    for pat, ok in res["should"]:
        if not ok:
            out.append(f"(soft) missing: {pat}")
    return out


# ----------------------------------------------------------------- invocation
def invoke(cmd, prompt, timeout):
    """Two calling conventions, because CLIs disagree about where a prompt goes.

    Default is stdin (claude -p, ollama run, llm). If the command contains the
    literal {prompt}, the shell-quoted prompt is substituted there instead, for
    CLIs whose prompt flag takes an argument (gemini -p {prompt}).
    """
    kwargs = {}
    if "{prompt}" in cmd:
        cmd = cmd.replace("{prompt}", shlex.quote(prompt))
        # DEVNULL, never None: with None the child inherits this terminal's
        # stdin and an interactive CLI blocks on it forever.
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = prompt
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=timeout, **kwargs)
    if p.returncode != 0 and not p.stdout.strip():
        raise RuntimeError(f"command failed ({p.returncode}): {p.stderr.strip()[:400]}")
    return p.stdout


# ---------------------------------------------------------------------- output
C = {"g": "\033[32m", "r": "\033[31m", "y": "\033[33m", "b": "\033[1m", "0": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["run", "emit", "grade", "list"])
    ap.add_argument("dir", nargs="?", help="prompt/reply dir for emit and grade")
    ap.add_argument("--cmd", default=os.environ.get("EVAL_CMD"),
                    help="command producing the reply on stdout; gets the prompt "
                         "on stdin, or substituted at {prompt} if the command "
                         "contains that literal (or set $EVAL_CMD)")
    ap.add_argument("--case", help="substring filter on case id")
    ap.add_argument("--tag", action="append", help="filter by tag (repeatable)")
    ap.add_argument("--arms", default="with,without",
                    help="comma list: with,without (default both = ablation)")
    ap.add_argument("--timeout", type=int, default=120,
                    help="per-call timeout in seconds (default: 120)")
    ap.add_argument("--threshold", type=float, default=1.0,
                    help="exit 1 if any 'with' case scores below this")
    ap.add_argument("--json", metavar="PATH", help="write full results as JSON")
    args = ap.parse_args()

    cases = load_cases(args.case, args.tag)
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2
    arms = [a for a in args.arms.split(",") if a]

    if args.mode == "list":
        for c in cases:
            print(f"{c['id']:32} tags={','.join(c['tags'])}")
        return 0

    if args.mode == "emit":
        out = Path(args.dir or "prompts")
        out.mkdir(parents=True, exist_ok=True)
        for c in cases:
            for arm in arms:
                (out / f"{c['id']}.{arm}.prompt.txt").write_text(build_prompt(c, arm))
        print(f"wrote {len(cases) * len(arms)} prompts to {out}/")
        print("Paste each into any assistant, save the reply next to it as")
        print(f"  <case>.<arm>.reply.txt   then:  {sys.argv[0]} grade {out}")
        return 0

    if args.mode == "run":
        n = len(cases) * len(arms)
        print(f"running {n} call(s): {len(cases)} case(s) x {len(arms)} arm(s), "
              f"timeout {args.timeout}s each")
    results, missing, errors = [], 0, 0
    for c in cases:
        row = {"id": c["id"], "tags": c["tags"], "arms": {}}
        for arm in arms:
            if args.mode == "grade":
                f = Path(args.dir or "prompts") / f"{c['id']}.{arm}.reply.txt"
                if not f.exists():
                    missing += 1
                    continue
                reply = f.read_text()
            else:
                if not args.cmd:
                    print("error: --cmd or $EVAL_CMD required for 'run'", file=sys.stderr)
                    return 2
                print(f"  {c['id']} [{arm}] ...", end="", flush=True)
                t0 = time.time()
                try:
                    reply = invoke(args.cmd, build_prompt(c, arm), args.timeout)
                except subprocess.TimeoutExpired:
                    print(f" {C['r']}timeout after {args.timeout}s{C['0']}")
                    errors += 1
                    continue
                except Exception as e:  # noqa: BLE001
                    print(f" {C['r']}error{C['0']}: {e}")
                    errors += 1
                    continue
                print(f" {time.time() - t0:.1f}s")
            score, hard_ok, res = grade(c, reply)
            row["arms"][arm] = {"score": round(score, 3), "hard_ok": hard_ok,
                                "failures": failures(res), "reply_chars": len(reply)}
        results.append(row)

    # ------------------------------------------------------------- report
    print(f"\n{C['b']}case                              with   without   delta{C['0']}")
    worst = 1.0
    for r in results:
        w = r["arms"].get("with", {}).get("score")
        wo = r["arms"].get("without", {}).get("score")
        if w is not None:
            worst = min(worst, w)
        fw = f"{w:.2f}" if w is not None else "  - "
        fwo = f"{wo:.2f}" if wo is not None else "  - "
        if w is not None and wo is not None:
            d = w - wo
            col = C["g"] if d > 0.01 else (C["y"] if abs(d) <= 0.01 else C["r"])
            fd = f"{col}{d:+.2f}{C['0']}"
        else:
            fd = "    "
        mark = C["g"] + "ok  " + C["0"] if (w or 0) >= args.threshold else C["r"] + "FAIL" + C["0"]
        print(f"{mark} {r['id']:28} {fw}    {fwo}    {fd}")

    for r in results:
        for arm, a in r["arms"].items():
            if a["failures"]:
                print(f"\n{C['b']}{r['id']} [{arm}]{C['0']}")
                for f in a["failures"]:
                    colour = C["y"] if f.startswith("(soft)") else C["r"]
                    print(f"  {colour}{f}{C['0']}")

    withs = [r["arms"]["with"]["score"] for r in results if "with" in r["arms"]]
    withouts = [r["arms"]["without"]["score"] for r in results if "without" in r["arms"]]
    if withs:
        print(f"\nmean with skill:    {sum(withs)/len(withs):.3f}")
    if withouts:
        print(f"mean without skill: {sum(withouts)/len(withouts):.3f}")
        if withs:
            print(f"{C['b']}skill delta:        {sum(withs)/len(withs) - sum(withouts)/len(withouts):+.3f}{C['0']}")
    if missing:
        print(f"\n{C['y']}{missing} reply file(s) missing{C['0']}")
    if errors:
        print(f"\n{C['r']}{errors} call(s) failed or timed out — results incomplete{C['0']}")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"\nwrote {args.json}")

    if errors or missing:
        return 1
    return 1 if withs and worst < args.threshold else 0


if __name__ == "__main__":
    sys.exit(main())
