# Perf Lab for Hermes

**Proof for speed-ups.** When you ask Hermes to make code faster, Perf Lab measures the
code before and after, checks the output did not change, shows where the time goes as a
flame graph, and makes the measuring unskippable with hooks. The numbers under the reply
come from the plugin, not from the model.

Community plugin, not an official Nous Research product. MIT licence.

![Faster, same output](docs/faster.png)

| Rejected: faster, but the output changed | Where the time goes |
|---|---|
| ![Rejected](docs/rejected.png) | ![Flame graph](docs/flame-graph.png) |

*Screenshots are frames from a live Hermes Desktop session (real agent, real measurements) on the bundled
demo script. In the rejected frame the footer appears twice: that was a bug, fixed in this version.*

## What it does

| Moment | Without Perf Lab | With Perf Lab |
|---|---|---|
| Agent edits the code straight away | Nothing to compare against later | Edit is **blocked** until a baseline is measured |
| Agent says "done, 10× faster" | You take its word | The turn is **held** until it re-measures; you see the plugin's number (say 6.8×) and a correction |
| The faster code gives different results | You may never notice | **Rejected**: "output changed", with the first differing lines |
| The difference is just noise | Reported as a win | "No real change": 90 % of before/after run pairs must agree |

In the Desktop pane (⌘K → **Perf Lab: Open / close**, or the status-bar chip):

- a verdict card: before → after, speed-up, output identical ✓ / changed ✗
- every timed run as a dot, so you can see for yourself whether the speed-up is more than noise
- a flame graph (click a bar to zoom) with the hottest function in red, and a top-5 list
- history of median times per script

## How it works

**Tools the agent calls**

- `perf_bench(command, cwd, runs=5, warmup=1, ignore_patterns=[], compare_files=[], set_baseline=false)`
  runs the command N times without a shell, records the median and a fingerprint of the
  output (stdout + exit code + any `compare_files`). The first run of a command is the
  baseline; later runs are compared with it.
- `perf_profile(command, cwd)` samples a Python command's call stack. Uses `py-spy` if it is
  installed, otherwise a built-in stdlib sampler (no native code, nothing to install).
- `perf_skip(reason)` turns the gate off for one chat when code truly cannot be timed. The
  reason is printed under the reply, so skipping is never silent.

**Hooks (only in chats about speed, only while the Gatekeeper switch is on)**

| Hook | What it does |
|---|---|
| `pre_llm_call` | Spots a speed request ("slow", "faster", "optimise", ...) and gives the agent the rules |
| `pre_tool_call` | Blocks `write_file` / `patch` on code until the project has a baseline |
| `post_tool_call` | Records edited files so earlier numbers become "out of date" |
| `pre_verify` | Keeps the turn going if code changed after the last measurement (max 2 nudges) |
| `transform_llm_output` | Appends the measured result and corrects speed claims that disagree by more than 20 % |

Every hook catches its own errors and fails open: a Perf Lab bug never blocks your work.

**Verdict rules** (deterministic, `perf_core.compare`)

- `faster`: output identical **and** at least 90 % of before/after run pairs favour the new code **and** it is at least 5 % quicker. It compares the quickest half of each set of runs, because background load only ever adds time (the reasoning in Python's `timeit` docs), so a slow outlier cannot hide a real speed-up and one lucky run cannot invent one
- `changed_output`: output differs from the baseline, whatever the speed
- `no_change`: the before and after times overlap too much to call
- `inconclusive`: the median moved, but the machine was too noisy to prove it (more runs, quieter machine)
- `slower`, `failed`, `unstable_output` (output differs between runs of the same code; add `ignore_patterns` for timing or date lines)

## Install

```bash
hermes plugins install Murali-lns/perf-lab --enable
```

Then in Hermes Desktop: Settings → Plugins → turn on the **Perf Lab** desktop half (package
halves start off). Restart the Hermes gateway once so the tools and the pane backend load.

For development: `ln -sfn ~/hermes/perf-lab ~/.hermes/plugins/perf-lab && hermes plugins enable perf-lab`.

## Try it

`examples/backtest/backtest.py` is a deliberately slow moving-average backtest (about 1.3 s).
In a chat: *"examples/backtest/backtest.py is slow, make it faster without changing results."*

## Safety and privacy

- **Runs only interpreters.** `perf_bench` / `perf_profile` accept commands that start with an
  interpreter (`python`, `node`, `ruby`, `uv run`, ...), never a shell, and refuse inline code
  (`-c`, `-e`). This keeps them from becoming a way around Hermes' command approvals. A script
  can still do anything the user can, exactly as when the agent runs it in the terminal.
- **Local only.** No network calls, no telemetry, no credentials read.
- **What is stored** in `$HERMES_HOME/plugin-data/perf-lab/`: commands, run times, output
  fingerprints, the first 200 KB of each run's normalised stdout (to show what changed),
  profiles (function names and file paths), and per-chat gate state keyed by a hash of the
  chat id. **Forget** in the pane deletes a script's data.
- **Edits replies.** `transform_llm_output` appends a clearly labelled Perf Lab section to the
  agent's reply. It never rewrites the agent's own text.

## Development

```bash
~/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
hermes plugins validate . && hermes plugins doctor .
```

The tests are written against the behaviours above (faster + same output, changed output,
noise, wrong claims, blocked edits, held "done", command allowlist, fail-open hooks).
