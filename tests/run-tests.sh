#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
##############################################################################
# Offline test suite for log-publish-action.
#
# Everything runs through dry_run, so the suite needs no AWS credentials
# and no network access. That matters: the lfreleng test workflows run
# under harden-runner with egress blocked.
#
# Usage: bash tests/run-tests.sh
##############################################################################

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION_PATH="$(dirname "$SCRIPT_DIR")"
PUBLISH="${ACTION_PATH}/scripts/publish-logs.sh"
VALIDATE="${ACTION_PATH}/scripts/validate.sh"

pass=0
fail=0

ok() {
    echo "  ✅ $1"
    pass=$((pass + 1))
}

ko() {
    echo "  ❌ $1"
    [ -n "${2:-}" ] && echo "     $2"
    fail=$((fail + 1))
}

assert_contains() {
    local haystack="$1" needle="$2" label="$3"
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        ok "$label"
    else
        ko "$label" "expected to find: $needle"
    fi
}

assert_not_contains() {
    local haystack="$1" needle="$2" label="$3"
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
        ko "$label" "did not expect to find: $needle"
    else
        ok "$label"
    fi
}

assert_eq() {
    if [ "$1" = "$2" ]; then
        ok "$3"
    else
        ko "$3" "expected '$2', got '$1'"
    fi
}

# Build a throwaway workspace that looks like a real build tree.
make_workspace() {
    local ws
    ws="$(mktemp -d -t lpa-ws.XXXXXXXX)"
    mkdir -p "$ws/archives" \
        "$ws/target/SFT" \
        "$ws/target/surefire-reports" \
        "$ws/allure-report/data" \
        "$ws/allure-report/history"

    echo 'karaf started' > "$ws/archives/karaf.log"
    echo 'some notes' > "$ws/archives/notes.txt"
    printf 'PNG\x89fakebinary' > "$ws/archives/screenshot.png"

    echo 'feature test output' > "$ws/target/SFT/karaf.log"
    echo '<testsuite/>' > "$ws/target/surefire-reports/TEST-a.xml"

    echo '<html><a href="data/suites.json">x</a></html>' \
        > "$ws/allure-report/index.html"
    echo 'body{color:red}' > "$ws/allure-report/styles.css"
    echo 'console.log(1)' > "$ws/allure-report/app.js"
    echo '{"uid":"1"}' > "$ws/allure-report/data/suites.json"
    echo '{"items":[]}' > "$ws/allure-report/history/history.json"

    printf '%s' "$ws"
}

# Run the publisher in dry-run mode and return its combined output.
run_publish() {
    local ws="$1"
    shift
    local out_file
    out_file="$(mktemp -t lpa-out.XXXXXXXX)"
    (
        export GITHUB_REPOSITORY='opendaylight/yangtools'
        export GITHUB_WORKFLOW='Gerrit Maven Merge'
        export GITHUB_RUN_NUMBER='4242'
        export GITHUB_RUN_ATTEMPT='2'
        export GITHUB_RUN_ID='38258444807'
        export GITHUB_SERVER_URL='https://github.com'
        export GITHUB_SHA='deadbeef'
        export GITHUB_REF='refs/heads/master'
        export GITHUB_JOB='maven-merge'
        export GITHUB_ACTOR='askb'
        export GITHUB_EVENT_NAME='push'
        export RUNNER_OS='Linux'
        export INPUT_S3_BUCKET='logs-example-org'
        export INPUT_WORKSPACE="$ws"
        export INPUT_DRY_RUN='true'
        export INPUT_BASE_URL='https://logs.example.org'
        unset GITHUB_OUTPUT GITHUB_STEP_SUMMARY
        # Per-test overrides arrive as NAME=VALUE arguments.
        for kv in "$@"; do
            export "${kv?}"
        done
        bash "$PUBLISH"
    ) > "$out_file" 2>&1
    cat "$out_file"
    rm -f "$out_file"
}

