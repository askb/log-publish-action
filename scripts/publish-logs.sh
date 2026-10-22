#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
##############################################################################
# Collect build logs and test reports and publish them to an S3 log
# server, a Cloudflare R2 bucket, a GitHub workflow artifact, or any
# combination of those.
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
r2_bucket="${INPUT_R2_BUCKET:-}"
r2_account_id="${INPUT_R2_ACCOUNT_ID:-}"
r2_endpoint_url="${INPUT_R2_ENDPOINT_URL:-}"
r2_access_key_id="${INPUT_R2_ACCESS_KEY_ID:-}"
r2_secret_access_key="${INPUT_R2_SECRET_ACCESS_KEY:-}"
r2_base_url="${INPUT_R2_BASE_URL:-}"
r2_index="${INPUT_R2_INDEX:-true}"
github_token="${INPUT_GITHUB_TOKEN:-}"
dry_run="${INPUT_DRY_RUN:-false}"
permit_fail="${INPUT_PERMIT_FAIL:-true}"

workspace="${workspace:-${GITHUB_WORKSPACE:-$PWD}}"
job_name="${job_name:-${GITHUB_JOB:-job}}"

if [ -z "$r2_endpoint_url" ] && [ -n "$r2_account_id" ]; then
    r2_endpoint_url="https://${r2_account_id}.r2.cloudflarestorage.com"
fi

# Which destinations are active. Everything up to the upload stage runs
# identically either way; only the final hand off differs. The list is
# comma or space separated and its order picks the primary log_url, so
# "r2,s3" dual publishes but advertises the R2 copy.
uses_s3='false'
uses_r2='false'
uses_artifact='false'
remote_order=()
for dest in ${destination//,/ }; do
    case "$dest" in
        s3)
            uses_s3='true'
            remote_order+=(s3)
            ;;
        r2)
            uses_r2='true'
            remote_order+=(r2)
            ;;
        artifact) uses_artifact='true' ;;
        both)
            uses_s3='true'
            uses_artifact='true'
            remote_order+=(s3)
            ;;
    esac
done

# Files matching these are worth compressing. Binary and already
# compressed formats are excluded; gzipping them wastes CPU and can
# inflate the result.
COMPRESS_EXTENSIONS='html|htm|log|txt|xml|json|css|js|csv|md|yaml|yml|svg'

upload_count=0
failed_count=0
file_count=0
s3_upload_count=0
s3_failed_count=0
r2_upload_count=0
r2_failed_count=0

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

# The AWS CLI against the AWS account the caller authenticated to, using
# whatever credentials are already in the environment (normally the
# short lived OIDC session from configure-aws-credentials).
s3_cli() {
    AWS_REGION="$aws_region" AWS_DEFAULT_REGION="$aws_region" aws "$@"
}

# The AWS CLI against Cloudflare R2. Runs in a subshell so the R2 token
# never replaces the AWS session used by s3_cli, which is what makes dual
# publishing possible. R2 ignores the region but the CLI demands one, and
# only computes optional checksums when R2 actually requires them.
r2_cli() {
    (
        unset AWS_SESSION_TOKEN AWS_SECURITY_TOKEN AWS_PROFILE
        export AWS_ACCESS_KEY_ID="$r2_access_key_id"
        export AWS_SECRET_ACCESS_KEY="$r2_secret_access_key"
        export AWS_REGION='auto'
        export AWS_DEFAULT_REGION='auto'
        export AWS_REQUEST_CHECKSUM_CALCULATION='when_required'
        export AWS_RESPONSE_CHECKSUM_VALIDATION='when_required'
        aws --endpoint-url "$r2_endpoint_url" "$@"
    )
}

# Upload one file with retries. A transient TLS or DNS blip should not
# cost the whole upload.
upload_object() {
    local target="$1" bucket="$2" src="$3" key="$4" ctype="$5" encoding="$6"
    local args attempt=1
    args=(--content-type "$ctype" --only-show-errors)
    if [ "$encoding" != '-' ]; then
        args+=(--content-encoding "$encoding")
    fi
    while [ "$attempt" -le 3 ]; do
        if "${target}_cli" s3 cp "$src" \
            "s3://${bucket}/${s3_path}/${key}" "${args[@]}" 2>&1; then
            return 0
        fi
        log_warn "${target}: upload attempt ${attempt} failed for ${key}"
        attempt=$((attempt + 1))
        sleep $((attempt * 2))
    done
    return 1
}

