# AGENTS.md

Polygon-Replica is a web problem-authoring and judging system. Start at
[docs/README.md](docs/README.md), then read only the owning protocol and package
map pages for the area being changed.

Repository rules:

- Before editing documentation, read [docs/AGENTS.md](docs/AGENTS.md).
- Write tests for observable outcomes. Assert affirmatively: do not assert
  old behaviors do not exist. Retire stale tests eagerly. Before adding,
  changing, or retaining a test, read [docs/testing.md](docs/testing.md).
- Python code follows [docs/coding-style.md](docs/coding-style.md).
- Refactoring follows [docs/refactoring.md](docs/refactoring.md).
- Application layers and module responsibilities follow
  [docs/module-taxonomy.md](docs/module-taxonomy.md).
- Preserve unrelated worktree changes. Do not commit or push unless requested.

## Environment discipline

- Run tests only on Linux.
- If a command fails because of the environment or missing dependencies, first
  assume that the selected interpreter or virtualenv is incorrect.
- Before running tests or switching environments, ask which Linux environment
  and virtualenv to use.
- Do not silently switch to WSL, another Python interpreter, another
  virtualenv, a remote host, or another machine.
