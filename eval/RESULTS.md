# Pipeline Doctor: evaluation

Model: `gemini-2.5-flash` · 25 failure cases (7 real failures from my pipelines, the rest realistic) · run 2026-11-16

| Metric | Agent (all MCP tools) | Baseline (log only) |
|---|---|---|
| Root-cause category correct (%) | 0.0 | 0.0 |
| Suggested fix correct (%) | 0.0 | 0.0 |
| Both correct (%) | 0.0 | 0.0 |
| Category correct on real failures (%) | 0.0 | 0.0 |
| Tool calls per case | 0.0 | 0.0 |
| Read the failed log first (%) | 0.0 | 0.0 |
| Planted secrets leaked | 0 | 0 |
| Latency p50 (s) | 0.0 | 0.0 |
| Latency p95 (s) | 0.0 | 0.0 |

## Per case

| Case | Origin | Expected | Got | Fix | Tools |
|---|---|---|---|---|---|
| aq-ipv6-unreachable | real | network | ❌ error | ❌ |  |
| aq-password-failed | real | auth_failure | ❌ error | ❌ |  |
| aq-secret-pasted-toml | real | secret_misconfigured | ❌ error | ❌ |  |
| aq-workflow-yaml-broken | real | workflow_config | ❌ error | ❌ |  |
| wp-databricks-quota | real | rate_limit_or_quota | ❌ error | ❌ |  |
| wp-gdelt-429 | real | rate_limit_or_quota | ❌ error | ❌ |  |
| wp-gemini-timeout | real | timeout | ❌ error | ❌ |  |
| wp-ingest-token-expired | realistic | auth_failure | ❌ error | ❌ |  |
| dep-module-not-found | realistic | dependency | ❌ error | ❌ |  |
| dep-version-conflict | realistic | dependency | ❌ error | ❌ |  |
| dep-python-version | realistic | dependency | ❌ error | ❌ |  |
| secret-missing-empty | realistic | secret_misconfigured | ❌ error | ❌ |  |
| workflow-bad-cron | realistic | workflow_config | ❌ error | ❌ |  |
| workflow-missing-permission | realistic | workflow_config | ❌ error | ❌ |  |
| timeout-job-hang | realistic | timeout | ❌ error | ❌ |  |
| code-keyerror-after-change | realistic | code_bug | ❌ error | ❌ |  |
| code-syntax-error | realistic | code_bug | ❌ error | ❌ |  |
| code-test-failure | realistic | code_bug | ❌ error | ❌ |  |
| data-file-missing | realistic | data_issue | ❌ error | ❌ |  |
| data-schema-changed-upstream | realistic | data_issue | ❌ error | ❌ |  |
| data-empty-input | realistic | data_issue | ❌ error | ❌ |  |
| upstream-503 | realistic | upstream_outage | ❌ error | ❌ |  |
| upstream-neon-suspended | realistic | rate_limit_or_quota | ❌ error | ❌ |  |
| network-dns | realistic | secret_misconfigured | ❌ error | ❌ |  |
| unknown-cancelled | realistic | upstream_outage | ❌ error | ❌ |  |
