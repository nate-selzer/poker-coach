# Building TexasSolver

The skill drives the console build of https://github.com/bupticybee/TexasSolver (branch `console`) with this repo's patch (`solver/texassolver.patch`: a `set_raise_limit` command, per-action EVs in `dump_result` for EV-loss grading, and no Python bindings).

On macOS, `brew install cmake libomp` first. Then, from the repo root:

```bash
solver/build.sh ~/workspace
```

That clones TexasSolver into `~/workspace/TexasSolver` and builds `build/console_solver`. The skill looks for it in `~/workspace/texassolver` by default; point `TEXASSOLVER_DIR` at `~/workspace/TexasSolver` or rename the folder.

Sanity check: `./build/console_solver -i resources/text/commandline_sample_input.txt -r resources` should finish in well under a minute, and every action node in `output_result.json` should have an `evs` block. Without the EV patch the skill still works; solver grades just have no `costs ~Xbb`.

Tuning: `POKER_COACH_ACCURACY` (target exploitability, % of pot, default 3.0) and `POKER_COACH_ITERATIONS` (default 100) trade solve time for precision.
