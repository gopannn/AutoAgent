#!/bin/sh
# Runs the compiler-owned acceptance tests against the service under test over
# the network. This container never mounts or imports generated code, so the
# code under test cannot tamper with the verdict written to /out.
set -u

python /harness/wait_ready.py "${SUT_REPLICA_URLS:-$SUT_BASE_URL}" "$READY_TIMEOUT" > /out/ready.log 2>&1
echo $? > /out/ready.exit

python /harness/versions.py > /out/versions_oracle.json 2> /out/versions_oracle.log

if [ "$(cat /out/ready.exit)" != "0" ]; then
    exit 0
fi

# With a backing service, state is reset before every test (plugins/vc_state_reset.py).
set --
if [ -n "${VC_REDIS_ADDR:-}" ]; then
    set -- -p vc_state_reset
    export PYTHONPATH=/harness/plugins
fi

cd /compiler_spec || exit 125
python -m pytest -c /dev/null --rootdir=/compiler_spec --import-mode=importlib \
    -p no:cacheprovider -o junit_family=xunit2 --junitxml=/out/junit.xml "$@" \
    -q -rA /compiler_spec > /out/pytest.log 2>&1
echo $? > /out/pytest.exit
exit 0
