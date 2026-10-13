#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
##############################################################################
# Collect build logs and test reports and publish them to an S3 log
# server, to a GitHub workflow artifact, or to both.
#
# Collection, console capture, metadata generation and compression are
# independent of the destination; only the final upload stage differs.
#
# This reproduces what Jenkins does today through global-jjb's
# lf-infra-publish macro -> shell/logs-deploy.sh -> lftools deploy s3,
# with three deliberate corrections:
#
#   1. Compressed files keep their original object key. Jenkins renames
#      log.html to log.html.gz, which breaks every relative link inside a
#      multi file report. Setting Content-Encoding instead preserves the
#      links and still saves the bytes.
#   2. Content types come from an explicit table rather than the Python
#      mimetypes database, whose answers vary by interpreter version and
#      which collapses css, js and json to text/plain.
#   3. Index markers are written to genuinely nested prefixes, rather than
#      to a bare second segment at the bucket root.
##############################################################################

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
destination="${INPUT_DESTINATION:-s3}"
s3_bucket="${INPUT_S3_BUCKET:-}"
s3_path_input="${INPUT_S3_PATH:-}"
path_prefix="${INPUT_PATH_PREFIX:-logs}"
namespace="${INPUT_NAMESPACE:-gha}"
job_name="${INPUT_JOB_NAME:-}"
workspace="${INPUT_WORKSPACE:-}"
archive_dir="${INPUT_ARCHIVE_DIR:-archives}"
archive_patterns="${INPUT_ARCHIVE_PATTERNS:-}"
capture_console="${INPUT_CAPTURE_CONSOLE:-false}"
include_sysinfo="${INPUT_INCLUDE_SYSINFO:-true}"
compress="${INPUT_COMPRESS:-true}"
compress_suffix="${INPUT_COMPRESS_SUFFIX:-false}"
index_markers="${INPUT_INDEX_MARKERS:-true}"
base_url="${INPUT_BASE_URL:-}"
aws_region="${INPUT_AWS_REGION:-us-east-1}"
github_token="${INPUT_GITHUB_TOKEN:-}"
dry_run="${INPUT_DRY_RUN:-false}"
permit_fail="${INPUT_PERMIT_FAIL:-true}"

workspace="${workspace:-${GITHUB_WORKSPACE:-$PWD}}"
job_name="${job_name:-${GITHUB_JOB:-job}}"

# Which destinations are active. Everything up to the upload stage runs
# identically either way; only the final hand off differs.
uses_s3='false'
uses_artifact='false'
case "$destination" in
    s3) uses_s3='true' ;;
    artifact) uses_artifact='true' ;;
    both)
        uses_s3='true'
        uses_artifact='true'
        ;;
esac

# Files matching these are worth compressing. Binary and already
# compressed formats are excluded; gzipping them wastes CPU and can
# inflate the result.
COMPRESS_EXTENSIONS='html|htm|log|txt|xml|json|css|js|csv|md|yaml|yml|svg'

upload_count=0
failed_count=0
file_count=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Reduce an arbitrary string to something safe for an S3 key segment.
# Matrix job names routinely contain spaces, slashes and parentheses, for
# example "15.1.x / publish-nexus"; left unsanitised these would silently
# create extra path levels.
slugify() {
    printf '%s' "$1" |
        tr '[:upper:]' '[:lower:]' |
        sed -e 's/[^a-z0-9._-]\+/-/g' -e 's/^-\+//' -e 's/-\+$//' |
        cut -c1-100
}

# Map a filename to a content type. Driven by an explicit table so the
# result does not depend on the host's mime database.
content_type_for() {
    local name
    name="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    case "$name" in
        *.html | *.htm) printf 'text/html' ;;
        *.css) printf 'text/css' ;;
        *.js | *.mjs) printf 'text/javascript' ;;
        *.json) printf 'application/json' ;;
        *.xml) printf 'application/xml' ;;
        *.svg) printf 'image/svg+xml' ;;
        *.png) printf 'image/png' ;;
        *.jpg | *.jpeg) printf 'image/jpeg' ;;
        *.gif) printf 'image/gif' ;;
        *.ico) printf 'image/vnd.microsoft.icon' ;;
        *.webp) printf 'image/webp' ;;
        *.woff) printf 'font/woff' ;;
        *.woff2) printf 'font/woff2' ;;
        *.ttf) printf 'font/ttf' ;;
        *.pdf) printf 'application/pdf' ;;
        *.csv) printf 'text/csv' ;;
        *.md) printf 'text/markdown' ;;
        *.yaml | *.yml) printf 'application/yaml' ;;
        *.zip) printf 'application/zip' ;;
        *.jar) printf 'application/java-archive' ;;
        *.tar) printf 'application/x-tar' ;;
        *.gz | *.tgz) printf 'application/gzip' ;;
        # Everything else, including the extensionless and .log cases that
        # dominate build output, is readable text.
        *) printf 'text/plain' ;;
    esac
}

