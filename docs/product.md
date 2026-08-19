# Product scope and rationale

Polygon Replica covers the stage of problem setting that begins once an author has a working idea, an initial solution, and enough tests to start validation. Its responsibility is to carry that work to a contest-ready problem.

## A complete problem-setting system

A problem-setting product has three layers. Execution establishes whether the problem behaves as intended. Collaboration gives the team an authoritative version and a controlled way to change it. Ecosystem contracts make the result usable by the systems around it.

The product owns the connection between these layers. The source accepted by the team, the evidence produced by Verification, and the Package delivered at the end all refer to the same problem state.

### Execution tools

The execution layer turns authored source into evidence. Verification checks the intended behavior on remote DOMjudge Judgehosts, while statement rendering presents the same structured samples in the UI, TeX, and HTML. Interactive and multi-pass problems keep their complete pass structure throughout this process.

### Collaboration

The collaboration layer makes that evidence authoritative for a team. Git records the official problem history, while isolated workspaces hold changes until they are reviewed and published. People and agents use the same permission model. [Polygon-Skills](https://github.com/fstqwq/Polygon-Skills) gives agents the project context and conventions needed to work with better taste inside that authority.

### Ecosystem contracts

The ecosystem layer preserves compatibility across the product boundary. Existing Polygon sources and working habits carry over directly. A successful Verification produces a Native Package tied to the published source, and adapters turn that Package into deliverables ready for multiple contest systems.

The exact boundaries are defined by the [problem source](protocol/problem-source.md), [execution](protocol/execution.md), and [package](protocol/package.md) contracts.

Polygon Replica owns this workflow from authored source to deliverable Package. Live contest operation remains with the target contest system. The hosted Polygon private API is outside the product contract. Self-hosting places deployment and execution infrastructure under the operator's control.
