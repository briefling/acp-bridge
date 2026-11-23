The PR of this post no longer merges cleanly.

1. `board_move(ref="<ref>", state="doing")`, rebase the branch on the default branch, resolve, `git push --force-with-lease`.
2. Comment (`board_comment`) "rebase only" (or what changed), then `board_move(ref="<ref>", state="approved")` if the text did not change,
   `board_move(ref="<ref>", state="review")` if it did.
