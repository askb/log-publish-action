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

s3_bucket="${INPUT_S3_BUCKET:-}"
s3_path="${INPUT_S3_PATH:-}"
path_prefix="${INPUT_PATH_PREFIX:-logs}"
namespace="${INPUT_NAMESPACE:-gha}"
capture_console="${INPUT_CAPTURE_CONSOLE:-false}"
github_token="${INPUT_GITHUB_TOKEN:-}"
dry_run="${INPUT_DRY_RUN:-false}"

echo 'Validating inputs...'

if [ -z "$s3_bucket" ]; then
    echo 'Error: s3_bucket is required ❌'
    exit 1
fi

# S3 bucket naming rules: 3-63 chars, lowercase alphanumeric, dots and
# hyphens, must start and end alphanumeric. Catching this here avoids a
# confusing failure deep inside the AWS CLI.
if ! printf '%s' "$s3_bucket" |
    grep -qE '^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'; then
    echo "Error: s3_bucket '$s3_bucket' is not a valid bucket name ❌"
    exit 1
fi

for flag in capture_console dry_run; do
    case "$flag" in
        capture_console) value="$capture_console" ;;
        dry_run) value="$dry_run" ;;
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
    trimmed="${s3_path#/}"
    trimmed="${trimmed%/}"
    segments="$(printf '%s' "$trimmed" | awk -F/ '{print NF}')"
    if [ "$segments" -lt 3 ]; then
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

if [ "$dry_run" != 'true' ] && ! command -v aws > /dev/null 2>&1; then
    echo 'Error: the AWS CLI is not installed ❌'
    echo '  GitHub hosted runners ship it; self hosted runners may not.'
    exit 1
fi

echo 'All inputs validated ✅'
