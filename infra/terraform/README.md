# Review API and step 8b deployment preparation

**No apply is authorized by this code.** The review API runs on Lambda behind an
API Gateway HTTP API. There is no Function URL. Neon is created manually by the
owner; Terraform does not create an account, project or database.

## Dependencies and apply approvals

Mangum `0.22.0` and `hashicorp/aws = 6.71.0` are approved and locked. Both
Terraform lock files cover Linux x86_64, Intel Mac and Apple Silicon Mac. Preserve
these checked-in locks; CI initializes with `-lockfile=readonly`. Handler tests use
the real Mangum adapter and synthetic API Gateway HTTP API events.

Every apply still requires a reviewed plan summary and explicit named approval.
The one-time bootstrap has its own plan/apply approval; `apply 8a` authorizes the
main 8a plan only. No script applies infrastructure automatically. Tests are
offline and do not contact AWS or Neon.

## Create Neon privately

Create your free-tier account/project yourself and send only its region, never
connection strings. Align `aws_region` with a suitable nearby AWS region. Keep two
connections: owner/direct for migrations and `ledgerguard_app`/pooled for runtime.
In AWS mode the runtime URL must explicitly set `sslmode=require`, `verify-ca`
or `verify-full`; missing or weaker modes are refused without exposing the URL.
Prefer certificate verification where configured. No Neon provider, token or URL
enters state.

Using the direct owner connection supplied privately as `DATABASE_URL`, run the
existing `uv run python -m ledgerguard.db migrate` command. It rejects Neon pooler
hosts and runs checkpoint setup with a session advisory lock. If the app role was
created NOLOGIN, enable LOGIN as owner and use psql's interactive `\password
ledgerguard_app` to choose its password without putting it in SQL or shell history.
The app connection must authenticate as that role. Runtime connections disable
prepared statements (`prepare_threshold=None`), including audit and monthly calls.
For runtime checkpoints construct `PostgresSaver` on that pooled connection with a
dictionary row factory; do not use the library connection helper's prepared default.

## Secrets: names only in Terraform

SSM requires a non-empty value, so it cannot create an empty SecureString. Instead
Terraform outputs these exact names, and grants only the app parameter ARN:

- `/ledgerguard/app-database-url`: pooled app-role URL, read by the API.
- `/ledgerguard/direct-database-url`: direct owner URL, never readable by the API.

If `name` changes, use the corresponding output names. Create each Standard-tier
SecureString privately using AWS CLI `ssm put-parameter` with `--type SecureString`.
Use a protected local file (`--value file://PATH`) or a secure interactive process;
do not put URL literals in shell commands/history, logs, Terraform variables or
chat. Remove temporary files securely afterward. Terraform never creates, reads,
imports or manages parameter values, so real secrets never enter state. The default
SSM-managed KMS key is used; no paid customer-managed key is provisioned.

The API Lambda environment contains only `LEDGERGUARD_ENV=aws` and the app parameter
name. It retrieves/decrypts the value with its scoped role, caches it only in the
warm process, and never logs it. Redeploy/restart warm functions after rotation.
The direct URL remains for owner-operated migrations, not the review API.

## Build, bootstrap and plan

`make api-zip` exports the locked runtime
requirements, installs Linux x86_64/Python 3.12 binary wheels with hashes, and writes
`dist/api.zip`. No development packages, PDFs, secrets or environment files are
packaged. Both API and web ZIPs must exist before a real Terraform plan. The builder checks
the 250 MiB uncompressed Lambda package limit.

Deployment uploads the ZIP to a private artifacts bucket in the Lambda region,
rather than using Lambda's size-limited direct upload. The bucket is versioned,
AES256-encrypted, blocks all public access, requires TLS, and expires noncurrent
versions after 30 days. The current object is retained; incomplete multipart
uploads are removed after one day. `aws_s3_object.api` uses `filemd5` as its
`source_hash`, and Lambda references its bucket, key and exact object version.
A changed ZIP uploads a new version and updates Lambda during an approved apply.
No S3 upload or infrastructure operation runs during CI's build/validation.

`make terraform-fmt` checks formatting. `make terraform-validate` initializes each
module with `-backend=false -lockfile=readonly` and validates without credentials or
cloud reads. CI runs those commands and builds the ZIP but never deploys.