should_compress() {
    local name
    name="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    [ "$compress" = 'true' ] || return 1
    printf '%s' "$name" | grep -qE "\.(${COMPRESS_EXTENSIONS})$"
}

log_info() { echo "$*"; }
log_warn() { echo "::warning::$*"; }
log_error() { echo "::error::$*"; }

# ---------------------------------------------------------------------------
# Resolve the destination path
# ---------------------------------------------------------------------------
if [ -n "$s3_path_input" ]; then
    s3_path="${s3_path_input#/}"
    s3_path="${s3_path%/}"
else
    repo_slug="$(slugify "${GITHUB_REPOSITORY##*/}")"
    workflow_slug="$(slugify "${GITHUB_WORKFLOW:-workflow}")"
    job_slug="$(slugify "$job_name")"
    run_number="${GITHUB_RUN_NUMBER:-0}"
    run_attempt="${GITHUB_RUN_ATTEMPT:-1}"
    s3_path="${path_prefix}/${namespace}/${repo_slug}/${workflow_slug}"
    s3_path="${s3_path}/${run_number}/${run_attempt}/${job_slug}"
fi

if [ "$uses_s3" = 'true' ]; then
    log_info "Publishing to s3://${s3_bucket}/${s3_path}/"
fi
if [ "$uses_artifact" = 'true' ]; then
    log_info "Publishing as workflow artifact (destination: ${destination})"
fi

# ---------------------------------------------------------------------------
# Stage the content
# ---------------------------------------------------------------------------
staging="$(mktemp -d -t log-publish.XXXXXXXX)"
manifest="${staging}.manifest"
: > "$manifest"

cleanup() {
    rm -f "$manifest" 2> /dev/null || true
    # A dry run exists to be inspected, so the staged tree is left in
    # place and its location reported.
    if [ "$dry_run" = 'true' ]; then
        return 0
    fi
    # The artifact destination uploads the staged tree from a later
    # composite step, which cannot run if this script removes it. That
    # step owns the cleanup instead.
    if [ "$uses_artifact" = 'true' ]; then
        return 0
    fi
    rm -rf "$staging" 2> /dev/null || true
}
trap cleanup EXIT

# Copy a file into the staging tree, preserving its path relative to the
# workspace so the published layout mirrors the build layout.
stage_file() {
    local src="$1" rel="$2" dest
    dest="${staging}/${rel}"
    mkdir -p "$(dirname "$dest")"
    cp -f "$src" "$dest" 2> /dev/null || return 0
}

# 1. The conventional archives directory, copied wholesale.
if [ -d "${workspace}/${archive_dir}" ]; then
    log_info "Collecting ${archive_dir}/ ..."
    while IFS= read -r -d '' src; do
        rel="${src#"${workspace}/${archive_dir}/"}"
        stage_file "$src" "$rel"
    done < <(find "${workspace}/${archive_dir}" -type f -print0 2> /dev/null)
fi

# 2. Explicit glob patterns, the ARCHIVE_ARTIFACTS equivalent.
if [ -n "$archive_patterns" ]; then
    log_info 'Collecting archive_patterns ...'
    shopt -s globstar nullglob dotglob
    while IFS= read -r pattern; do
        pattern="$(printf '%s' "$pattern" | sed -e 's/^[[:space:]]*//' \
            -e 's/[[:space:]]*$//')"
        [ -z "$pattern" ] && continue
        case "$pattern" in
            /* | *..*)
                log_warn "Ignoring unsafe pattern: $pattern"
                continue
                ;;
        esac
        matched=0
        for src in "${workspace}"/${pattern}; do
            [ -f "$src" ] || continue
            rel="${src#"${workspace}/"}"
            stage_file "$src" "$rel"
            matched=$((matched + 1))
        done
        log_info "  ${pattern} -> ${matched} file(s)"
    done <<< "$archive_patterns"
    shopt -u globstar nullglob dotglob
fi

# 3. Build metadata, the _build-details.log equivalent.
{
    echo "build-url: ${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
    echo "repository: ${GITHUB_REPOSITORY:-}"
    echo "workflow: ${GITHUB_WORKFLOW:-}"
    echo "job: ${job_name}"
    echo "run-id: ${GITHUB_RUN_ID:-}"
    echo "run-number: ${GITHUB_RUN_NUMBER:-}"
    echo "run-attempt: ${GITHUB_RUN_ATTEMPT:-}"
    echo "ref: ${GITHUB_REF:-}"
    echo "sha: ${GITHUB_SHA:-}"
    echo "event: ${GITHUB_EVENT_NAME:-}"
    echo "actor: ${GITHUB_ACTOR:-}"
    echo "runner-os: ${RUNNER_OS:-}"
    echo "published: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} > "${staging}/_build-details.log"

# 4. Runner characteristics, the _sys-info.log equivalent.
if [ "$include_sysinfo" = 'true' ]; then
    {
        echo '--- uname ---'
        uname -a 2> /dev/null || true
        echo
        echo '--- cpu ---'
        nproc 2> /dev/null || true
        echo
        echo '--- memory ---'
        free -h 2> /dev/null || true
        echo
        echo '--- disk ---'
        df -h 2> /dev/null || true
    } > "${staging}/_sys-info.log" 2> /dev/null || true
fi

# 5. Console logs, fetched through the REST API. A job cannot read its own
#    log while it is still running, which is why this belongs in a final
#    job that depends on the others.
if [ "$capture_console" = 'true' ] && [ "$dry_run" != 'true' ]; then
    log_info 'Capturing console logs ...'
    api="${GITHUB_API_URL:-https://api.github.com}"
    jobs_url="${api}/repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
    jobs_url="${jobs_url}/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs?per_page=100"
    if jobs_json="$(curl -sSfL \
        -H "Authorization: Bearer ${github_token}" \
        -H 'Accept: application/vnd.github+json' \
        "$jobs_url" 2> /dev/null)"; then
        mkdir -p "${staging}/console"
        while IFS=$'\t' read -r jid jname; do
            [ -z "$jid" ] && continue
            target="${staging}/console/$(slugify "$jname").log"
            if curl -sSfL \
                -H "Authorization: Bearer ${github_token}" \
                -H 'Accept: application/vnd.github+json' \
                "${api}/repos/${GITHUB_REPOSITORY}/actions/jobs/${jid}/logs" \
                -o "$target" 2> /dev/null; then
                log_info "  captured ${jname}"
            else
                rm -f "$target"
                log_warn "Could not capture console log for ${jname}"
            fi
        done < <(printf '%s' "$jobs_json" |
            jq -r '.jobs[] | [(.id|tostring), .name] | @tsv' 2> /dev/null)
    else
        log_warn 'Could not list jobs; console logs not captured'
    fi
fi

# ---------------------------------------------------------------------------
# Compress and build the manifest
# ---------------------------------------------------------------------------
while IFS= read -r -d '' src; do
    rel="${src#"${staging}/"}"
    encoding=''
    key="$rel"

    if should_compress "$rel"; then
        if gzip -9 -c "$src" > "${src}.gz" 2> /dev/null; then
            mv -f "${src}.gz" "$src"
            encoding='gzip'
            # Legacy Jenkins naming, off by default: renaming the object
            # breaks relative links between report pages.
            if [ "$compress_suffix" = 'true' ]; then
                key="${rel}.gz"
            fi
        fi
    fi

    # The content type always reflects the original filename. A gzipped
    # log.html is still text/html; the compression is described by
    # Content-Encoding, not by the type.
    ctype="$(content_type_for "$rel")"
    size="$(wc -c < "$src" | tr -d ' ')"

    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$rel" "$key" "$ctype" "${encoding:--}" "$size" >> "$manifest"
    file_count=$((file_count + 1))
done < <(find "$staging" -type f -print0 | sort -z)

log_info "Collected ${file_count} file(s)"

if [ "$file_count" -eq 0 ]; then
    log_warn 'No files collected; nothing to publish'
fi

# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------
if [ "$dry_run" = 'true' ]; then
    log_info 'Dry run: no S3 calls will be made'
    log_info "staging-dir: ${staging}"
    log_info '--- manifest ---'
    cat "$manifest"
elif [ "$uses_s3" = 'true' ]; then
    export AWS_REGION="$aws_region"
    export AWS_DEFAULT_REGION="$aws_region"

    while IFS=$'\t' read -r rel key ctype encoding _size; do
        args=(--content-type "$ctype" --only-show-errors)
        if [ "$encoding" != '-' ]; then
            args+=(--content-encoding "$encoding")
        fi
        # A transient TLS or DNS blip should not cost the whole upload.
        attempt=1
        ok='false'
        while [ "$attempt" -le 3 ]; do
            if aws s3 cp "${staging}/${rel}" \
                "s3://${s3_bucket}/${s3_path}/${key}" \
                "${args[@]}" 2>&1; then
                ok='true'
                break
            fi
            log_warn "Upload attempt ${attempt} failed for ${key}"
            attempt=$((attempt + 1))
            sleep $((attempt * 2))
        done
        if [ "$ok" = 'true' ]; then
            upload_count=$((upload_count + 1))
        else
            failed_count=$((failed_count + 1))
            log_error "Failed to upload ${key}"
        fi
    done < "$manifest"

    log_info "Uploaded ${upload_count} file(s), ${failed_count} failure(s)"

    # -----------------------------------------------------------------------
    # Index markers
    #
    # S3 has no directories, so the log server regenerates index.html from
    # object events. Writing and immediately removing a marker in each of
    # the first three prefixes is what triggers that regeneration.
    # -----------------------------------------------------------------------
    if [ "$index_markers" = 'true' ] && [ "$failed_count" -eq 0 ]; then
        marker="${staging}/_tmpfile"
        : > "$marker"
        prefix=''
        seg_index=0
        IFS='/' read -r -a segments <<< "$s3_path"
        for segment in "${segments[@]}"; do
            [ "$seg_index" -ge 3 ] && break
            prefix="${prefix}${segment}/"
            if aws s3 cp "$marker" "s3://${s3_bucket}/${prefix}_tmpfile" \
                --only-show-errors 2>&1; then
                aws s3 rm "s3://${s3_bucket}/${prefix}_tmpfile" \
                    --only-show-errors 2>&1 || true
                log_info "Refreshed index for ${prefix}"
            else
                log_warn "Could not refresh index for ${prefix}"
            fi
            seg_index=$((seg_index + 1))
        done
    fi
fi

# ---------------------------------------------------------------------------
# Artifact hand off
#
# actions/upload-artifact is an action, not a CLI, so the upload itself
# happens in the composite step that follows. All this stage does is keep
# the staged tree and report where it is.
# ---------------------------------------------------------------------------
staging_dir_output=''
artifact_name_output=''
if [ "$dry_run" = 'true' ]; then
    staging_dir_output="$staging"
elif [ "$uses_artifact" = 'true' ]; then
    staging_dir_output="$staging"
    artifact_name_output="${INPUT_ARTIFACT_NAME:-build-logs}"
    log_info "Staged tree retained for artifact upload: ${staging}"
fi

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
log_url=''
if [ -n "$base_url" ] && [ "$uses_s3" = 'true' ]; then
    log_url="${base_url%/}/${s3_path}/"
fi

persisted_manifest="${RUNNER_TEMP:-/tmp}/log-publish-manifest.tsv"
cp -f "$manifest" "$persisted_manifest" 2> /dev/null || true

if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
        echo "s3_path=${s3_path}"
        echo "log_url=${log_url}"
        echo "file_count=${file_count}"
        echo "upload_count=${upload_count}"
        echo "failed_count=${failed_count}"
        echo "manifest_path=${persisted_manifest}"
        echo "staging_dir=${staging_dir_output}"
        echo "artifact_name=${artifact_name_output}"
    } >> "$GITHUB_OUTPUT"
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
        echo '### 🪵 Build logs published'
        echo
        if [ -n "$log_url" ]; then
            echo "[Browse logs](${log_url})"
        elif [ "$uses_s3" = 'true' ]; then
            echo "\`s3://${s3_bucket}/${s3_path}/\`"
        fi
        if [ "$uses_artifact" = 'true' ] && [ "$dry_run" != 'true' ]; then
            echo "Workflow artifact: \`${artifact_name_output}\`"
        fi
        echo
        echo "- Destination: ${destination}"
        echo "- Files collected: ${file_count}"
        if [ "$dry_run" = 'true' ]; then
            echo '- Dry run: no files uploaded'
        elif [ "$uses_s3" = 'true' ]; then
            echo "- Uploaded: ${upload_count}"
            echo "- Failed: ${failed_count}"
        fi
    } >> "$GITHUB_STEP_SUMMARY"
fi

if [ "$failed_count" -gt 0 ]; then
    if [ "$permit_fail" = 'true' ]; then
        log_warn "${failed_count} file(s) failed to upload (permit_fail) ⚠️"
        exit 0
    fi
    log_error "${failed_count} file(s) failed to upload ❌"
    exit 1
fi

log_info 'Log publishing complete ✅'
