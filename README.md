<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 🪵 Log Publishing Action

Publishes build logs and test reports to an S3 log server, a Cloudflare
R2 bucket, GitHub workflow artifacts, or any combination of those.

## log-publish-action

This action restores, for GitHub Actions, the post-build log shipping that
Jenkins performs today through `global-jjb`'s `lf-infra-publish` macro
(`logs-deploy.sh` → `lftools deploy s3`). It writes to the same bucket
layout, so the existing server-side directory index generation keeps
working without any change to the log server.

Build diagnostics that die with the runner — `karaf.log` from
single-feature tests, Robot Framework results, Allure reports, surefire
output — become durable, linkable URLs.

## Features

- **Jenkins Path Parity**: Writes the same bucket layout the log server
  already indexes, so published directories remain browsable
- **Survives Failure**: Designed to run under `if: always()`, because the
  logs worth keeping come from builds that failed
- **Link-Safe Compression**: Gzips in place rather than renaming to `.gz`,
  so relative links inside multi-file reports keep resolving
- **Correct Content Types**: Explicit type table covering HTML, CSS, JS,
  JSON, XML, images and archives
- **Console Log Capture**: Optionally retrieves each job's console log
  through the GitHub REST API, the analogue of Jenkins' `console.log`
- **Matrix Safe**: Requires a per-leg job identifier so parallel matrix
  legs cannot overwrite each other
- **Retried Uploads**: Three attempts with backoff per object
- **Non-Masking**: `permit_fail` defaults true, so losing logs never
  changes the result of the build that produced them
- **Offline Testable**: `dry_run` exercises the whole pipeline with no AWS
  credentials and no network access
- **Destination Agnostic**: Collection, console capture, metadata and
  compression are independent of where the result lands, so the same
  pipeline feeds S3, R2, workflow artifacts, or any mix of them
- **Dual Publishing**: `s3,r2` writes the same tree to both stores with
  separate credentials, for a gradual migration between them

## Usage Examples

### Basic

Collect `archives/` and publish it, whatever the build result:

<!-- markdownlint-disable MD013 -->

```yaml
- name: "Publish build logs"
  if: always()
  uses: lfreleng-actions/log-publish-action@main
  with:
    s3_bucket: "logs.example.org"
    base_url: "https://logs.example.org"
```

### Workflow artifacts instead of S3

This needs no bucket, no credentials and no network access to PyPI:

```yaml
- name: "Publish build logs"
  if: always()
  uses: lfreleng-actions/log-publish-action@main
  with:
    destination: "artifact"
    artifact_name: "build-logs"
    artifact_retention_days: "30"
```

### Cloudflare R2

Cloudflare offers no OIDC federation, so pass an R2 API token from
secrets. The token never replaces an AWS session already present in the
job:

```yaml
- name: "Publish build logs"
  if: always()
  uses: lfreleng-actions/log-publish-action@main
  with:
    destination: "r2"
    r2_bucket: ${{ vars.R2_LOG_BUCKET }}
    r2_account_id: ${{ vars.R2_ACCOUNT_ID }}
    r2_access_key_id: ${{ secrets.R2_LOG_ACCESS_KEY_ID }}
    r2_secret_access_key: ${{ secrets.R2_LOG_SECRET_ACCESS_KEY }}
    r2_base_url: ${{ vars.R2_LOG_BASE_URL }}
```

### Dual publishing during a migration

List both stores. The first one listed that has a public URL becomes
`log_url`; `s3_log_url` and `r2_log_url` expose each copy:

```yaml
- uses: aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd # v6.3.0
  with:
    role-to-assume: ${{ vars.AWS_LOG_ROLE_ARN }}
    aws-region: ${{ vars.S3_LOG_REGION }}
- name: "Publish build logs"
  if: always()
  uses: lfreleng-actions/log-publish-action@main
  with:
    destination: "s3,r2"
    s3_bucket: ${{ vars.S3_LOG_BUCKET }}
    base_url: ${{ vars.LOG_BASE_URL }}
    r2_bucket: ${{ vars.R2_LOG_BUCKET }}
    r2_account_id: ${{ vars.R2_ACCOUNT_ID }}
    r2_access_key_id: ${{ secrets.R2_LOG_ACCESS_KEY_ID }}
    r2_secret_access_key: ${{ secrets.R2_LOG_SECRET_ACCESS_KEY }}
    r2_base_url: ${{ vars.R2_LOG_BASE_URL }}
```

