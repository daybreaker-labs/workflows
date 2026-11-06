#!/usr/bin/env bash
# Fixture setup for the shared Testbox workflow: one tool on GITHUB_PATH and
# variables in both GITHUB_ENV forms, with spaces, quotes and a newline.
set -euo pipefail
tools="$RUNNER_TEMP/testbox-fixture/bin"
mkdir -p "$tools"
printf '#!/bin/sh\necho testbox-fixture-tool\n' > "$tools/testbox-fixture-tool"
chmod +x "$tools/testbox-fixture-tool"
echo "$tools" >> "$GITHUB_PATH"
{
  echo "TESTBOX_FIXTURE_URL=postgres://user:pass@localhost:5433/db?sslmode=disable"
  echo "TESTBOX_FIXTURE_QUOTED=it's \"quoted\" \$HOME"
  echo 'TESTBOX_FIXTURE_LINES<<TESTBOX_EOF'
  printf 'first line\nNOT_A_NAME=second line\n'
  echo 'TESTBOX_EOF'
} >> "$GITHUB_ENV"