# A dry run deliberately leaves its staging tree behind for inspection.
# Tests that only read the manifest should not leak it into /tmp.
run_publish_clean() {
    local output staged
    output="$(run_publish "$@")"
    staged="$(printf '%s' "$output" | sed -n 's/^staging-dir: //p' | head -1)"
    case "$staged" in
        */log-publish.*) rm -rf "$staged" ;;
    esac
    printf '%s' "$output"
}

# Capture what the script wrote to GITHUB_OUTPUT rather than stdout.
# Needed for the artifact destination, whose whole contract is the
# staging_dir output consumed by the upload step.
run_publish_outputs() {
    local ws="$1"
    shift
    local out_file result
    out_file="$(mktemp -t lpa-ghout.XXXXXXXX)"
    run_publish "$ws" "GITHUB_OUTPUT=$out_file" "$@" > /dev/null 2>&1
    result="$(cat "$out_file")"
    rm -f "$out_file"
    printf '%s' "$result"
}

# Read one key from a captured GITHUB_OUTPUT block.
output_field() {
    printf '%s' "$1" | sed -n "s/^${2}=//p" | head -1
}

# Extract one manifest column for a staged path.
manifest_field() {
    local output="$1" rel="$2" col="$3"
    printf '%s' "$output" |
        awk -F'\t' -v r="$rel" -v c="$col" '$1 == r {print $c; exit}'
}

echo '════════════════════════════════════════════════════════'
echo ' log-publish-action test suite'
echo '════════════════════════════════════════════════════════'

# ---------------------------------------------------------------------------
echo
echo '▶ Input validation'
# ---------------------------------------------------------------------------

out="$(INPUT_S3_BUCKET='' bash "$VALIDATE" 2>&1)"
assert_contains "$out" 's3_bucket is required' 'missing bucket is rejected'

out="$(INPUT_S3_BUCKET='Not_A_Bucket' bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'not a valid bucket name' 'invalid bucket is rejected'

out="$(INPUT_S3_BUCKET='logs-example-org' INPUT_S3_PATH='logs/gha' \
    bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'at least three segments' 'shallow s3_path is rejected'

out="$(INPUT_S3_BUCKET='logs-example-org' INPUT_DRY_RUN='true' \
    INPUT_S3_PATH='logs/gha/yangtools/verify/1' bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'validated' 'deep s3_path is accepted'

out="$(INPUT_S3_BUCKET='logs-example-org' INPUT_CAPTURE_CONSOLE='true' \
    INPUT_GITHUB_TOKEN='' INPUT_DRY_RUN='false' bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'requires github_token' 'console capture needs a token'

out="$(INPUT_S3_BUCKET='logs-example-org' INPUT_DRY_RUN='maybe' \
    bash "$VALIDATE" 2>&1)"
assert_contains "$out" "must be 'true' or 'false'" 'non boolean flag rejected'

# ---------------------------------------------------------------------------
echo
echo '▶ Path derivation'
# ---------------------------------------------------------------------------

ws="$(make_workspace)"
out="$(run_publish_clean "$ws")"

assert_contains "$out" \
    'logs/gha/yangtools/gerrit-maven-merge/4242/2/maven-merge' \
    'derived path includes repo, workflow, run, attempt and job'

# A matrix leg name must not be able to inject extra path segments.
out="$(run_publish_clean "$ws" 'INPUT_JOB_NAME=15.1.x / publish-nexus')"
assert_contains "$out" '4242/2/15.1.x-publish-nexus' \
    'matrix job name is slugified into a single segment'
assert_not_contains "$out" '15.1.x / publish-nexus/' \
    'matrix job name does not create nested paths'

# Two different matrix legs must resolve to different prefixes, or they
# would overwrite each other in S3.
a="$(run_publish_clean "$ws" 'INPUT_JOB_NAME=15.1.x / publish' |
    grep -o 'logs/gha/[^ ]*' | head -1)"
