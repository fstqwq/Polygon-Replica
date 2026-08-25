# Polygon Replica

Polygon Replica is a self-hosted problem-setting system compatible with Codeforces Polygon. It uses [DOMjudge](https://www.domjudge.org/) judgehosts for execution, with the browser as its primary interface.

- **Better compatibility.** The TeX statement pipeline uses our maintained hard fork of [Polygon-WF-Styles](third_party/Polygon-WF-Styles/README.md) to produce World Finals-style statements, while retaining (almost) full compatibility with Codeforces Polygon's FreeMarker sources. Existing Polygon sources and working habits therefore carry over directly. Interactive and multi-pass problems are fully supported as first-class problem types. All problems can be packaged for direct delivery to multiple contest systems.
- **Better scalability.** DOMjudge judgehosts scale execution independently across remote machines using either `domjudge/judgehost:latest` or the [modified version for long-running stability](docs/operations/deployment.md#judgehost-image-choice). This also enables last-minute calibration on the actual contest hardware by wiring the on-site judgehosts into Polygon Replica. Content-addressed caching reduces repeated computation and duplicate storage as workloads grow.
- **Better AI integration.** Each agent has an explicit, user-controlled scope that can never exceed the connected user's current permissions. [Polygon-Skills](https://github.com/fstqwq/Polygon-Skills) gives them the project context and conventions needed to apply good taste.

## Documentation

- [User guide (in Chinese)](docs/user-guide.md)
- [Deployment and operations](docs/operations/deployment.md)
- [Problem source format](docs/protocol/problem-source.md)
- [Package import and export](docs/protocol/package.md)
- [Development documentation index](docs/README.md)

## License

Polygon Replica is licensed under the [GNU Affero General Public License, version 3 or any later version](LICENSE). Third-party components remain under the licenses listed in [`third_party/README.md`](third_party/README.md).

## Known limitations

Polygon Replica is designed for a **small team** completing a limited run of problem-setting tasks.
Running at public scale would require a highly reliable judge backend and substantially more compute, memory, and storage. Ideally, that judge backend would be shared with a large online judge (like Codeforces does), so the same capacity can process ordinary submissions between problem-verification workloads instead of sitting idle. Building and operating such infrastructure is outside Polygon Replica's scope.

- **Coordination is limited to a single Python process under the GIL.** Additional judgehosts increase execution parallelism, but UI requests and judgehost protocol traffic remain in one coordinator process. The coordination layer therefore does not scale horizontally.
- **Larger deployments are undertested.** Current stress tests have exercised up to 16 judgehosts connected over a private network, including full verification of a problem with a five-second time limit across roughly 8,000 solution-test combinations and 5 GB of test data.
- **Partial scoring is unsupported.** OI-style scoring and other settings that award partial credit are outside the project's scope.
