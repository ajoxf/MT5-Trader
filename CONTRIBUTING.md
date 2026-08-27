# Contributing

## Branching rules

**Never commit directly to `main`.** `main` must always hold working,
reviewed code.

All work happens on a branch, and reaches `main` through a pull request.

### Branch naming

| Prefix | Use for | Example |
|---|---|---|
| `feature/` | New functionality | `feature/rsi-strategy` |
| `fix/` | Bug fixes | `fix/order-retry-loop` |
| `refactor/` | Restructuring, no behaviour change | `refactor/split-broker-client` |
| `docs/` | Documentation only | `docs/setup-guide` |

## Workflow

```bash
# 1. Start from an up-to-date main
git checkout main
git pull origin main

# 2. Create your branch
git checkout -b feature/my-thing

# 3. Work, then commit
git add .
git commit -m "Add RSI entry signal"

# 4. Push and set tracking (first push only)
git push -u origin feature/my-thing
```

Then open a pull request on GitHub, get it reviewed, and merge. Delete the
branch after merging.

## Knowing which branch you are on

Git commits and pushes to whichever branch is currently checked out. Check
before you commit:

```bash
git branch --show-current   # prints the branch name
git status                  # "On branch ..." on the first line
```

After `git push -u origin <branch>` the branch is linked to its remote, so
plain `git push` and `git pull` go to that same branch from then on.

If `git branch --show-current` says `main`, stop and create a branch first.

## Keeping your branch current

If `main` moves ahead while you work:

```bash
git checkout main && git pull origin main
git checkout feature/my-thing
git merge main          # resolve any conflicts, then commit
```

## Commit messages

- Present tense, imperative: "Add trailing stop", not "Added trailing stop"
- One logical change per commit
- Explain *why* in the body when the reason is not obvious from the diff

## Pull requests

- Keep them small and focused — easier to review, safer to merge
- Describe what changed and why
- Note whether it was tested on a demo account
- At least one review before merge

## Security

Never commit credentials, account numbers, broker passwords, or API keys.
Configuration goes in `.env`, which is git-ignored. Use `.env.example` to
document new variables (names only, no values).