### Maven build with test artifacts

```yaml
- name: "Publish build logs"
  if: always()
  uses: lfreleng-actions/log-publish-action@main
  with:
    s3_bucket: "logs.example.org"
    base_url: "https://logs.example.org"
    archive_patterns: |
      **/target/surefire-reports/*.xml
      **/target/SFT/*.log
```

### Allure report

Allure output is a multi-file site; `compress_suffix` must stay `false`
or its internal references break.

```yaml
- name: "Publish Allure report"
  if: always()
  uses: lfreleng-actions/log-publish-action@main
  with:
    s3_bucket: "logs.example.org"
    base_url: "https://logs.example.org"
    archive_patterns: "allure-report/**"
```

The report is then served at
`<log_url>allure-report/index.html`.

### Matrix builds

Without a distinct `job_name`, every matrix leg resolves to the same
prefix and the legs overwrite one another.

```yaml
strategy:
  matrix:
    jdk: ["17", "21"]
steps:
  - name: "Publish build logs"
    if: always()
    uses: lfreleng-actions/log-publish-action@main
    with:
      s3_bucket: "logs.example.org"
      job_name: "build-jdk${{ matrix.jdk }}"
```

### Console log capture

A job cannot read its own log while that job runs, so console capture
belongs in a dedicated final job.

```yaml
publish-logs:
  needs: [build, test]
  if: always()
  runs-on: ubuntu-latest
  permissions:
    contents: read
    actions: read        # required to read job logs
    id-token: write      # required for OIDC
  steps:
    - uses: aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd # v6.3.0
      with:
        role-to-assume: ${{ vars.AWS_LOG_ROLE_ARN }}
        aws-region: ${{ vars.S3_LOG_REGION }}

    - uses: lfreleng-actions/log-publish-action@main
      with:
        s3_bucket: ${{ vars.S3_LOG_BUCKET }}
        base_url: ${{ vars.LOG_BASE_URL }}
        capture_console: true
        github_token: ${{ secrets.GITHUB_TOKEN }}
```

<!-- markdownlint-enable MD013 -->

## Choosing a Destination

<!-- markdownlint-disable MD013 -->

|                     | `s3`                             | `r2`                               | `artifact`                                             |
| ------------------- | -------------------------------- | ---------------------------------- | ------------------------------------------------------ |
| Anonymous access    | Yes                              | Yes, through a custom domain       | **No** — requires a GitHub login, even on public repos |
| HTML reports        | Rendered in the browser          | Rendered in the browser            | Zip download; unpack Allure and Robot first            |
| Directory listings  | Server-side Lambda               | `index.html` written by the action | Not applicable                                         |
| Credentials         | OIDC, short-lived                | R2 API token in secrets            | None                                                   |
| Retention           | Bucket lifecycle, today 180 days | Bucket lifecycle rule              | 1–90 days on public repos                              |
| Stable per-file URL | Yes                              | Yes                                | No — artifact IDs change every run                     |
| Cost                | Storage plus egress              | Storage, no egress fees            | Free for public repositories                           |

<!-- markdownlint-enable MD013 -->

Use `s3` when a link has to work for someone who is not signed in: a bug
report, a mailing list thread, or a dashboard pointing at trend history.
Use `artifact` for bulk content that maintainers alone open, where a login
costs nothing. `r2` gives the same anonymous URLs as `s3` without egress
charges. Combine any of them, for example `s3,r2` or `r2,artifact`, to
write the same set to each. The legacy value `both` means `s3,artifact`.

### Driving the choice from an organisation variable

Pin the destination at the workflow layer rather than in each repository.
One organisation variable then moves an entire fleet, with no action
release and no change to any calling repository:

