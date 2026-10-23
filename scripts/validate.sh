#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
##############################################################################
# Validate the inputs supplied to log-publish-action.
#
# Kept separate from publish-logs.sh so that misconfiguration fails loudly
# and immediately, rather than being swallowed by permit_fail during the
# publish step.
##############################################################################

set -euo pipefail

destination="${INPUT_DESTINATION:-s3}"
s3_bucket="${INPUT_S3_BUCKET:-}"
s3_path="${INPUT_S3_PATH:-}"
path_prefix="${INPUT_PATH_PREFIX:-logs}"
namespace="${INPUT_NAMESPACE:-gha}"
capture_console="${INPUT_CAPTURE_CONSOLE:-false}"
r2_bucket="${INPUT_R2_BUCKET:-}"
r2_account_id="${INPUT_R2_ACCOUNT_ID:-}"
r2_endpoint_url="${INPUT_R2_ENDPOINT_URL:-}"
r2_access_key_id="${INPUT_R2_ACCESS_KEY_ID:-}"
r2_secret_access_key="${INPUT_R2_SECRET_ACCESS_KEY:-}"
r2_index="${INPUT_R2_INDEX:-true}"
github_token="${INPUT_GITHUB_TOKEN:-}"
dry_run="${INPUT_DRY_RUN:-false}"

echo 'Validating inputs...'

# A comma or space separated list of s3, r2 and artifact. The legacy
# value both means s3 plus artifact.
uses_s3='false'
uses_r2='false'
seen=0
for dest in ${destination//,/ }; do
    case "$dest" in
        s3) uses_s3='true' ;;
        r2) uses_r2='true' ;;
        artifact) ;;
        both) uses_s3='true' ;;
        *)
            echo "Error: destination must list s3, r2, artifact or both ❌"
            echo "  Received: '$destination'"
            exit 1
            ;;
    esac
    seen=$((seen + 1))
done
if [ "$seen" -eq 0 ]; then
    echo "Error: destination must list s3, r2, artifact or both ❌"
    exit 1
fi

# The S3 specific checks apply only when something is actually going to
# S3. An artifact only run needs no bucket, no credentials and no CLI.

if [ "$uses_s3" = 'true' ] && [ -z "$s3_bucket" ]; then
    echo "Error: s3_bucket is required when destination is '$destination' ❌"
    exit 1
fi

# S3 bucket naming rules: 3-63 chars, lowercase alphanumeric, dots and
# hyphens, must start and end alphanumeric. Catching this here avoids a
# confusing failure deep inside the AWS CLI.
if [ -n "$s3_bucket" ] &&
    ! printf '%s' "$s3_bucket" |
        grep -qE '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'; then
    echo "Error: s3_bucket '$s3_bucket' is not a valid bucket name ❌"
    exit 1
fi

if [ "$uses_r2" = 'true' ]; then
    if [ -z "$r2_bucket" ]; then
        echo "Error: r2_bucket is required when destination includes r2 ❌"
        exit 1
    fi
    if ! printf '%s' "$r2_bucket" |
        grep -qE '^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$'; then
        echo "Error: r2_bucket '$r2_bucket' is not a valid bucket name ❌"
        exit 1
    fi
    if [ -n "$r2_endpoint_url" ]; then
        case "$r2_endpoint_url" in
            https://*) ;;
            *)
                echo 'Error: r2_endpoint_url must be an https URL ❌'
                exit 1
                ;;
        esac
    elif ! printf '%s' "$r2_account_id" | grep -qE '^[0-9a-f]{32}$'; then
        echo 'Error: r2_account_id (32 hex characters) or r2_endpoint_url' \
            'is required when destination includes r2 ❌'
        exit 1
    fi
    if [ "$dry_run" != 'true' ] &&
        { [ -z "$r2_access_key_id" ] || [ -z "$r2_secret_access_key" ]; }; then
        echo 'Error: r2_access_key_id and r2_secret_access_key are' \
            'required when destination includes r2 ❌'
        echo '  Pass them from secrets; R2 has no OIDC federation.'
        exit 1
    fi
fi

for flag in capture_console dry_run r2_index; do
    case "$flag" in
        capture_console) value="$capture_console" ;;
        dry_run) value="$dry_run" ;;
        r2_index) value="$r2_index" ;;
        *) value='' ;;
    esac
    if [ "$value" != 'true' ] && [ "$value" != 'false' ]; then
        echo "Error: $flag must be 'true' or 'false', got '$value' ❌"
        exit 1
    fi
done

if [ -n "$s3_path" ]; then
    # The index marker is written to the first three path segments. A
    # shallower path would place markers outside the intended hierarchy.
    # Artifact only runs never write markers, so the depth is immaterial.
    trimmed="${s3_path#/}"
    trimmed="${trimmed%/}"
    segments="$(printf '%s' "$trimmed" | awk -F/ '{print NF}')"
    if [ "$uses_s3" = 'true' ] && [ "$segments" -lt 3 ]; then
        echo "Error: s3_path must be at least three segments deep ❌"
        echo "  Received: '$s3_path' ($segments segment(s))"
        echo "  The first three segments receive the index marker."
        exit 1
    fi
else
    if [ -z "$path_prefix" ] || [ -z "$namespace" ]; then
        echo 'Error: path_prefix and namespace cannot be empty ❌'
        echo '  Supply s3_path directly to use a custom layout.'
        exit 1
    fi
fi

if [ "$capture_console" = 'true' ] &&
    [ -z "$github_token" ] &&
    [ "$dry_run" != 'true' ]; then
    echo 'Error: capture_console requires github_token ❌'
    echo '  Grant the job actions:read and pass a token.'
    exit 1
fi

if { [ "$uses_s3" = 'true' ] || [ "$uses_r2" = 'true' ]; } &&
    [ "$dry_run" != 'true' ] &&
    ! command -v aws > /dev/null 2>&1; then
    echo 'Error: the AWS CLI is not installed ❌'
    echo '  GitHub hosted runners ship it; self hosted runners may not.'
    exit 1
fi

echo 'All inputs validated ✅'
