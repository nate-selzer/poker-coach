# TexasSolver notice

`bin/console_solver` in the chat package is built from TexasSolver
(https://github.com/bupticybee/TexasSolver, branch `console`, commit
02c8021ee1307c18aa1e76b772c6ae1451601bc6), Copyright (C) its authors, licensed
under the GNU Affero General Public License v3.0 (see `LICENSE`).

It is modified by `solver/texassolver.patch` in https://github.com/nate-selzer/poker-coach:
a `set_raise_limit` command, per-action EVs in `dump_result`, and no Python
bindings in the build. `solver/build.sh` there rebuilds it from source.
`resources/card5_dic_sorted.txt.xz` is TexasSolver's hand-ranking table,
compressed.