# Upload the whole manifest, plus any generated index pages, to one
# target. Leaves per target counts in target_upload_count and
# target_failed_count and adds them to the overall totals.
upload_manifest() {
    local target="$1" bucket="$2" rel key ctype encoding page
    target_upload_count=0
    target_failed_count=0
    while IFS=$'\t' read -r rel key ctype encoding _size; do
        if upload_object "$target" "$bucket" "${staging}/${rel}" \
            "$key" "$ctype" "$encoding"; then
            target_upload_count=$((target_upload_count + 1))
        else
            target_failed_count=$((target_failed_count + 1))
            log_error "${target}: failed to upload ${key}"
        fi
    done < "$manifest"
    if [ "$target" = 'r2' ] && [ -d "$index_dir" ]; then
        while IFS= read -r -d '' page; do
            key="${page#"${index_dir}/"}"
            if upload_object r2 "$bucket" "$page" "$key" 'text/html' '-'; then
                target_upload_count=$((target_upload_count + 1))
            else
                target_failed_count=$((target_failed_count + 1))
                log_error "r2: failed to upload ${key}"
            fi
        done < <(find "$index_dir" -type f -print0 | sort -z)
    fi
    upload_count=$((upload_count + target_upload_count))
    failed_count=$((failed_count + target_failed_count))
    log_info "${target}: uploaded ${target_upload_count} file(s)," \
        "${target_failed_count} failure(s)"
}

# S3 has no directories, so the log server regenerates index.html from
# object events. Writing and immediately removing a marker in each of
# the first three prefixes is what triggers that regeneration.
refresh_s3_index_markers() {
    local marker prefix='' seg_index=0 segment segments
    marker="${staging}/_tmpfile"
    : > "$marker"
    IFS='/' read -r -a segments <<< "$s3_path"
    for segment in "${segments[@]}"; do
        [ "$seg_index" -ge 3 ] && break
        prefix="${prefix}${segment}/"
        if s3_cli s3 cp "$marker" "s3://${s3_bucket}/${prefix}_tmpfile" \
            --only-show-errors 2>&1; then
            s3_cli s3 rm "s3://${s3_bucket}/${prefix}_tmpfile" \
                --only-show-errors 2>&1 || true
            log_info "Refreshed index for ${prefix}"
        else
            log_warn "Could not refresh index for ${prefix}"
        fi
        seg_index=$((seg_index + 1))
    done
    rm -f "$marker"
}

html_escape() {
    sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
        -e 's/"/\&quot;/g' <<< "$1"
}

# Browsers treat a backslash in an https URL as a slash, so encode it.
href_escape() {
    sed -e 's/%/%25/g' -e 's/\\/%5C/g' -e 's/ /%20/g' -e 's/#/%23/g' -e 's/?/%3F/g' \
        -e 's/&/\&amp;/g' -e 's/"/%22/g' -e 's/</%3C/g' -e 's/>/%3E/g' \
        <<< "$1"
}

