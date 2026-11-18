"""Thin wrapper over the TexasSolver console binary.

Solver player 1 is OOP (BB here), player 0 is IP (SB). Action strings are the
solver's own ("CHECK", "BET 1.650000", "RAISE 5.400000", "CALL", "FOLD"); the
number after BET/RAISE is the chips the actor adds with that action.
"""
import json
import lzma
import os
import shutil
import stat
import subprocess

from cards import canon

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.expanduser(os.environ.get("POKER_COACH_DIR", "~/workspace/poker-coach"))
SOLVER_DIR = os.path.expanduser(os.environ.get("TEXASSOLVER_DIR", "~/workspace/texassolver"))
# The chat package ships a Linux build in bin/ and the hand-ranking table
# compressed in resources/; a local build (SETUP.md) lives in SOLVER_DIR.
BUNDLED_BINARY = os.path.join(SKILL_DIR, "bin", "console_solver")
BUNDLED_TABLE = os.path.join(SKILL_DIR, "resources", "card5_dic_sorted.txt.xz")


def _bundled():
    """(binary, resources) prepared from the chat package, in the data dir.

    The skill folder may be read-only and zips drop the executable bit, so the
    binary is copied out and the table decompressed once.
    """
    binary = os.path.join(DATA_DIR, "bin", "console_solver")
    resources = os.path.join(DATA_DIR, "resources")
    table = os.path.join(resources, "compairer", "card5_dic_sorted.txt")
    if not os.path.exists(binary):
        os.makedirs(os.path.dirname(binary), exist_ok=True)
        shutil.copyfile(BUNDLED_BINARY, binary + ".tmp")
        os.chmod(binary + ".tmp", stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
        os.replace(binary + ".tmp", binary)
    if not os.path.exists(table):
        os.makedirs(os.path.dirname(table), exist_ok=True)
        with lzma.open(BUNDLED_TABLE) as src, open(table + ".tmp", "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(table + ".tmp", table)
    return binary, resources


def solver_paths():
    """(binary, resources dir): TEXASSOLVER_DIR if set, else the bundled build, else the local one."""
    if "TEXASSOLVER_DIR" not in os.environ and os.path.exists(BUNDLED_BINARY):
        return _bundled()
    return os.path.join(SOLVER_DIR, "build", "console_solver"), os.path.join(SOLVER_DIR, "resources")

SEAT_TO_PLAYER = {"SB": 0, "BB": 1}
PLAYER_TO_SEAT = {0: "SB", 1: "BB"}

STREETS = ["flop", "turn", "river"]
# The street being solved gets a menu of sizes plus raises and all-in; later
# streets in the same solve only shape its values, so they get a single bet
# size and no raises to keep the tree small (each is re-solved with the full
# menu once it is reached). Bets that commit over 67% of a player's chips become all-in.
# POKER_COACH_FLOP_BETS trims the flop menu (chat uses one size to fit its slower sandbox).
FLOP_BETS = [int(x) for x in os.environ.get("POKER_COACH_FLOP_BETS", "33,75").split(",")]
ROOT_SIZES = {
    "flop": {"bet": FLOP_BETS, "raise": [60]},
    "turn": {"bet": [50, 100], "raise": [60]},
    "river": {"bet": [50, 100], "raise": [60]},
}
LATER_SIZES = {"bet": [75], "donk": [75]}
# POKER_COACH_FAST=1 (chat) leaves river betting out of flop solves: about 2.5x
# faster, at the cost of flop values that ignore river bluffs and value bets.
FAST = os.environ.get("POKER_COACH_FAST") == "1"
RAISE_LIMIT = int(os.environ.get("POKER_COACH_RAISE_LIMIT", "2"))  # per street
# The solver rounds bet sizes to whole chips, so solve in tenths of a bb.
CHIPS_PER_BB = 10
ACCURACY = float(os.environ.get("POKER_COACH_ACCURACY", "3.0"))  # % of pot
MAX_ITERATIONS = int(os.environ.get("POKER_COACH_ITERATIONS", "100"))


def cpu_budget():
    """CPUs this process may actually use: affinity and cgroup quota, not the host's count.

    Containers often report every host CPU while capping the quota to one or two;
    starting a thread per reported CPU then makes the solver thrash.
    """
    n = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 2)
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:  # cgroup v2: "<quota> <period>" or "max <period>"
            quota, period = f.read().split()
        if quota != "max":
            n = min(n, max(1, int(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return max(1, n)


def range_string(weights, floor=()):
    """Solver range string from {combo or hand class: weight}.

    Keys in `floor` are kept at weight >= 0.02 so the real hands are always
    in the solve. Exact combos switch off the solver's suit isomorphism, which
    makes a flop solve far slower, so flop ranges should be hand classes.
    """
    weights = {(canon(k) if len(k) == 4 else k): w for k, w in weights.items()}
    for key in floor:
        key = canon(key) if len(key) == 4 else key
        weights[key] = max(weights.get(key, 0.0), 0.02)
    return ",".join(f"{k}:{min(w, 1.0):.4f}" for k, w in weights.items() if w > 0.005)


def solve(board, pot, stack, ranges, floor, out_path, threads=None):
    """Solve from the start of the current street. Returns the dumped root node.

    `ranges` is {"SB": weights, "BB": weights}; see range_string for `floor`.
    """
    lines = [
        f"set_pot {pot * CHIPS_PER_BB:.0f}",
        f"set_effective_stack {stack * CHIPS_PER_BB:.0f}",
        "set_board " + ",".join(board),
        "set_range_ip " + range_string(ranges["SB"], floor),
        "set_range_oop " + range_string(ranges["BB"], floor),
    ]
    root_street = STREETS[len(board) - 3]
    for street in STREETS[len(board) - 3:]:
        is_root = street == root_street
        if FAST and root_street == "flop" and street == "river":
            continue  # no river bets: the river is checked down
        kinds = ROOT_SIZES[street] if is_root else LATER_SIZES
        for side in ("oop", "ip"):
            for kind, sizes in kinds.items():
                if not (kind == "donk" and side == "ip"):
                    lines.append(f"set_bet_sizes {side},{street},{kind}," + ",".join(str(x) for x in sizes))
            if is_root:
                lines.append(f"set_bet_sizes {side},{street},allin")
    lines += [
        # TexasSolver compares commit + bet against threshold x set stack, but
        # commit includes half the starting pot, so scale the threshold to mean
        # "67% of each player's total chips" rather than of what is left.
        f"set_allin_threshold {0.67 * (stack + pot / 2) / stack:.4f}",
        f"set_raise_limit {RAISE_LIMIT}",
        "build_tree",
        f"set_thread_num {threads or cpu_budget()}",
        f"set_accuracy {ACCURACY}",
        f"set_max_iteration {MAX_ITERATIONS}",
        "set_print_interval 10",  # also how often it checks accuracy to stop early
        "set_use_isomorphism 1",
        "start_solve",
        "set_dump_rounds 1",
        f"dump_result {out_path}.part",
    ]
    in_path = out_path + ".in.txt"
    with open(in_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    binary, resources = solver_paths()
    # The solver writes a tmp_log.txt into its working directory.
    proc = subprocess.run(
        [binary, "-i", os.path.abspath(in_path), "-r", resources],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(out_path)),
    )
    if proc.returncode != 0 or not os.path.exists(out_path + ".part"):
        raise RuntimeError("solver failed:\n" + proc.stdout[-2000:] + proc.stderr[-2000:])
    os.replace(out_path + ".part", out_path)  # readers only ever see a finished dump
    with open(out_path) as f:
        return json.load(f)


def amount_bb(action):
    """Chips added by a BET/RAISE action, in bb."""
    return float(action.split()[1]) / CHIPS_PER_BB


def node_at(root, actions):
    node = root
    for a in actions:
        node = node["childrens"][a]
    return node


def combo_strategy(node, combo):
    """{action: prob} for one combo at an action node, or None if absent."""
    strat = node["strategy"]
    table = strat["strategy"]
    probs = table.get(combo) or table.get(combo[2:] + combo[:2])
    if probs is None:
        return None
    return dict(zip(strat["actions"], probs))


def combo_evs(node, combo):
    """{action: EV in bb} for one combo at an action node, or None if absent.

    Needs a solver built with the EV patch (SETUP.md); older dumps have no
    "evs" block. EVs are measured from the start of the solved street with
    each player's share of the starting pot counted as already lost, so only
    differences between actions at the same node are meaningful.
    """
    block = node.get("evs")
    if not block:
        return None
    table = block["evs"]
    evs = table.get(combo) or table.get(combo[2:] + combo[:2])
    if evs is None:
        return None
    return {a: v / CHIPS_PER_BB for a, v in zip(block["actions"], evs)}


def ev_loss(evs, chosen):
    """bb given up by `chosen` vs the best action, from combo_evs output."""
    if not evs or chosen not in evs:
        return None
    return round(max(0.0, max(evs.values()) - evs[chosen]), 2)


def narrow(root, actions, ranges):
    """Multiply each seat's weights by its strategy along `actions`."""
    out = {seat: dict(w) for seat, w in ranges.items()}
    node = root
    for a in actions:
        seat = PLAYER_TO_SEAT[node["player"]]
        acts = node["strategy"]["actions"]
        idx = acts.index(a)
        table = node["strategy"]["strategy"]
        new = {}
        for combo, w in out[seat].items():
            probs = table.get(combo) or table.get(combo[2:] + combo[:2])
            if probs is not None:
                new[combo] = w * probs[idx]
        out[seat] = new
        node = node.get("childrens", {}).get(a)
        if node is None or node.get("node_type") != "action_node":
            break  # street (or hand) is over
    return out


def range_strategy(node, weights):
    """Weighted-average strategy over a seat's whole range at a node."""
    acts = node["strategy"]["actions"]
    table = node["strategy"]["strategy"]
    totals = [0.0] * len(acts)
    total_w = 0.0
    for combo, w in weights.items():
        probs = table.get(combo) or table.get(combo[2:] + combo[:2])
        if probs is None or w <= 0:
            continue
        total_w += w
        for i, p in enumerate(probs):
            totals[i] += w * p
    if total_w == 0:
        return {}
    return {a: t / total_w for a, t in zip(acts, totals)}