```yaml
- uses: lfreleng-actions/log-publish-action@v1
  with:
    destination: ${{ vars.LOG_PUBLISH_DESTINATION || 's3' }}
    s3_bucket: ${{ vars.S3_LOG_BUCKET }}
```

That matters because the log server is infrastructure. Should it retire
on cost grounds, the collection, console capture, metadata and
compression stages all keep working; the hand-off alone moves.

## Inputs

<!-- markdownlint-disable MD013 -->

| Input                        | Required | Default             | Description                                                        |
| ---------------------------- | -------- | ------------------- | ------------------------------------------------------------------ |
| `destination`                | No       | `s3`                | List of `s3`, `r2`, `artifact`; `both` means `s3,artifact`         |
| `s3_bucket`                  | Cond.    |                     | Target S3 bucket; required unless `destination` is `artifact`      |
| `s3_path`                    | No       |                     | Full path in the bucket; overrides the derived path                |
| `path_prefix`                | No       | `logs`              | Top-level prefix within the bucket                                 |
| `namespace`                  | No       | `gha`               | Second path element, analogous to the Jenkins silo                 |
| `job_name`                   | No       | `$GITHUB_JOB`       | Job identifier in the path; set per matrix leg                     |
| `workspace`                  | No       | `$GITHUB_WORKSPACE` | Directory to collect files from                                    |
| `archive_dir`                | No       | `archives`          | Directory below the workspace always collected                     |
| `archive_patterns`           | No       |                     | Newline-separated globs, relative to the workspace                 |
| `capture_console`            | No       | `false`             | Fetch job console logs through the REST API                        |
| `include_sysinfo`            | No       | `true`              | Write a `_sys-info.log` describing the runner                      |
| `compress`                   | No       | `true`              | Compress text content before upload                                |
| `compress_suffix`            | No       | `false`             | Append `.gz` to compressed files (legacy Jenkins naming)           |
| `index_markers`              | No       | `true`              | Trigger server-side regeneration of `index.html`                   |
| `base_url`                   | No       |                     | Public base URL of the log server, used to build `log_url`         |
| `aws_region`                 | No       | `us-east-1`         | AWS region of the bucket                                           |
| `r2_bucket`                  | Cond.    |                     | Target R2 bucket; required when `destination` includes `r2`        |
| `r2_account_id`              | Cond.    |                     | Cloudflare account id; derives the R2 S3 API endpoint              |
| `r2_endpoint_url`            | No       |                     | Explicit R2 endpoint; overrides `r2_account_id`                    |
| `r2_access_key_id`           | Cond.    |                     | R2 API token access key id; pass from a secret                     |
| `r2_secret_access_key`       | Cond.    |                     | R2 API token secret access key; pass from a secret                 |
| `r2_base_url`                | No       |                     | Public R2 custom domain URL, used to build `r2_log_url`            |
| `r2_index`                   | No       | `true`              | Write `index.html` listings on R2, keeping any existing one        |
| `artifact_name`              | No       | `build-logs`        | Name of the uploaded artifact; set per matrix leg                  |
| `artifact_retention_days`    | No       |                     | Artifact retention; empty uses the repository default              |
| `artifact_if_no_files_found` | No       | `warn`              | Behaviour when the run collects nothing: `warn`, `error`, `ignore` |
| `github_token`               | No       |                     | Token for `capture_console`                                        |
| `dry_run`                    | No       | `false`             | Collect and build the manifest, but make no S3 or R2 calls         |
| `permit_fail`                | No       | `true`              | Do not fail the step when publishing fails                         |

## Outputs

| Output          | Description                                          |
| --------------- | ---------------------------------------------------- |
| `s3_path`       | Resolved path within the bucket                      |
| `log_url`       | Public URL on the first listed remote destination    |
| `s3_log_url`    | Public URL of the S3 copy, empty when not published  |
| `r2_log_url`    | Public URL of the R2 copy, empty when not published  |
| `file_count`    | Number of files collected                            |
| `upload_count`  | Files uploaded, summed across S3 and R2              |
| `failed_count`  | Number of files that failed to upload                |
| `manifest_path` | Path to the generated upload manifest                |
| `staging_dir`   | Staged tree, when the artifact destination is in use |
| `artifact_name` | Name of the uploaded artifact, empty when none       |

