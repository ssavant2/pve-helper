# Public release checklist

Before pushing this repository to a public remote, check both the current tree
and the Git history that will be pushed.

## Current tree

The public files should use placeholders such as:

```text
example.com
nfs-server.example.com
pve-node-1
nfs-fs
nfs-vm
```

Local values belong in ignored files, for example:

```text
.env
docker-compose.yml
docs/*.local.md
docs/local/
.local/
```

Run:

```bash
git grep -n -E 'real-domain|real-host|real-ip|real-pool-name' -- .
git status --ignored=matching docs/environment.local.md
```

The local environment file should show up only as ignored.

## Commit history

Removing a value from the latest commit is not enough if an older commit still
contains it. If the branch has not been pushed yet, prefer rewriting the local
unpublished commits before the first public push.

For this repository, the safe shape before first public push is usually:

```bash
git fetch origin
git reset --soft origin/main
git commit -m "Add storage browser and public deployment docs"
```

Then inspect the rewritten history:

```bash
git log --oneline origin/main..HEAD
git grep -n -E 'real-domain|real-host|real-ip|real-pool-name' HEAD -- .
```

If sensitive values have already been pushed to GitHub, use a history-rewrite
tool such as `git filter-repo` or BFG, force-push every affected branch/tag, and
rotate any exposed credentials. GitHub can retain cached objects for a while
even after a force-push, so treat pushed secrets as compromised.