Bootstrap `infra/bootstrap` once: choose a unique state bucket name, initialize,
and produce a saved plan. Review its S3-only plan summary and obtain explicit
bootstrap approval before applying. It creates a private, TLS-only, versioned,
AES256-encrypted bucket. Preserve its local bootstrap state privately.

Copy `backend.hcl.example` to ignored `backend.hcl`, supply the bucket/region, then
initialize this module with `terraform init -backend-config=backend.hcl`. The backend
uses encryption, versioned S3 objects and native `use_lockfile` locking; no DynamoDB
lock table is needed. Supply alert email and non-secret configuration privately,
then produce a saved `terraform plan -out=tfplan`. Show its resource/cost/security
summary. The 8b web changes require the exact approval `apply 8b` before that saved plan is applied.
Never apply a newly changed plan under an older approval.

## Identity and permissions

Cognito self sign-up is disabled. You create your own user, verify its email and
choose its password. The public client supports authorization code flow. The
prepared 8b Terraform derives callback and logout URLs from the web HTTP API;
the old callback/logout variables are removed. The web app now implements server-side Cognito sign-in with PKCE and the approved
`aws-jwt-verify 5.2.1` dependency. Deployment and a signed-in review still require
the owner's plan approval and interactive sign-in.

The web Lambda uses Node.js 24, a Lambda Web Adapter layer, and a role with only
log-write permissions. Its separate HTTP API forwards requests without an
authorizer: the web app must authenticate requests itself. The API's JWT
authorizer remains in place. There is no web Function URL. The layer is pinned
to us-east-1; verify availability before using another region.

Protected API paths use the Cognito JWT authorizer. This design uses the **ID
token**, whose audience is the configured client and whose verified email identifies
the reviewer; Cognito access tokens normally lack the email claim. The app checks
`token_use=id`, verified email and a non-empty email exclusively in
`scope['aws.event']['requestContext']['authorizer']['jwt']['claims']`. Client headers
and body cannot set identity. Local token/reviewer shortcuts cause AWS startup to
fail even when set to empty strings. Local mode remains the default and unchanged.
Only `/health` is unauthenticated. ANY method forwarding on the five known paths
preserves FastAPI's method rejection; it does not add application endpoints.

The API role can read only the pooled app parameter and write its own logs. It has
no Bedrock grant. A separate policy scopes the two current inference
profiles and underlying model IDs, and is attached only to the 8c pipeline role. No wildcard model IDs
are granted. The API timeout is 30 seconds to match HTTP API's synchronous limit;
the 198-second pipeline belongs in the separate asynchronous 8c function.
CloudWatch API/Lambda log groups retain records for 14 days and access logs omit
JWTs, claims, query strings and bodies. Concurrency and route throttles limit burst
load. A fixed $10 monthly account cost budget alerts at 80% and 100%; alerts do not
stop spending, and neither an alert nor a free tier guarantees a zero AWS bill.

## Teardown

Teardown also requires explicit named approval after reviewing a destroy plan.
For the main module, first plan disabling `protect_user_pool`, review and approve
that change, then apply it. Produce `terraform plan -destroy -out=tfplan-destroy`,
review it, and obtain `destroy 8a` approval before `terraform apply tfplan-destroy`.
The equivalent `terraform destroy` must never run without that approval.

The artifacts bucket has `force_destroy=false`. Before an approved teardown,
review and explicitly authorize removal of its synthetic ZIP objects and all
versions/delete markers; otherwise bucket deletion will refuse to proceed. The
30-day lifecycle does not remove the current version automatically. The state
bucket remains separate and protected.

SSM parameters and Neon are owner-managed and are not removed by Terraform.
Delete their synthetic data/project and parameters yourself only when no longer
needed. The bootstrap state bucket is protected from destruction and may retain
versioned state; do not remove it while any stack still uses it. Removing that
protection and deleting versions/bucket requires a separate approved teardown.

## Step 8c: S3 pipeline runner (built; deployed smoke not verified)

The uploader and separate asynchronous Lambda are built and covered by offline
and local Postgres tests. They are not verified against a deployed S3-to-Lambda-to-
Neon run until an explicitly authorized live smoke run succeeds. No code or test
uploads a real batch or invokes Bedrock automatically. Applying infrastructure,
uploading a trigger manifest, and the live smoke run require specific approval.
The API role retains no Bedrock permission and the web Lambda is unchanged.