b="$(run_publish_clean "$ws" 'INPUT_JOB_NAME=16.0.x / publish' |
    grep -o 'logs/gha/[^ ]*' | head -1)"
if [ "$a" != "$b" ]; then
    ok 'distinct matrix legs resolve to distinct prefixes'
else
    ko 'distinct matrix legs resolve to distinct prefixes' "both were $a"
fi

out="$(run_publish_clean "$ws" 'INPUT_S3_PATH=custom/one/two/three')"
assert_contains "$out" 'custom/one/two/three' 's3_path override is honoured'

# ---------------------------------------------------------------------------
echo
echo '▶ Content collection'
# ---------------------------------------------------------------------------

out="$(run_publish_clean "$ws")"
assert_contains "$out" 'karaf.log' 'archives directory is collected'
assert_contains "$out" '_build-details.log' 'build metadata is written'
assert_contains "$out" '_sys-info.log' 'runner info is written'

out="$(run_publish_clean "$ws" 'INPUT_INCLUDE_SYSINFO=false')"
assert_not_contains "$out" '_sys-info.log' 'runner info can be disabled'

out="$(run_publish_clean "$ws" \
    'INPUT_ARCHIVE_PATTERNS=target/SFT/*.log
target/surefire-reports/*.xml')"
assert_contains "$out" 'target/SFT/karaf.log' 'glob pattern collects SFT logs'
assert_contains "$out" 'target/surefire-reports/TEST-a.xml' \
    'glob pattern collects surefire reports'

out="$(run_publish_clean "$ws" 'INPUT_ARCHIVE_PATTERNS=/etc/passwd')"
assert_contains "$out" 'Ignoring unsafe pattern' 'absolute pattern is refused'

out="$(run_publish_clean "$ws" 'INPUT_ARCHIVE_PATTERNS=../../../etc/passwd')"
assert_contains "$out" 'Ignoring unsafe pattern' 'traversal pattern is refused'

# ---------------------------------------------------------------------------
echo
echo '▶ Content types'
# ---------------------------------------------------------------------------

out="$(run_publish_clean "$ws" 'INPUT_ARCHIVE_PATTERNS=allure-report/**')"

assert_eq "$(manifest_field "$out" 'allure-report/index.html' 3)" \
    'text/html' 'html is text/html'
assert_eq "$(manifest_field "$out" 'allure-report/styles.css' 3)" \
    'text/css' 'css is text/css (lftools would say text/plain)'
assert_eq "$(manifest_field "$out" 'allure-report/app.js' 3)" \
    'text/javascript' 'js is text/javascript (lftools would say text/plain)'
assert_eq "$(manifest_field "$out" 'allure-report/data/suites.json' 3)" \
    'application/json' 'json is application/json'
assert_eq "$(manifest_field "$out" 'karaf.log' 3)" \
    'text/plain' 'log is text/plain'
assert_eq "$(manifest_field "$out" 'screenshot.png' 3)" \
    'image/png' 'png is image/png (lftools would say text/plain)'
assert_eq "$(manifest_field "$out" 'target/surefire-reports/TEST-a.xml' 3)" \
    '' 'files outside the pattern are not collected'

out="$(run_publish_clean "$ws" 'INPUT_ARCHIVE_PATTERNS=target/**/*.xml')"
assert_eq "$(manifest_field "$out" 'target/surefire-reports/TEST-a.xml' 3)" \
    'application/xml' 'xml is application/xml regardless of mime database'

# ---------------------------------------------------------------------------
echo
echo '▶ Compression'
# ---------------------------------------------------------------------------

out="$(run_publish_clean "$ws" 'INPUT_ARCHIVE_PATTERNS=allure-report/**')"

assert_eq "$(manifest_field "$out" 'karaf.log' 4)" \
    'gzip' 'text content is compressed'
assert_eq "$(manifest_field "$out" 'screenshot.png' 4)" \
    '-' 'binary content is not compressed'