<!-- markdownlint-enable MD013 -->

## Published Layout

The derived path is:

```text
<path_prefix>/<namespace>/<repo>/<workflow>/<run_number>/<run_attempt>/<job>/
```

For example:

```text
logs/gha/yangtools/gerrit-verify/42/1/build/
```

The action publishes the contents of `archives/` at the root of that
directory, matching the Jenkins convention. Files matching
`archive_patterns` keep their path relative to the workspace. The action
also writes:

| File                 | Contents                                      |
| -------------------- | --------------------------------------------- |
| `_build-details.log` | Repository, workflow, run, actor, SHA, result |
| `_sys-info.log`      | Runner OS, architecture, tool versions        |
| `console.log`        | Job console output, when `capture_console`    |

### Why the path must be at least three segments deep

The log server regenerates `index.html` from an S3 event. The action
writes, then deletes, a marker object in each of the first three path
segments to trigger that regeneration. A shallower path leaves parts of
the hierarchy unbrowsable, so the action rejects one. Setting
`index_markers` to `false` lifts the rule, and the action then accepts a
path of any depth.

## Deviations From The Jenkins Behaviour

This action differs from `lftools deploy s3` in three places by design.
Each difference fixes a defect that remains observable in production.

### 1. Compression does not rename the object

`lftools` gzips `**/*.{html,log,txt,xml}` and renames the result to
`.gz`. Any relative link to the original name then breaks. This is
visible on the live log server: a Robot Framework `report.html.gz`
contains `href="log.html"`, and that URL returns **404**, because the
publisher renamed the file to `log.html.gz`. The report-to-log
click-through has failed for years.

The same mechanism would break Allure far more severely, since Allure is
a wholly multi-file site that loads `app.js`, `styles.css` and
`data/*.json` by relative path.

This action instead gzips **in place**: the object key does not change,
the response carries `Content-Encoding: gzip`, and the real
`Content-Type` survives. Browsers decompress transparently, links keep
working, and the upload still saves the bytes. Set `compress_suffix:
true` to restore the legacy naming.

### 2. Content types are explicit

`lftools` special-cases three types: `text/plain`, `text/html` and
`application/xml`; everything else falls through to `text/plain`, which
mislabels `.css`, `.js`, `.json` and `.png`.

The behaviour also varies by interpreter. On Python 3.14,
`mimetypes.guess_type('out.xml')` returns `text/xml`, not
`application/xml`, so the XML branch becomes dead code and `out.xml.gz`
loses its `Content-Encoding` entirely — served as binary garbage. This
is a latent regression that appears purely on a newer interpreter.

This action uses an explicit extension-to-type table, which does not
depend on any mime database.

### 3. Index markers use cumulative prefixes

`lftools` builds its second marker prefix from the second path segment
alone rather than from the first two, so that marker lands at the bucket
root. Verified: `s3-logs.opendaylight.org/releng/index.html` returns
404, while `/logs/releng/index.html` returns 200. Harmless, but wrong.

This action writes cumulative prefixes.

## Prerequisites

### AWS credentials

These apply when `destination` is `s3` or `both`. Publishing to
artifacts alone requires no bucket and no credentials.

The action needs `s3:PutObject` and `s3:DeleteObject` on the target
prefix. Use OIDC rather than long-lived keys:

<!-- markdownlint-disable MD013 -->

```yaml
permissions:
  id-token: write
  contents: read

steps:
  - uses: aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd # v6.3.0
    with:
      role-to-assume: ${{ vars.AWS_LOG_ROLE_ARN }}
      aws-region: ${{ vars.S3_LOG_REGION }}
```

<!-- markdownlint-enable MD013 -->

The IAM trust policy should scope to the organisation's repositories,
and the permission policy to the log prefix alone. Scope the trust
`sub` claim to branch refs so pull requests and forks cannot assume
the role:

