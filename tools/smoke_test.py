#!/usr/bin/env python3
"""Play one drilled heads-up hand with a packaged chat skill, the way chat runs it.

    tools/smoke_test.py <poker-coach-chat.zip>

Unzips into a temp dir, plays a `strong` drill hand to the end (always taking
the first option that isn't fold), and checks that the flop was solved and that
export/import round-trips. Prints each command's time.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile


def main(zip_path):
    root = tempfile.mkdtemp()
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(root)
    skill = os.path.join(root, "poker-coach-chat")
    # Read-only like claude.ai's skill folder; zip extraction also drops the exec bit.
    for dirpath, _, files in os.walk(skill):
        for f in files:
            os.chmod(os.path.join(dirpath, f), 0o444)
    env = dict(os.environ, POKER_COACH_DIR=os.path.join(root, "data"))
    env.pop("TEXASSOLVER_DIR", None)

    def run(*args):
        start = time.time()
        out = subprocess.run([sys.executable, os.path.join(skill, "scripts", "coach.py"), *args],
                             env=env, capture_output=True, text=True, timeout=900)
        print(f"$ coach.py {' '.join(args)}  ({time.time() - start:.1f}s)")
        print(out.stdout + out.stderr)
        if out.returncode != 0:
            sys.exit(f"command failed: {args}")
        return out.stdout

    run("drill", "strong")
    out = run("new")
    graded_postflop = False
    for _ in range(12):
        if "HAND OVER" in out:
            break
        if "PAUSED" in out:
            out = run("next")
            continue
        options = re.findall(r"\[([\w.]+)\]", out.split("YOUR OPTIONS:")[-1])
        choice = next(o for o in options if o != "fold")
        on_flop = "--- FLOP" in out
        out = run("act", choice)
        graded_postflop |= on_flop and "GRADE:" in out
    assert "HAND OVER" in out, "hand did not finish"
    assert graded_postflop, "no postflop decision was graded"

    run("export", os.path.join(root, "out"))
    save = os.path.join(root, "out", "poker-coach-save.tar.gz")
    env["POKER_COACH_DIR"] = os.path.join(root, "fresh")
    out = run("import", save)
    assert "1 heads-up hands" in out and "drill strong" in out, "import did not restore progress"
    print("smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1])
