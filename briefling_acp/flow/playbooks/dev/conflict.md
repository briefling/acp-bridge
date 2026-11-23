The PR of this item no longer merges cleanly.

1. `board_move(ref="<ref>", state="doing")`, rebase on the default branch, resolve, run the tests, `git push --force-with-lease`.
2. Comment "rebase only" or "rebase + changes" (list them), then `board_move(ref="<ref>", state="review")`.
