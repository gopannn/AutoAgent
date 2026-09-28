#!/bin/sh
# Static analysis of the generated code inside the verifier image. Nothing here
# executes generated code. Workspace config files are ignored on purpose:
# ruff runs --isolated and pyright uses the compiler-owned config.
set -u
cd /workspace || exit 125

run() {
    name=$1
    shift
    "$@" > "/out/$name.log" 2>&1
    echo $? > "/out/$name.exit"
}

run ruff ruff check --isolated --no-cache --output-format concise --target-version py312 \
    --select E9,F,B,S --ignore S101,S104 .
run pyright pyright --project /harness/pyrightconfig.json
run semgrep semgrep scan --config /opt/semgrep-rules --metrics off --disable-version-check \
    --no-git-ignore --error /workspace
python /harness/versions.py > /out/versions_static.json 2> /out/versions_static.log
exit 0
