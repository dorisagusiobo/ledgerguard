# Step 8a: deployment preparation

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

The Lambda environment contains only `LEDGERGUARD_ENV=aws` and the app parameter
name. It retrieves/decrypts the value with its scoped role, caches it only in the
warm process, and never logs it. Redeploy/restart warm functions after rotation.
The direct URL remains for owner-operated migrations, not the review API.

## Build, bootstrap and plan

`make api-zip` exports the locked runtime
requirements, installs Linux x86_64/Python 3.12 binary wheels with hashes, and writes
`dist/api.zip`. No development packages, PDFs, secrets or environment files are
packaged. The API ZIP must exist before a real Terraform plan. The builder checks
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
summary. Only after the exact approval `apply 8a` may that saved plan be applied.
Never apply a newly changed plan under an older approval.

## Identity and permissions

Cognito self sign-up is disabled. You create your own user, verify its email and
choose its password. The public client supports authorization code flow; 8b will
add the server-side web sign-in and PKCE exchange. Until then, callbacks default
to localhost and no hosted screen is deployed.

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
no Bedrock grant. A separate unattached policy scopes the two current inference
profiles and underlying model IDs for the future 8c role. No wildcard model IDs
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