`make api-zip` packages both handlers in the same versioned artifact. The pipeline
handler is `ledgerguard.pipeline_lambda.handler`. The builder stamps
`ledgerguard/build_revision.txt` with `git rev-parse HEAD`, plus `-dirty` when the
worktree has changes; live run metadata preserves that revision. Build artifacts
must be regenerated after source changes and before reviewing a deployment plan.

After deployment is approved, the `pipeline_bucket` output identifies a separate
private bucket. To submit synthetic dev PDFs, use:

```sh
uv run python -m ledgerguard.pipeline_upload --data data/synthetic-v2 --split dev --limit 3 --bucket BUCKET
```

Replace `BUCKET` with that output. This command is the spending trigger: it writes
only selected dev PDFs and the explicit `policy.md`, `contracts.json`, `history.json`
reference allowlist. It never uploads invoice JSON, answer keys or held-out test
PDFs. It validates all inputs before uploading and publishes the manifest last,
then prints the new UUID batch ID. It refuses the test split. A failed upload does
not publish its manifest; retry with a fresh batch ID rather than reusing objects.

Bucket layout:

```text
batches/<batch_id>/manifest.json
batches/<batch_id>/invoices/<document_id>.pdf
batches/<batch_id>/reference/{policy.md,contracts.json,history.json}
claims/<batch_id>.json
results/<batch_id>.json
```

The bucket blocks public access, enforces owner ownership, AES256 encryption and
TLS, and expires all objects after 14 days. The notification matches only
`batches/` plus `/manifest.json`; the handler additionally checks the exact UUID
key and bucket and requires exactly one S3 record. It rejects extra manifest
fields, duplicate/unsafe document IDs, more than 40 documents, PDFs over 5 MiB,
non-PDF bytes and SHA-256 mismatches. Manifests are capped at 64 KiB and each
reference file at 5 MiB to bound untrusted input memory.

After PDF validation, an atomic `IfNoneMatch='*'` claim prevents duplicate events
from starting another paid run. An existing claim or a conditional-write conflict
returns without running a model. Claims are never reused. A failed batch needs a
new ID. Results also use conditional writes so a later event cannot overwrite the
first outcome. Invalid events without a trusted bucket/key have no safe result
address; they fail without writing an object. Addressable batch failures record
only `status=failed` and the exception class. The Lambda boundary rethrows a
sanitized `PipelineFailure` with that class name, preventing the runtime from
logging the original exception message. No document text, model output or
connection value is logged.

The runner loads references only after claiming, then uses the existing gateway,
Postgres audit sink and monthly ledger. Checkpoints use an app-role pooled
connection with prepared statements disabled and strict serialization; runtime
code never sets up checkpoints or migrates schema. Saving checks the app role
again, persists completed, stopped and partial results with `mode=live` and every
submitted PDF hash, and writes summary counts, cost, status and run ID to S3. The
summary's `stopped_reason` is the gateway's stop code. A saved run appears through
the existing review API; the runner never makes a review decision.

`pipeline_batch_max_cost` is a server-controlled USD string, default `"0.50"`.
Terraform and the handler permit lowering it to zero but not raising the existing
$0.50 ceiling. Manifest values cannot change it. The per-invoice 20-call/$0.50
and persistent $10/month gateway caps remain unchanged. Shared batch call/tool
limits are document count times 20/10. Bedrock uses us-east-1, one SDK attempt,
a 5-second connection timeout and a 60-second read timeout. S3 SDK operations
also have only one attempt.

The pipeline Lambda uses Python 3.12/x86_64, 1024 MiB, timeout 900 seconds and
reserved concurrency 1, with no Function URL. Asynchronous function-error retries
are zero and maximum event age is 3600 seconds. Duplicate delivery is still
possible and is handled by the claim. Its role can write only its own 14-day logs,
read only the app SSM parameter and `batches/*`, and write only `claims/*` and
`results/*`. It cannot write a trigger batch or read the direct owner parameter. The handler
never appends review decisions. The S3 invocation permission is scoped to the bucket ARN
and account. Infrastructure preparation and validation do not run the pipeline.
