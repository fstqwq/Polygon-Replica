#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

TESTLIB_REPO="$TMP_DIR/testlib"
KATTIS_REPO="$TMP_DIR/problem-package-format"

git clone --depth=1 https://github.com/MikeMirzayanov/testlib "$TESTLIB_REPO"
git clone --depth=1 https://github.com/Kattis/problem-package-format "$KATTIS_REPO"

mkdir -p "$ROOT_DIR/third_party/upstream/testlib"
cp "$TESTLIB_REPO/testlib.h" "$ROOT_DIR/third_party/upstream/testlib/testlib.h"
cp "$TESTLIB_REPO/README.md" "$ROOT_DIR/third_party/upstream/testlib/README.md"
cp "$TESTLIB_REPO/LICENSE" "$ROOT_DIR/third_party/upstream/testlib/LICENSE"

mkdir -p "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/spec"
mkdir -p "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/support/schemas"
mkdir -p "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/examples/problems"

cp "$KATTIS_REPO/spec/2025-09.md" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/spec/2025-09.md"
cp "$KATTIS_REPO/spec/legacy-icpc.md" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/spec/legacy-icpc.md"
cp "$KATTIS_REPO/spec/readme.md" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/spec/readme.md"
cp "$KATTIS_REPO/support/schemas/problem.cue" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/support/schemas/problem.cue"
cp "$KATTIS_REPO/support/schemas/test_group.cue" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/support/schemas/test_group.cue"

rm -rf "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/examples/problems/passfail"
rm -rf "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/examples/problems/interactive"
rm -rf "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/examples/problems/multipass"

cp -r "$KATTIS_REPO/examples/problems/passfail" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/examples/problems/"
cp -r "$KATTIS_REPO/examples/problems/interactive" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/examples/problems/"
cp -r "$KATTIS_REPO/examples/problems/multipass" "$ROOT_DIR/third_party/upstream/kattis/problem-package-format/examples/problems/"

echo "Upstream assets synced into third_party/upstream"
