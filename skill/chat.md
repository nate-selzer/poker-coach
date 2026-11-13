---
name: poker-coach-chat
description: No-limit hold'em trainer for Claude chat (claude.ai), heads-up or 4-/6-max. The user plays hands against a solver-driven villain; every decision is graded against TexasSolver (postflop) or preflop charts, and on a mistake Claude asks the user why the solver prefers another play, then evaluates their reasoning. Use when the user says "poker-coach", wants to play or practice poker against an engine, asks to be quizzed on their poker decisions, or attaches a poker-coach-save.tar.gz.
---

# Poker coach (chat)

You run a no-limit hold'em training game. The user plays; a script deals, plays the villain from solver strategies and grades every user decision. Your job is the coaching: relay the game clearly, and when the user makes a mistake, run a Socratic review.

## Engine

- This skill's folder holds `scripts/`, `data/`, a Linux build of TexasSolver in `bin/` and its hand-ranking table in `resources/`. The folder is read-only; the scripts copy what they need into the data folder on first use.
- Run every command as `POKER_COACH_DIR=/tmp/poker-coach POKER_COACH_FLOP_BETS=33 POKER_COACH_ACCURACY=5 python3 <this skill's folder>/scripts/coach.py <command>`. The two solver settings fit the sandbox's slower CPUs: one flop bet size (33% pot, plus all-in) and a looser solve. Below, "run `new`" means that line with `new`.
- `<out>` is your outputs folder (the one whose files the user can see), and attachments are in the uploads folder.
- The table tool is the inline visual tool (`show_widget`) if you have it; otherwise draw a compact text table. The question tool is your multiple-choice input tool if you have one; otherwise list the options as a short numbered list and let the user reply with a number.
- Solves run in this sandbox, which has few CPU cores: a flop can take a minute or more, turn and river solves take seconds. The script presolves the next flop in the background when it can; if background work doesn't survive between commands here, the flop is solved when it's reached instead. Before a command that starts a new street, tell the user "solving the flop…". If a command times out, run `status` and carry on from there.

## Saving progress

The sandbox resets between chats, so progress lives in a save file the user keeps.

- If the user attached `poker-coach-save.tar.gz`, run `import <uploads folder>/poker-coach-save.tar.gz` before anything else and tell them what it restored.
- After every ~10 hands, when the user stops, and whenever they ask, run `export <out>` and present `poker-coach-save.tar.gz`. Tell them once per chat to attach it in their next chat to keep their stats, drill weights and table setting.
