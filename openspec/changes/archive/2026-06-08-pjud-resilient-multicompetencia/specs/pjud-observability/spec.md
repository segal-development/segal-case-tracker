# Observability Specification

## Purpose

Structured logging, metrics, and webhook alerts for production monitoring and incident response.

## Requirements

### Requirement: Structured JSON Logging

The system MUST emit structured JSON logs with context.

| Field | Required | Description |
|-------|----------|-------------|
| `timestamp` | Yes | ISO 8601 format |
| `level` | Yes | DEBUG, INFO, WARNING, ERROR |
| `user_rut` | When available | Anonymized user identifier |
| `competency` | When available | civil, laboral, penal |
| `operation` | Yes | login, get_cases, get_detail, download |
| `duration_ms` | Yes | Operation duration |
| `error` | On failure | Error type and message |

#### Scenario: Log successful scrape

- GIVEN a `get_my_cases()` call completes successfully
- WHEN the operation finishes
- THEN a JSON log MUST include `operation: "get_cases"`, `competency`, `duration_ms`, `case_count`

### Requirement: Prometheus Metrics

The system MUST expose metrics for monitoring dashboards.

| Metric | Type | Labels |
|--------|------|--------|
| `pjud_cases_scraped_total` | Counter | competency |
| `pjud_request_duration_seconds` | Histogram | competency, operation |
| `pjud_errors_total` | Counter | competency, error_type |
| `pjud_circuit_state` | Gauge | competency (0=closed, 1=open, 2=half-open) |

#### Scenario: Metric incremented on scrape

- GIVEN metrics endpoint is enabled
- WHEN 10 Civil cases are scraped
- THEN `pjud_cases_scraped_total{competency="civil"}` MUST increment by 10

### Requirement: Webhook Alerts

The system MUST fire webhook alerts for critical events.

| Event | Severity | Webhook Payload |
|-------|----------|-----------------|
| Circuit opened | CRITICAL | competency, failure_count, timestamp |
| Health check failed | WARNING | competency, error, timestamp |
| Structure change detected | WARNING | competency, hash_diff, timestamp |

#### Scenario: Webhook on circuit open

- GIVEN webhook URL is configured
- WHEN circuit opens for Civil competency
- THEN POST to webhook URL within 30 seconds
- AND payload MUST include `event: "circuit_open"`, `competency: "civil"`
