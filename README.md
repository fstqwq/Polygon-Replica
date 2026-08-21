# Polygon Replica

Polygon Replica is a self-hosted problem-setting system compatible with Codeforces Polygon. It uses [DOMjudge](https://www.domjudge.org/) judgehosts for execution, with the browser as its primary interface.

- **Better compatibility.** Existing Polygon sources and working habits carry over directly. Interactive and multi-pass problems are native, first-class problem types. Their complete pass structure is visible in the UI and rendered faithfully in TeX and HTML samples. The same workflow produces packages that can be delivered directly to multiple contest systems.
- **Better scalability.** DOMjudge judgehosts scale execution independently across remote machines using either `domjudge/judgehost:latest` or the [modified version for long-running stability](docs/operations/deployment.md#judgehost-image-choice). Content-addressed caching reduces repeated computation and duplicate storage as workloads grow.
- **Better AI integration.** Each Agent has an explicit, user-controlled scope that can never exceed the connected user's current permissions. [Polygon-Skills](https://github.com/fstqwq/Polygon-Skills) gives them the project context and conventions needed to apply good taste.

## Documentation

- [Deployment and operations](docs/operations/deployment.md)
- [Problem source format](docs/protocol/problem-source.md)
- [Package import and export](docs/protocol/package.md)
- [Development documentation index](docs/README.md)

## Known limitations

- **Coordination is limited to a single Python process under the GIL.** This keeps memory use low and synchronization simple. However, UI requests and judgehost traffic share the same concurrency ceiling.
- **Larger deployments are undertested.** Current stress tests cover small-team collaborative problem setting and execution on up to 16 judgehosts connected over a private network, including full verification of a five-second problem spanning roughly 8,000 solution-test combinations and 5 GB of test data.