# The regression that matters: compressing must not rename the object, or
# every relative link inside a report breaks.
assert_eq "$(manifest_field "$out" 'allure-report/index.html' 2)" \
    'allure-report/index.html' 'compression preserves the object key'
assert_eq "$(manifest_field "$out" 'allure-report/index.html' 3)" \
    'text/html' 'compressed html keeps its real content type'
assert_eq "$(manifest_field "$out" 'allure-report/index.html' 4)" \
    'gzip' 'compressed html is marked gzip encoded'

out="$(run_publish_clean "$ws" 'INPUT_COMPRESS_SUFFIX=true')"
assert_eq "$(manifest_field "$out" 'karaf.log' 2)" \
    'karaf.log.gz' 'legacy suffix mode renames the object'

out="$(run_publish_clean "$ws" 'INPUT_COMPRESS=false')"
assert_eq "$(manifest_field "$out" 'karaf.log' 4)" \
    '-' 'compression can be disabled'

# ---------------------------------------------------------------------------
echo
echo '▶ Outputs'
# ---------------------------------------------------------------------------

tmp_out="$(mktemp -t lpa-gho.XXXXXXXX)"
tmp_sum="$(mktemp -t lpa-sum.XXXXXXXX)"
(
    export GITHUB_OUTPUT="$tmp_out"
    export GITHUB_STEP_SUMMARY="$tmp_sum"
    run_publish_clean "$ws" "GITHUB_OUTPUT=$tmp_out" "GITHUB_STEP_SUMMARY=$tmp_sum"
) > /dev/null 2>&1

outputs="$(cat "$tmp_out" 2> /dev/null)"
summary="$(cat "$tmp_sum" 2> /dev/null)"

assert_contains "$outputs" 's3_path=logs/gha/yangtools' 's3_path output set'
assert_contains "$outputs" \
    'log_url=https://logs.example.org/logs/gha/yangtools' 'log_url output set'
assert_contains "$outputs" 'file_count=' 'file_count output set'
assert_contains "$summary" 'Build logs published' 'step summary written'

rm -f "$tmp_out" "$tmp_sum"

# ---------------------------------------------------------------------------
echo
echo '▶ Robustness'
# ---------------------------------------------------------------------------

empty_ws="$(mktemp -d -t lpa-empty.XXXXXXXX)"
out="$(run_publish_clean "$empty_ws")"
rc=$?
assert_eq "$rc" '0' 'an empty workspace does not fail the build'
assert_contains "$out" '_build-details.log' 'metadata is published regardless'
rm -rf "$empty_ws"

# Filenames with spaces must survive collection intact.
space_ws="$(make_workspace)"
mkdir -p "$space_ws/archives/sub dir"
echo 'spaced' > "$space_ws/archives/sub dir/odd name.log"
out="$(run_publish_clean "$space_ws")"
assert_contains "$out" 'sub dir/odd name.log' 'paths with spaces are handled'
rm -rf "$space_ws"

# ---------------------------------------------------------------------------
echo
echo '▶ Byte level verification'
# ---------------------------------------------------------------------------

# The manifest says "gzip"; confirm the staged bytes really are gzip and
# that the original content survives a round trip.
out="$(run_publish "$ws" 'INPUT_ARCHIVE_PATTERNS=allure-report/**')"
staged="$(printf '%s' "$out" | sed -n 's/^staging-dir: //p' | head -1)"