# R2 has no equivalent of the S3 log server's listing Lambda, so render a
# plain index.html for every directory of this run. A directory that
# already ships its own index.html, such as an Allure report, keeps it.
generate_index_pages() {
    local dir kind name title page
    rm -rf "$index_dir"
    mkdir -p "$index_dir"
    # One "directory<TAB>kind<TAB>child" row per immediate child.
    cut -f2 "$manifest" | awk -F/ '{
        parent = ""
        for (i = 1; i <= NF; i++) {
            kind = (i < NF) ? "d" : "f"
            print parent "\t" kind "\t" $i
            parent = parent $i "/"
        }
    }' | sort -u > "${index_dir}.rows"
    while IFS= read -r dir; do
        # Defence in depth: the manifest already excludes control
        # characters, but never let a row steer a page outside the tree.
        case "/${dir}" in
            */../* | */./*)
                log_warn "Skipping index for unsafe directory: ${dir}"
                continue
                ;;
        esac
        if grep -qxF -- "${dir}index.html" < <(cut -f2 "$manifest"); then
            continue
        fi
        page="${index_dir}/${dir}index.html"
        mkdir -p "$(dirname "$page")"
        title="$(html_escape "/${s3_path}/${dir}")"
        {
            echo '<!DOCTYPE html>'
            echo '<html><head><meta charset="utf-8">'
            echo "<title>Index of ${title}</title></head><body>"
            echo "<h1>Index of ${title}</h1><ul>"
            [ -n "$dir" ] && echo '<li><a href="../">../</a></li>'
            # Emit just kind and name: root rows have an empty first
            # column, and read would collapse that leading tab.
            while IFS=$'\t' read -r kind name; do
                [ "$kind" = 'd' ] && name="${name}/"
                # The ./ prefix keeps a name such as javascript:x.log a
                # relative path rather than a URL scheme.
                echo "<li><a href=\"./$(href_escape "$name")\">$(html_escape "$name")</a></li>"
            done < <(awk -F'\t' -v d="$dir" -v OFS='\t' '$1 == d {print $2, $3}' \
                "${index_dir}.rows" | sort -t$'\t' -k1,1 -k2,2)
            echo '</ul></body></html>'
        } > "$page"
    done < <(cut -f1 "${index_dir}.rows" | sort -u)
    rm -f "${index_dir}.rows"
}

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
if [ "$uses_r2" = 'true' ]; then
    log_info "Publishing to r2://${r2_bucket}/${s3_path}/"
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

index_dir="${staging}.index"

cleanup() {
    rm -f "$manifest" 2> /dev/null || true
    # A dry run exists to be inspected, so the staged tree and any
    # generated index pages are left in place and their location reported.
    if [ "$dry_run" = 'true' ]; then
        return 0
    fi
    rm -rf "$index_dir" 2> /dev/null || true
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
    # The manifest is tab separated and line based, so a tab, newline or
    # other control character in a path would corrupt it and could steer
    # later stages to the wrong key. Such names are never legitimate
    # build output; drop them.
    if [[ "$rel" =~ [[:cntrl:]] ]]; then
        log_warn "Skipping file with control characters: $(printf '%q' "$rel")"
        rm -f "$src"
        continue
    fi
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
    log_info 'Dry run: no S3 or R2 calls will be made'
    log_info "staging-dir: ${staging}"
    log_info '--- manifest ---'
    cat "$manifest"
    if [ "$uses_r2" = 'true' ] && [ "$r2_index" = 'true' ]; then
        generate_index_pages
        log_info "index-dir: ${index_dir}"
        log_info '--- r2 index pages ---'
        (cd "$index_dir" && find . -type f -name index.html | sed 's|^\./||' | sort)
    fi
else
    if [ "$uses_s3" = 'true' ]; then
        upload_manifest s3 "$s3_bucket"
        s3_upload_count="$target_upload_count"
        s3_failed_count="$target_failed_count"
        if [ "$index_markers" = 'true' ] && [ "$s3_failed_count" -eq 0 ]; then
            refresh_s3_index_markers
        fi
    fi
    if [ "$uses_r2" = 'true' ]; then
        if [ "$r2_index" = 'true' ]; then
            generate_index_pages
        fi
        upload_manifest r2 "$r2_bucket"
        r2_upload_count="$target_upload_count"
        r2_failed_count="$target_failed_count"
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
s3_log_url=''
r2_log_url=''
if [ -n "$base_url" ] && [ "$uses_s3" = 'true' ]; then
    s3_log_url="${base_url%/}/${s3_path}/"
fi
if [ -n "$r2_base_url" ] && [ "$uses_r2" = 'true' ]; then
    r2_log_url="${r2_base_url%/}/${s3_path}/"
fi
# The first destination listed that has a public URL is the primary.
log_url=''
for dest in "${remote_order[@]}"; do
    case "$dest" in
        s3) candidate="$s3_log_url" ;;
        r2) candidate="$r2_log_url" ;;
        *) candidate='' ;;
    esac
    if [ -n "$candidate" ]; then
        log_url="$candidate"
        break
    fi
done

persisted_manifest="${RUNNER_TEMP:-/tmp}/log-publish-manifest.tsv"
cp -f "$manifest" "$persisted_manifest" 2> /dev/null || true

if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
        echo "s3_path=${s3_path}"
        echo "log_url=${log_url}"
        echo "s3_log_url=${s3_log_url}"
        echo "r2_log_url=${r2_log_url}"
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
        if [ "$uses_s3" = 'true' ]; then
            if [ -n "$s3_log_url" ]; then
                echo "- S3: [Browse logs](${s3_log_url})"
            else
                echo "- S3: \`s3://${s3_bucket}/${s3_path}/\`"
            fi
        fi
        if [ "$uses_r2" = 'true' ]; then
            if [ -n "$r2_log_url" ]; then
                echo "- R2: [Browse logs](${r2_log_url})"
            else
                echo "- R2: \`r2://${r2_bucket}/${s3_path}/\`"
            fi
        fi
        if [ "$uses_artifact" = 'true' ] && [ "$dry_run" != 'true' ]; then
            echo "Workflow artifact: \`${artifact_name_output}\`"
        fi
        echo
        echo "- Destination: ${destination}"
        echo "- Files collected: ${file_count}"
        if [ "$dry_run" = 'true' ]; then
            echo '- Dry run: no files uploaded'
        else
            if [ "$uses_s3" = 'true' ]; then
                echo "- S3 uploaded: ${s3_upload_count}, failed: ${s3_failed_count}"
            fi
            if [ "$uses_r2" = 'true' ]; then
                echo "- R2 uploaded: ${r2_upload_count}, failed: ${r2_failed_count}"
            fi
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