<!-- markdownlint-disable MD013 -->

```json
"StringLike": {
  "token.actions.githubusercontent.com:sub": "repo:my-org/*:ref:refs/heads/*"
}
```

<!-- markdownlint-enable MD013 -->

Place `configure-aws-credentials` next to this action rather than at the
top of a long job. The session is short-lived, so a role assumed at the
start of a multi-hour build can expire before the upload runs.

### R2 credentials and bucket setup

These apply when `destination` includes `r2`. R2 speaks the S3 API, so
the action uses a second boto3 client against
`https://<account_id>.r2.cloudflarestorage.com` with region `auto`. That
client carries the R2 token explicitly, never the AWS session, and skips
the optional checksums R2 rejects.

On the Cloudflare side:

1. Create the bucket and add an object lifecycle rule that deletes
   objects after the retention period, for example 180 days.
2. Connect a custom domain for public access, and keep the `r2.dev`
   development URL disabled. The custom domain's zone must live in the
   same Cloudflare account.
3. R2 does not map `dir/` to `dir/index.html`. Add a URL rewrite rule on
   the custom domain that appends `index.html` to paths ending in `/`.
   The action writes those pages when `r2_index` is `true`.
4. Create an R2 API token with **Object Read & Write** on that bucket
   alone, and store it as `R2_LOG_ACCESS_KEY_ID` and
   `R2_LOG_SECRET_ACCESS_KEY` organisation secrets.

A reusable workflow sees the secrets its caller passes and no others, so
callers need `secrets: inherit` or explicit secret mappings.

### Tooling

The action runs on the runner's own `python3`, version 3.10 or later,
with `pip`. GitHub-hosted Ubuntu runners have both; self-hosted runners
may need them installed. The action validates this before it does any
work, and needs no `setup-python` step.

Uploads to S3 or R2 use boto3, which the action installs at run time
from the hash-pinned `requirements.txt` into the runner temp directory.
pip refuses any package or hash not in that file. A dry run or an
artifact destination alone installs nothing.

Jobs that run harden-runner with `egress-policy: block` must allow
`pypi.org:443` and `files.pythonhosted.org:443` for that install, as
well as the S3 or R2 endpoint.

## Testing

The pytest suite runs fully offline. It needs
[uv](https://docs.astral.sh/uv/):

```shell
uv sync --frozen
uv run pytest
```

It covers input validation, path derivation, content collection,
content-type assignment, compression (including gzip round-tripping of
the actual staged bytes), outputs and robustness. Upload logic runs
against recording fakes. The wire tests then drive real boto3 clients
and intercept each request before it leaves the process, proving which
endpoint, credentials and headers each store receives, and that S3 and
R2 never share credentials.

CI runs the suite on every supported Python, 3.10 to 3.15. To run it
locally on a given version:

```shell
uv run --python 3.15 pytest
```

`uv.lock` pins every dependency. After changing it, regenerate the
runtime requirements the action installs from; CI fails if they drift:

```shell
uv export --frozen --no-dev --no-emit-project -o requirements.txt
```

## Troubleshooting

| Symptom                    | Cause                                         |
| -------------------------- | --------------------------------------------- |
| Step skipped on failure    | Missing `if: always()` (implicit `success()`) |
| Matrix legs overwrite      | `job_name` not set per leg                    |
| `index.html` not refreshed | Markers off, or path under 3 segments         |
| Console log empty          | Captured from the job that reads it           |
| `403` on upload            | IAM policy misses the target prefix           |
| R2 directory URL 404s      | Missing rewrite rule for `/` → `index.html`   |
| R2 `401`/`403` on upload   | Token not scoped to the bucket, or expired    |
| Report links 404           | `compress_suffix` on a multi-file report      |

## Implementation Notes

A bare `if:` expression in GitHub Actions carries an implicit
`success()`. The official documentation states: *"A default status check
of `success()` applies unless you include one of these functions."*
A step guarded by `if: ${{ env.SOMETHING != '' }}` alone will not run
when an earlier step fails — the moment when logs matter most. Always
guard log publishing with `if: always()`.
