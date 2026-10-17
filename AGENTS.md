# Agent instructions

## Branches and the shared checkout

Many sessions share the main checkout (`~/projects/agent-media`) and commit
straight to `main`. Never `git checkout` / `git switch` a branch in the shared
checkout; branches live in worktrees
(`git worktree add ../agent-media-<topic> -b <branch>`, as `agent-media-audio`,
`-frames` and `-org` do). `.githooks/pre-commit` refuses a commit in the
shared checkout when HEAD is not `main` (activate once per clone with
`git config core.hooksPath .githooks`).