if [ -n "$staged" ] && [ -d "$staged" ]; then
    ok 'dry run reports its staging directory'

    if file_out="$(gzip -t "$staged/karaf.log" 2>&1)"; then
        ok 'compressed log is valid gzip'
    else
        ko 'compressed log is valid gzip' "$file_out"
    fi

    assert_eq "$(gzip -dc "$staged/karaf.log" 2> /dev/null)" \
        'karaf started' 'compressed log round trips to original content'

    assert_eq "$(gzip -dc "$staged/allure-report/styles.css" 2> /dev/null)" \
        'body{color:red}' 'compressed css round trips'

    # A binary file must be byte identical, not silently gzipped.
    assert_eq "$(cat "$staged/screenshot.png" 2> /dev/null)" \
        "$(cat "$ws/archives/screenshot.png" 2> /dev/null)" \
        'binary file is stored untouched'

    # The link target inside the report must still exist under its own
    # name. This is the defect the legacy .gz renaming introduced.
    if [ -f "$staged/allure-report/data/suites.json" ]; then
        ok 'report asset keeps the name its html links to'
    else
        ko 'report asset keeps the name its html links to' \
            "$(ls "$staged/allure-report/data" 2> /dev/null)"
    fi

    rm -rf "$staged"
else
    ko 'dry run reports its staging directory' 'no staging-dir in output'
fi

# Under legacy suffix mode the link target is renamed, which is exactly
# why that mode is not the default.
out="$(run_publish "$ws" 'INPUT_ARCHIVE_PATTERNS=allure-report/**' \
    'INPUT_COMPRESS_SUFFIX=true')"
assert_eq "$(manifest_field "$out" 'allure-report/data/suites.json' 2)" \
    'allure-report/data/suites.json.gz' \
    'legacy suffix mode renames the linked asset (documented hazard)'
staged="$(printf '%s' "$out" | sed -n 's/^staging-dir: //p' | head -1)"
[ -n "$staged" ] && rm -rf "$staged"

rm -rf "$ws"

# ---------------------------------------------------------------------------
echo
echo '▶ Destination routing'
# ---------------------------------------------------------------------------
ws="$(make_workspace)"

out="$(INPUT_S3_BUCKET='logs-example-org' INPUT_DESTINATION='ftp' \
    bash "$VALIDATE" 2>&1 || true)"
assert_contains "$out" 'destination must list s3, r2, artifact or both' \
    'unknown destination is rejected'

# The point of the artifact destination is that it needs no S3 at all:
# no bucket, no credentials, no CLI.
out="$(INPUT_S3_BUCKET='' INPUT_DESTINATION='artifact' \
    INPUT_DRY_RUN='false' bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'validated' 'artifact destination needs no bucket'

out="$(INPUT_S3_BUCKET='' INPUT_DESTINATION='both' \
    bash "$VALIDATE" 2>&1 || true)"
assert_contains "$out" 's3_bucket is required' \
    'both destination still requires a bucket'

out="$(INPUT_S3_BUCKET='' INPUT_DESTINATION='s3' \
    bash "$VALIDATE" 2>&1 || true)"
assert_contains "$out" 's3_bucket is required' \
    'default destination still requires a bucket'

# The three segment rule exists only to place S3 index markers, so it
# must not constrain an artifact only run.
out="$(INPUT_S3_BUCKET='' INPUT_DESTINATION='artifact' \
    INPUT_S3_PATH='shallow' INPUT_DRY_RUN='false' bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'validated' \
    'artifact destination ignores the path depth rule'

# A real (non dry) artifact run must complete offline and leave the tree
# behind for the upload step.
outputs="$(run_publish_outputs "$ws" 'INPUT_DESTINATION=artifact' \
    'INPUT_DRY_RUN=false')"
staged="$(output_field "$outputs" 'staging_dir')"
assert_not_contains "$outputs" 'failed_count=1' \
    'artifact run reports no upload failures'
assert_eq "$(output_field "$outputs" 'artifact_name')" 'build-logs' \
    'artifact run reports the artifact name'
if [ -n "$staged" ] && [ -d "$staged" ]; then
    ok 'artifact run retains the staged tree for upload'
else
    ko 'artifact run retains the staged tree for upload' \
        "staging_dir='${staged}'"
fi
if [ -n "$staged" ] && [ -f "${staged}/karaf.log" ]; then
    ok 'retained tree holds the collected content'
else
    ko 'retained tree holds the collected content' 'karaf.log missing'
