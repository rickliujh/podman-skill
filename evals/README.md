# Evals for the podman skill

Deliberately tool-agnostic. Cases are plain YAML, graders are regexes, and the
runner is stdlib-only Python 3 — no vendor CLI, no API key, no judge model, no
third-party packages. `pyyaml` is used when present and falls back to a bundled
parser when it isn't.

The point is not "is the answer good" — a capable model knows a lot of Podman
already. The point is **does the skill change the answer**, so every run scores
two arms and reports the delta:

- `with` — the skill's text injected ahead of the question
- `without` — the bare question

A case that scores the same in both arms is a case the skill isn't earning.

## Run it

Any command that writes the reply to stdout works. CLIs disagree about where the
prompt goes, so the runner supports both conventions:

- **stdin** (the default) — the prompt is piped in.
- **argument** — if `EVAL_CMD` contains the literal `{prompt}`, the shell-quoted
  prompt is substituted there instead and nothing is piped.

```bash
EVAL_CMD='claude -p'              python3 run.py run   # stdin
EVAL_CMD='llm -m gpt-4o'          python3 run.py run   # stdin
EVAL_CMD='ollama run llama3'      python3 run.py run   # stdin
EVAL_CMD='gemini -p {prompt}'     python3 run.py run   # argument
EVAL_CMD='python3 my_wrapper.py'  python3 run.py run   # stdin
```

`gemini -p` on its own fails with `Not enough argument following p`, because its
`-p` requires a value rather than reading stdin — use the `{prompt}` form.

The `with` arm's prompt is ~25 KB (it carries the whole skill). That is well
inside `ARG_MAX` on Linux and macOS, but prefer stdin where a CLI supports it.

```bash
python3 run.py run --timeout 300         # per-call cap (default 120s)
python3 run.py run --case proxy          # one case
python3 run.py run --tag policy          # by tag
python3 run.py run --arms with           # skip the baseline arm
python3 run.py run --threshold 0.7       # hard criteria only
python3 run.py run --json out.json       # machine-readable
python3 run.py list
```

Progress prints per call as it goes, so a slow model looks slow rather than hung.
Each call is capped by `--timeout` (120s default) and the child's stdin is always
closed — an interactive CLI cannot sit waiting on your terminal.

Exit status is 1 if any `with`-arm case scores below `--threshold`, **or** if any
call failed, timed out, or had no saved reply — an incomplete run never passes.

## Run it with no tooling at all

```bash
python3 run.py emit prompts/         # writes <case>.<arm>.prompt.txt
# paste each into any assistant; save replies as <case>.<arm>.reply.txt
python3 run.py grade prompts/
```

Grading is offline and deterministic, so replies gathered by hand, from a
colleague, or from a model with no CLI all score the same way.

## Scoring

| | |
|---|---|
| `must_mention` | all required — any miss scores the case 0 |
| `must_not_mention` | none may appear — any hit scores the case 0 |
| `should_mention` | partial credit across the remaining 0.3 |

Hard criteria satisfied = 0.7. Soft criteria fill the rest. A default
`--threshold` of 1.0 therefore demands the soft criteria too; pass
`--threshold 0.7` to require only the hard ones.

`must_not_mention` carries most of the weight here, because this skill is mostly
defined by what it rules out: rootful setups, `docker compose`, `chmod 777`,
`--tls-verify=false`.

## Cases

| id | asserts |
|---|---|
| `mac-empty-bind-mount` | diagnoses the `$HOME`/VM boundary; never reaches for `chmod 777` |
| `compose-single-path` | uses `podman compose`; never `docker compose`, `podman-compose`, or `DOCKER_HOST` |
| `rootless-low-port` | high port or in-VM sysctl; never `--rootful` |
| `proxy-x509` | `--import-native-ca`; never `--tls-verify=false` |
| `apple-silicon-exec-format` | architecture; not the entrypoint's permissions or line endings |
| `image-short-name` | fully-qualified images and `:z` in generated compose files |
| `negative-unrelated` | a Kubernetes question stays clean of Podman advice |

The last one guards the opposite failure: a description broad enough to fire on
everything. It should score ~1.0 in *both* arms — a delta near zero there is the
correct result, not a weak case.

## Adding a case

Drop a YAML file in `cases/`:

```yaml
id: my-case
tags: [area]
prompt: |
  What the user asks.
expect:
  must_mention:     ["(?i)required phrase"]
  must_not_mention: ["(?i)forbidden phrase"]
  should_mention:   ["(?i)nice to have"]
```

Regexes are Python `re`, applied to the raw reply. Write them loose enough to
survive paraphrase — match `import-native-ca`, not a whole sentence — and put
the precision into `must_not_mention`, where false positives are cheap to spot.

Keep the directory at `evals/` under the skill: it is also where
`claude plugin eval` looks, so this suite stays usable from there without moving
anything if that tool is ever enabled.
