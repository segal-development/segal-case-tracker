# Resilience Layer Specification

## Purpose

Circuit breaker, retry with backoff, health checks, and rate limiting for production-grade PJUD scraping.

## Requirements

### Requirement: Circuit Breaker

The system MUST implement circuit breaker pattern to prevent cascade failures.

| Config | Default | Description |
|--------|---------|-------------|
| `failure_threshold` | 5 | Failures to open circuit |
| `failure_window` | 60s | Window for counting failures |
| `recovery_timeout` | 30s | Time before half-open |

#### Scenario: Circuit opens after threshold

- GIVEN circuit is CLOSED
- WHEN 5 failures occur within 60 seconds
- THEN circuit MUST transition to OPEN
- AND subsequent requests MUST fail immediately with `CircuitOpenError`

#### Scenario: Circuit recovers via half-open

- GIVEN circuit is OPEN for 30 seconds
- WHEN a new request arrives
- THEN circuit MUST transition to HALF-OPEN
- AND allow exactly 1 probe request

### Requirement: Retry with Exponential Backoff

The system MUST retry transient failures with configurable backoff.

| Config | Default | Description |
|--------|---------|-------------|
| `max_retries` | 3 | Maximum retry attempts |
| `base_delay` | 1s | Initial delay |
| `max_delay` | 30s | Maximum delay cap |
| `backoff_factor` | 2 | Multiplier per retry |

#### Scenario: Retry sequence on transient failure

- GIVEN a request fails with `TimeoutError`
- WHEN retry policy is active
- THEN delays MUST be 1s, 2s, 4s before giving up
- AND total attempts MUST be 4 (1 initial + 3 retries)

### Requirement: Health Check

The system MUST perform periodic health checks per competency.

#### Scenario: Health check detects PJUD down

- GIVEN health check runs every 5 minutes
- WHEN PJUD returns 5xx or timeout
- THEN competency health MUST be marked DEGRADED
- AND an alert webhook MUST fire

### Requirement: Rate Limiter

The system MUST enforce request rate limits across all users.

#### Scenario: Rate limit exceeded

- GIVEN limit is 10 req/sec
- WHEN 11th request arrives in same second
- THEN request MUST be delayed until next window
- AND `RateLimitExceeded` metric MUST increment