fi
# The composite step owns this cleanup in production.
[ -n "$staged" ] && rm -rf "$staged"

# A custom artifact name must reach the output the upload step reads.
outputs="$(run_publish_outputs "$ws" 'INPUT_DESTINATION=artifact' \
    'INPUT_DRY_RUN=false' 'INPUT_ARTIFACT_NAME=logs-jdk21')"
assert_eq "$(output_field "$outputs" 'artifact_name')" 'logs-jdk21' \
    'custom artifact name is honoured'
staged="$(output_field "$outputs" 'staging_dir')"
[ -n "$staged" ] && rm -rf "$staged"

# Pure S3 mode must not retain anything, or the cleanup step would fire
# against a path that no longer exists.
outputs="$(run_publish_outputs "$ws" 'INPUT_DESTINATION=s3' \
    'INPUT_DRY_RUN=true')"
assert_eq "$(output_field "$outputs" 'artifact_name')" '' \
    's3 destination reports no artifact name'
staged="$(output_field "$outputs" 'staging_dir')"
[ -n "$staged" ] && rm -rf "$staged"

rm -rf "$ws"

# ---------------------------------------------------------------------------
echo
echo '▶ R2 destination'
# ---------------------------------------------------------------------------
ws="$(make_workspace)"
r2_acct='0123456789abcdef0123456789abcdef'

out="$(INPUT_DESTINATION='r2' INPUT_DRY_RUN='true' bash "$VALIDATE" 2>&1 ||
    true)"
assert_contains "$out" 'r2_bucket is required' 'r2 requires a bucket'

out="$(INPUT_DESTINATION='r2' INPUT_R2_BUCKET='odl-logs' INPUT_DRY_RUN='true' \
    bash "$VALIDATE" 2>&1 || true)"
assert_contains "$out" 'r2_account_id' 'r2 requires an account or endpoint'

out="$(INPUT_DESTINATION='r2' INPUT_R2_BUCKET='odl-logs' \
    INPUT_R2_ENDPOINT_URL='http://insecure.example' INPUT_DRY_RUN='true' \
    bash "$VALIDATE" 2>&1 || true)"
assert_contains "$out" 'must be an https URL' 'r2 endpoint must be https'

out="$(INPUT_DESTINATION='r2' INPUT_R2_BUCKET='odl-logs' \
    INPUT_R2_ACCOUNT_ID="$r2_acct" INPUT_DRY_RUN='false' \
    bash "$VALIDATE" 2>&1 || true)"
assert_contains "$out" 'r2_secret_access_key are required' \
    'r2 requires credentials for a real run'

# R2 alone needs no S3 bucket.
out="$(INPUT_DESTINATION='r2' INPUT_S3_BUCKET='' INPUT_R2_BUCKET='odl-logs' \
    INPUT_R2_ACCOUNT_ID="$r2_acct" INPUT_DRY_RUN='true' \
    bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'validated' 'r2 destination needs no s3 bucket'

out="$(INPUT_DESTINATION='s3, r2,artifact' INPUT_S3_BUCKET='logs-example-org' \
    INPUT_R2_BUCKET='odl-logs' INPUT_R2_ACCOUNT_ID="$r2_acct" \
    INPUT_DRY_RUN='true' bash "$VALIDATE" 2>&1)"
assert_contains "$out" 'validated' 'mixed comma and space list is accepted'

out="$(INPUT_DESTINATION='s3,gcs' INPUT_S3_BUCKET='logs-example-org' \
    bash "$VALIDATE" 2>&1 || true)"
assert_contains "$out" 'destination must list' 'unknown list member rejected'

# Dry run renders index pages but never overwrites a report's own.
echo 'x' > "$ws/archives/javascript:alert(1).log"
out="$(run_publish "$ws" 'INPUT_DESTINATION=r2' 'INPUT_R2_BUCKET=odl-logs' \
    "INPUT_R2_ACCOUNT_ID=$r2_acct" 'INPUT_ARCHIVE_PATTERNS=allure-report/**')"
