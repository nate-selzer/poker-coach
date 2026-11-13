# Poker coach

A no-limit hold'em trainer skill for Claude. You play hands against a solver-driven villain, heads-up or at a 4- or 6-max table. Every decision is graded against [TexasSolver](https://github.com/bupticybee/TexasSolver) postflop and against preflop charts. On a mistake, Claude asks why the solver prefers another play, then evaluates your answer with the numbers: equity, pot odds, range breakdowns and the EV each option gives up.

It also has drills for spot types you miss (strong hands, medium pairs, air, draws, preflop edges, or an adaptive mix weighted by your own mistakes), a leak report, and a mixing-calibration report every 200 hands.

## Claude chat (claude.ai)

1. Download [`poker-coach-chat.zip`](https://github.com/nate-selzer/poker-coach/releases/download/chat-skill/poker-coach-chat.zip) from the `chat-skill` release.
2. In claude.ai, open Settings → Capabilities, turn on code execution, and upload the zip under Skills.
3. In a chat, say "let's play poker-coach".

The zip carries a Linux build of TexasSolver, so solves run in the chat's sandbox. The sandbox resets between chats: Claude hands you `poker-coach-save.tar.gz` as you play; attach it in your next chat to keep your stats and settings.

## Claude Code

```bash
git clone https://github.com/nate-selzer/poker-coach.git ~/workspace/poker-coach-skill
ln -s ~/workspace/poker-coach-skill/skill ~/.claude/skills/poker-coach
~/workspace/poker-coach-skill/solver/build.sh ~/workspace   # see skill/SETUP.md
```

Then say "poker-coach" in a session. Game data lives in `~/workspace/poker-coach/`.

## Layout

- `skill/`: the skill. `SKILL.md` is the Claude Code version; `chat.md` plus the shared part of `SKILL.md` becomes the chat version's `SKILL.md`.
- `solver/`: the TexasSolver patch and build script.
- `tools/package_chat.sh`: builds `poker-coach-chat.zip`. `tools/smoke_test.py` plays a hand with it.
- `.github/workflows/chat-skill.yml`: on every push to `main`, builds the Linux solver, packages and smoke-tests the zip, and replaces it on the `chat-skill` release.

## License

AGPL-3.0, matching TexasSolver, whose modified build ships in the chat zip. See `solver/NOTICE.md`.
