# Contributing to Kessler

Thanks for helping. Bug reports, fixes, new templates and docs improvements are all welcome.

## Before you start

- **Small fix** (typo, broken link, obvious bug): open a pull request directly.
- **Larger change** (new feature, new resilience model, API change): open an issue first so we can agree on the approach before you write code.
- **Security problem:** do not open a public issue. Follow [SECURITY.md](SECURITY.md).

Look for issues labelled `good first issue` or `help wanted` if you want somewhere to start.

## Set up

You need Python 3.12, Node 22, CMake and Ninja. Postgres 18, Redis 8 and nginx are only needed to run the full stack.

```bash
python -m venv .venv && .venv/bin/pip install -r backend/requirements.in -r backend/requirements-dev.in -c backend/requirements.lock
cmake -S engine -B engine/build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build engine/build
(cd web && npm ci)
```

On Windows, use `.venv/Scripts/` in place of `.venv/bin/`. To run everything, see [Run it](README.md#run-it) in the README.

## Make a change

1. Fork the repository and create a branch from `main`, for example `fix/replica-timeout` or `feat/ctmc-estimate`.
2. Keep each pull request to one topic. Two small pull requests review faster than one large one.
3. Add or update tests for the behaviour you change.
4. Run the checks below before you push.

## Checks

CI runs these on every pull request, and all must pass before a merge.

```bash
./engine/build/kessler-tests                 # C++ engine
ruff check backend                           # Python lint
(cd backend && pytest -q)                    # Python tests (integration tests need Postgres and Redis)
(cd web && npx tsc -b && npx vitest run)     # TypeScript types and web tests
```

If you change the engine's behaviour, also run `python scripts/validate_engine.py --template marketplace-at-scale`.

## Style

- Match the style of the code around your change. Python follows the ruff config in `backend/pyproject.toml`; TypeScript follows `web/tsconfig.json`.
- Keep code comments short, and only where the code cannot speak for itself.
- Touch only what your change needs. Put unrelated cleanups in a separate pull request.
- Write commit messages in the imperative with an optional area prefix, for example `engine: cap retry budget refill` or `docs: fix install command`.

## Pull requests

Fill in the pull request template: what changed, why, and how you tested it. Link the issue it closes (`Closes #12`). A maintainer will review, may ask for changes, and merges once CI passes.

## Code of conduct

By taking part, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