assert_contains "$out" 'r2://odl-logs/logs/gha/yangtools' 'r2 target is logged'
pages="$(printf '%s' "$out" | sed -n '/^--- r2 index pages ---$/,$p')"
assert_contains "$pages" $'\nindex.html' 'root index page is generated'
assert_contains "$pages" 'allure-report/data/index.html' \
    'nested index page is generated'
assert_not_contains "$pages" 'allure-report/index.html' \
    'existing report index.html is preserved'
idx="$(printf '%s' "$out" | sed -n 's/^index-dir: //p' | head -1)"
root_page="$(cat "${idx}/index.html" 2> /dev/null)"
assert_contains "$root_page" '<a href="./karaf.log">karaf.log</a>' \
    'root index links a top level file'
assert_contains "$root_page" '<a href="./allure-report/">allure-report/</a>' \
    'root index links a subdirectory'
assert_not_contains "$root_page" 'href=""' 'root index has no empty links'
assert_contains "$root_page" 'href="./javascript:alert(1).log"' \
    'scheme-like file name stays a relative link'
assert_not_contains "$root_page" 'href="javascript:' \
    'file name cannot inject a javascript: URL'
assert_not_contains "$root_page" 'href="../"' 'root index has no parent link'
data_page="$(cat "${idx}/allure-report/data/index.html" 2> /dev/null)"
assert_contains "$data_page" '<a href="./suites.json">suites.json</a>' \
    'nested index links its files'
assert_contains "$data_page" 'href="../"' 'nested index links its parent'
staged="$(printf '%s' "$out" | sed -n 's/^staging-dir: //p' | head -1)"
case "$idx" in */log-publish.*.index) rm -rf "$idx" ;; esac
[ -n "$staged" ] && rm -rf "$staged"

# A stub aws records every call, with the credentials and endpoint it ran
# under, so routing can be verified without any network access.
stub_dir="$(mktemp -d -t lpa-aws.XXXXXXXX)"
calls="${stub_dir}/calls.log"
cat > "${stub_dir}/aws" << 'STUB'
#!/usr/bin/env bash
printf '%s|%s|%s|%s\n' "${AWS_ACCESS_KEY_ID:-}" "${AWS_SESSION_TOKEN:-}" \
    "${AWS_REGION:-}" "$*" >> "$LPA_CALLS"
STUB
chmod +x "${stub_dir}/aws"

tmp_out="$(mktemp -t lpa-gho.XXXXXXXX)"
tmp_sum="$(mktemp -t lpa-sum.XXXXXXXX)"
run_publish "$ws" 'INPUT_DRY_RUN=false' 'INPUT_DESTINATION=r2,s3' \
    "PATH=${stub_dir}:${PATH}" "LPA_CALLS=${calls}" \
    'AWS_ACCESS_KEY_ID=AKIAOIDCSESSION' 'AWS_SESSION_TOKEN=oidc-session' \
    'INPUT_R2_BUCKET=odl-logs' "INPUT_R2_ACCOUNT_ID=$r2_acct" \
    'INPUT_R2_ACCESS_KEY_ID=r2-key-id' 'INPUT_R2_SECRET_ACCESS_KEY=r2-secret' \
    'INPUT_R2_BASE_URL=https://logs-r2.example.org' \
    "GITHUB_OUTPUT=$tmp_out" "GITHUB_STEP_SUMMARY=$tmp_sum" > /dev/null 2>&1
log="$(cat "$calls" 2> /dev/null)"
outputs="$(cat "$tmp_out" 2> /dev/null)"
summary="$(cat "$tmp_sum" 2> /dev/null)"

r2_calls="$(grep -F 'r2.cloudflarestorage.com' <<< "$log")"
s3_calls="$(grep -vF 'r2.cloudflarestorage.com' <<< "$log")"
assert_contains "$r2_calls" \
    "--endpoint-url https://${r2_acct}.r2.cloudflarestorage.com s3 cp" \
    'r2 uploads use the account endpoint'
