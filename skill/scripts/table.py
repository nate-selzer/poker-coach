"""The TABLE line: compact JSON that skill/web/table.js draws as the table widget.

The coach passes it to the visual tool as
  <div data-s='JSON'></div><script src=".../table.js"></script>
so each widget is a short line instead of a full page of HTML.
"""
import json
import re

ACTION = re.compile(r"^(\w+)(?: \((?:you|villain)\))? (.+)$")


def last_actions(history):
    """{seat: last action this street} from history lines; posts count preflop."""
    acts, folded = {}, set()
    for line in history:
        if line.startswith("---"):
            acts = {}
            continue
        for part in line.split(", "):
            m = ACTION.match(part.strip())
            if m:
                acts[m.group(1)] = m.group(2)
                if m.group(2) == "folds":
                    folded.add(m.group(1))
    return acts, folded


def line(title, pot, seats, hero, history, board, hand, stack, desc, labels=None, over=False):
    acts, folded = last_actions(history)
    out = []
    for s in seats:
        a = acts.get(s, "")
        if s in folded:
            a = "folded"
        elif s == hero and not over:
            a = (a + " · to act") if a and not a.startswith("posts") else "to act"
        name = (labels or {}).get(s, s) + (" (you)" if s == hero else "")
        out.append({"n": name, "a": a, "h": int(s == hero), "o": int(s in folded),
                    "r": int(bool(re.search(r"bets|raises|all-in|3-bets", a)))})
    data = {"t": title, "pot": pot, "seats": out, "b": board, "hand": hand, "st": stack, "d": desc}
    return "TABLE: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("'", "’")
