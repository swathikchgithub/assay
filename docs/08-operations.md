# 8 · Operations

## Permissions

Assay reads. Give it a role that can do nothing else — the adapter's
`SELECT`-only guard is defence in depth, not the primary control.

```sql
CREATE ROLE IF NOT EXISTS ASSAY_RO;
GRANT USAGE  ON WAREHOUSE ANALYTICS_WH        TO ROLE ASSAY_RO;
GRANT USAGE  ON DATABASE  ANALYTICS           TO ROLE ASSAY_RO;
GRANT USAGE  ON ALL SCHEMAS IN DATABASE ANALYTICS TO ROLE ASSAY_RO;
GRANT SELECT ON ALL TABLES  IN DATABASE ANALYTICS TO ROLE ASSAY_RO;
GRANT SELECT ON ALL VIEWS   IN DATABASE ANALYTICS TO ROLE ASSAY_RO;
GRANT USAGE  ON FUTURE SCHEMAS IN DATABASE ANALYTICS TO ROLE ASSAY_RO;
GRANT SELECT ON FUTURE TABLES  IN DATABASE ANALYTICS TO ROLE ASSAY_RO;
GRANT SELECT ON FUTURE VIEWS   IN DATABASE ANALYTICS TO ROLE ASSAY_RO;
GRANT ROLE ASSAY_RO TO USER assay_reader;
```

Verified: the full suite runs on these grants alone, and the report is
byte-identical to one produced under a privileged role. `assay doctor` warns
when it finds itself connected as `ACCOUNTADMIN` or another privileged built-in.

## Authentication

Assay never handles passwords. `SNOWFLAKE_PASSWORD` is refused with a message
pointing at the alternatives.

**SSO** — the default. Nothing to configure beyond the account having a SAML
provider. If it does not, this fails with `390190`.

**Key pair** — the reliable path, and exempt from interactive MFA:

```bash
mkdir -p ~/.snowflake && chmod 700 ~/.snowflake && openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out ~/.snowflake/assay_key.p8 -nocrypt && chmod 600 ~/.snowflake/assay_key.p8
```

```bash
openssl rsa -in ~/.snowflake/assay_key.p8 -pubout | grep -v '^-----' | tr -d '\n'
```

```sql
ALTER USER assay_reader SET RSA_PUBLIC_KEY='<paste>';
```

**Check whether a key is already registered before running that** — `SET
RSA_PUBLIC_KEY` overwrites, which breaks whatever else uses it. `DESC USER`
shows `RSA_PUBLIC_KEY_FP` and `RSA_PUBLIC_KEY_2_FP`; Snowflake provides the
second slot for exactly this, and both are valid simultaneously.

To match a local key against a registered fingerprint without touching
Snowflake:

```bash
openssl rsa -in ~/.snowflake/assay_key.p8 -pubout -outform DER | openssl dgst -sha256 -binary | openssl base64
```

> **Known gap.** The adapter does not yet support an encrypted private key
> (`private_key_file_pwd`). `-nocrypt` at mode 600 is acceptable for a
> read-only role on a demo account; harden before production.

## Cost

Measured, not estimated. Storage is irrelevant; **idle warehouse time is
almost the entire bill.**

An X-Small warehouse bills 1 credit/hour with a **60-second minimum per
resume**, then per second. A full Assay run takes 7–9 seconds. What you pay for
is the idle period after it:

| `auto_suspend` | Credits/run | Nightly, per month |
|---|---|---|
| 600s (common default) | ~0.169 | ~5.1 |
| 300s | ~0.086 | ~2.6 |
| **60s** | **~0.019** | **~0.6** |

```sql
ALTER WAREHOUSE ANALYTICS_WH SET AUTO_SUSPEND = 60;
```

That one statement is worth more than every other optimisation here. The query
itself is ~3% of the cost.

For a hard ceiling rather than a trim:

```sql
CREATE RESOURCE MONITOR ASSAY_GUARD WITH CREDIT_QUOTA = 20 TRIGGERS ON 80 PERCENT DO NOTIFY ON 100 PERCENT DO SUSPEND;
```

```sql
ALTER WAREHOUSE ANALYTICS_WH SET RESOURCE_MONITOR = ASSAY_GUARD;
```

**Scans, not rows, drive cost.** A Snowflake round trip is ~0.37s, so a suite of
31 checks that memoises to 24 scans is meaningfully cheaper than one that does
not. Adding metrics adds scans roughly linearly; adding *rows* barely moves it.

## Scheduling

Nightly, after the pipeline that populates the metrics. Running before it
produces freshness warnings that are true but useless.

Retain `.assay/history.db` between runs or TMP-03 never gets a baseline. In a
container, mount it; in CI, cache it.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| TLS hostname mismatch | Wrong `SNOWFLAKE_ACCOUNT`. Snowflake wildcards DNS, so a bad identifier resolves and lands on another deployment | `SELECT SYSTEM$ALLOWLIST()`, take the `SNOWFLAKE_DEPLOYMENT_REGIONLESS` host, drop `.snowflakecomputing.com`. `assay doctor` catches this before authenticating |
| `390190` SAML error | Account has no SAML IdP; `externalbrowser` cannot work | Use key-pair auth |
| Every object "not found" | Case policy wrong | `--case-policy exact` for quoted lower-case projects; doctor reports which |
| Report is all skips | Tables empty, or first run | Doctor's row-count check names empty tables; TMP-03 always skips on run one |
| TMP-03 fires on many metrics at once | A shared upstream table was rebuilt | Expected. Check which metrics moved together |
| TMP-02 never fires | Fewer than 7 closed periods | By design — it will not enforce against an uncalibrated tolerance |
| `command not found: pip` | `uv venv` does not install pip | `uv pip install --python .venv/bin/python ...` |

## Security posture

- Read-only twice over: a `SELECT`-only role, and a guard rejecting any
  statement that is not `SELECT`/`WITH`.
- Credentials from the environment only; never a CLI argument.
- Identifiers validated at contract load, quoted at SQL construction; window
  bounds are bound parameters.
- Identity expressions parsed to an AST with an operator allow-list — never
  `eval()`.
- History is local SQLite, so no write grant is ever needed on the warehouse.

**Known limitation.** The read-only guard also blocks `SHOW` and `DESCRIBE`,
which are reads. That is the guard being blunt rather than safe, and it is why
Assay cannot inspect `auto_suspend` itself despite that setting dominating its
own cost.