assert_contains "$r2_calls" 's3://odl-logs/logs/gha/yangtools/' \
    'r2 uploads target the r2 bucket and shared layout'
assert_contains "$r2_calls" 'r2-key-id||auto|' \
    'r2 uploads use the r2 token, no session token, region auto'
assert_not_contains "$r2_calls" 'AKIAOIDCSESSION' \
    'r2 uploads never see the aws session'
assert_contains "$r2_calls" '/index.html --content-type text/html' \
    'r2 receives generated index pages'
assert_contains "$s3_calls" 'AKIAOIDCSESSION|oidc-session|us-east-1|s3 cp' \
    's3 uploads keep the oidc session'
assert_contains "$s3_calls" 's3://logs-example-org/logs/gha/yangtools/' \
    's3 uploads target the s3 bucket'
assert_not_contains "$s3_calls" 'r2-key-id' 's3 uploads never see the r2 token'
assert_contains "$s3_calls" '_tmpfile' 's3 still receives index markers'
assert_not_contains "$s3_calls" '/index.html --content-type' \
    's3 does not receive generated index pages'
assert_contains "$outputs" \
    'log_url=https://logs-r2.example.org/logs/gha/yangtools/' \
    'first listed destination (r2) is the primary log_url'
assert_contains "$outputs" 's3_log_url=https://logs.example.org/logs/gha/' \
    's3_log_url output set'
assert_contains "$outputs" 'r2_log_url=https://logs-r2.example.org/logs/' \
    'r2_log_url output set'
assert_contains "$outputs" 'failed_count=0' 'dual publish reports no failures'
assert_contains "$summary" '- R2: [Browse logs]' 'summary links the r2 copy'
assert_contains "$summary" '- S3: [Browse logs]' 'summary links the s3 copy'

# Order decides the primary.
: > "$calls"
outputs="$(run_publish_outputs "$ws" 'INPUT_DRY_RUN=false' \
    'INPUT_DESTINATION=s3,r2' "PATH=${stub_dir}:${PATH}" \
    "LPA_CALLS=${calls}" 'INPUT_R2_BUCKET=odl-logs' \
    "INPUT_R2_ACCOUNT_ID=$r2_acct" 'INPUT_R2_ACCESS_KEY_ID=k' \
    'INPUT_R2_SECRET_ACCESS_KEY=s' \
    'INPUT_R2_BASE_URL=https://logs-r2.example.org')"
assert_contains "$outputs" 'log_url=https://logs.example.org/logs/gha/' \
    'first listed destination (s3) is the primary log_url'

# Index generation can be turned off.
: > "$calls"
run_publish "$ws" 'INPUT_DRY_RUN=false' 'INPUT_DESTINATION=r2' \
    "PATH=${stub_dir}:${PATH}" "LPA_CALLS=${calls}" \
    'INPUT_R2_BUCKET=odl-logs' "INPUT_R2_ACCOUNT_ID=$r2_acct" \
    'INPUT_R2_ACCESS_KEY_ID=k' 'INPUT_R2_SECRET_ACCESS_KEY=s' \
    'INPUT_R2_INDEX=false' > /dev/null 2>&1
log="$(cat "$calls" 2> /dev/null)"
assert_not_contains "$log" '/index.html --content-type' \
    'r2_index=false skips generated pages'
assert_not_contains "$log" '_tmpfile' 'r2 never writes s3 index markers'

rm -rf "$stub_dir" "$tmp_out" "$tmp_sum" "$ws"

# ---------------------------------------------------------------------------
echo
echo '════════════════════════════════════════════════════════'
echo " Passed: ${pass}   Failed: ${fail}"
echo '════════════════════════════════════════════════════════'

[ "$fail" -eq 0 ] || exit 1
