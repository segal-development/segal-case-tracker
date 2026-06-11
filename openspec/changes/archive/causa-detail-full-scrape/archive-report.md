# Archive Report: causa-detail-full-scrape

**Date**: 2026-06-11  
**Change**: causa-detail-full-scrape  
**Status**: ARCHIVED — READY FOR CLOSURE  
**Branch**: feat/causa-detail-full-scrape (tracker, 3 slices merged)

---

## Executive Summary

The `causa-detail-full-scrape` change has been fully implemented, verified, and is now archived. All 15 implementation tasks completed successfully. Verification passed with 0 CRITICAL issues (546 tests, 100% pass rate). The change introduces parsing, storage, and change-detection for four additional PJUD civil case-detail tabs (Litigantes, Notificaciones, Escritos, Exhortos) with comprehensive notifications and webhook dispatch. Spec warnings have been corrected before archival.

---

## Slices Delivered

### Slice 1 — Parsing + Models + Migration + Scoping Fix (12 tasks)
**Status**: DELIVERED  
**Commits**: 0e0a0f3, 9e10f44, bd8b316, bb29f3b, 0afd12e, a2cfa42  

- Tab-scoped HTML parsers for all 4 new entity types (litigantes, notificaciones, escritos, exhortos)
- 4 new SQLAlchemy models with natural-key dedup spine
- 1 Alembic migration (4 tables + Alert entity columns)
- Scraper dataclass converters + EntitySyncSpec generic engine
- Storage-only sync (creates_alert=False, notify=False for all specs)
- Regression test: movements parser re-scoped to #historiaCiv to prevent cross-tab row bleeding
- HTML fixtures: real (C-1253-2015) + synthetic (with notif/escrito rows for change-detection)

### Slice 2 — Change Detection + Notifications (3 tasks)
**Status**: DELIVERED  
**Commits**: 6d037d0, abf0557  

- Alert entity-type/entity-id wiring (polymorphic target per ADR-003)
- NotificationService methods: notify_new_notificacion, notify_new_escrito, notify_new_exhorto
- Shared notification budget across all entity types (NOTIFY_MAX_PER_SYNC)
- Webhook event types: notificacion.created, escrito.created, exhorto.created
- Litigantes intentionally silent (no alerts, no notifications) per ADR-004

### Slice 3 — Integration + Verification (documentation refinement)
**Status**: DELIVERED  

- Spec corrections: clarified litigante storage-only behavior + fixed cap scenario example
- 546 unit tests passing (pytest -m "not integration")
- mypy validation clean (0 type errors)
- Alembic migrations at head (004 + 005 applied)

---

## Specification Corrections Applied (Before Archive)

### Warning 1: Stale Change Detection Table (CORRECTED)
**Location**: openspec/changes/causa-detail-full-scrape/specs/case-detail-entities/spec.md, line 103–112

**Original Issue**: 
The Change Detection requirement table listed `CaseLitigante → new_litigante → notify_new_litigante`, contradicting ADR-004 which mandates litigantes are store-only (no alerts, no notifications).

**Correction**:
Updated the table to clarify:
- CaseLitigante: (stored only, no alert) | (not invoked)
- Other entities unchanged: notificacion/escrito/exhorto create alerts + dispatch notifications

### Warning 2: Stale Cap Scenario Example (CORRECTED)
**Location**: openspec/changes/causa-detail-full-scrape/specs/case-detail-entities/spec.md, line 133–138

**Original Issue**:
The "Cap spans mixed entity types" scenario stated: "2 new movements + 2 new litigantes → 3 dispatches". Since litigantes are silent, this outcome is impossible.

**Correction**:
Updated example to use notificaciones instead: "2 new movements + 2 new notificaciones, cap=3 → 3 dispatches" (correctly shows cap limiting the 4th entity to 0 dispatches).

---

## Delta Specs Merged to Main

### 1. New Capability: case-detail-entities
**Source**: openspec/changes/causa-detail-full-scrape/specs/case-detail-entities/spec.md  
**Destination**: openspec/specs/case-detail-entities/spec.md (CREATED)  

**Contents**: Full specification for parsing, storage, and change-detection of Litigantes, Notificaciones, Escritos, Exhortos tabs from the PJUD civil case-detail modal.

**Requirements**: 8 major + supporting scenarios covering:
- Tab-scoped parsing (4 parsers, each scoped to a pane id)
- Litigante, Exhorto, Notificacion, Escrito parsing with row count + field validation
- Natural-key idempotency (4 entity-specific keys to prevent duplicates)
- Change detection (alerts + notifications per entity type, except litigantes)
- Notification cap across all entity types (NOTIFY_MAX_PER_SYNC)

### 2. Modified Capability: pjud-civil (Delta merged)
**Source**: openspec/changes/causa-detail-full-scrape/specs/pjud-civil/spec.md  
**Destination**: openspec/specs/pjud-civil/spec.md (UPDATED)  

**Changes Merged**:
- **Movements Parser Scoped to #historiaCiv** (NEW requirement): prevents cross-tab row bleeding
- **All Five Tabs Populated from Single Detail HTML** (NEW requirement): _parse_case_detail_html populates all 5 entity lists from the single detail response

**Existing requirements preserved**: Civil Scraper Architecture, Civil YAML Selectors, Civil Resilience Integration.

---

## Verification Results

**Verdict**: PASS WITH WARNINGS — READY TO ARCHIVE

| Gate | Result |
|---|---|
| pytest -m "not integration" -q | 546 passed, 1 deselected, 1 xfailed (0 failures) |
| mypy app/core | Success: no issues found in 4 source files |
| alembic upgrade head | Clean — migrations 004 + 005 applied |
| CRITICAL issues | 0 |
| WARNING items | 0 (spec warnings corrected before archive) |

### Test Coverage Summary
- **Slice 1 parsing tests**: 26 tests (tab-scoped parsers, empty-pane handling, field validation)
- **Idempotency tests**: 5 tests (natural-key dedup, zero re-alerts on unchanged data)
- **Slice 2 alert wiring**: 5 tests (entity_type/entity_id polymorphism, litigantes silent)
- **Notification dispatch**: 4 tests (email + webhook, event strings, HMAC signing)
- **Budget cap**: 1 test (shared cap across all entity types, alerts vs. dispatches)

### Residual Risks (Known & Accepted)
1. **Notificaciones/Escritos parser validation**: Proven on synthetic fixtures only. TODO(validate-real-rich-case) markers present. Requires live PJUD case validation before full production trust. **Status**: Accepted, documented, gate in place for Slice 2.
2. **Manual integration smoke test**: Full sync against test case C-1253-2015 requires live PJUD session. Cannot be verified in CI. **Status**: Known integration limitation, flag for manual QA before release.
3. **Python 3.13 deprecation warnings**: datetime.utcnow() deprecated, 548 warnings in test suite. Not a failure; scheduled for cleanup before Python 3.14. **Status**: Tracked, not blocking.

---

## Task Completion Checklist

### Slice 1 (12 tasks)
- [x] S1-T01: HTML test fixture files (real + synthetic)
- [x] S1-T02: base.py dataclasses + PJUDCaseDetail extension
- [x] S1-T03: Scoping regression test + _extract_pane_tbody + movements re-scope
- [x] S1-T04: Litigante parser
- [x] S1-T05: Exhorto parser
- [x] S1-T06: Notificacion + Escrito parsers
- [x] S1-T07: _parse_case_detail_html extension (all 5 lists)
- [x] S1-T08: SQLAlchemy models (4 entity tables + Alert columns + Case back-refs)
- [x] S1-T09: Single Alembic migration
- [x] S1-T10: Scraped DTOs + converters + natural_key functions
- [x] S1-T11: EntitySyncSpec + _sync_entities engine (storage-only)
- [x] S1-T12: Extend detect_and_sync_movements with 4 entity sync calls
- [x] S1-VER: Slice 1 verification gate (storage layer fully functional)

### Slice 2 (3 tasks)
- [x] S2-T01: Flip spec flags + Alert entity wiring
- [x] S2-T02: NotificationService new methods + webhook events
- [x] S2-T03: Notify dispatch wiring + shared budget refactor
- [x] S2-VER: Slice 2 verification gate (change detection + notifications functional)

---

## Architectural Decisions (ADRs) Honored

| ADR | Decision | Status |
|---|---|---|
| ADR-001 | Generic _sync_entities engine vs. 4 copy-paste methods | HONORED: single configurable engine with per-entity specs |
| ADR-002 | Single natural_key column per table for uniform dedup | HONORED: VARCHAR(64) + UNIQUE(case_id, natural_key) on all 4 tables |
| ADR-003 | Polymorphic Alert (entity_type + entity_id) vs. 4 nullable FKs | HONORED: fixed-width polymorphic pair, schema-agnostic to future entity types |
| ADR-004 | Litigantes store-only, no alerts/notifications | HONORED: creates_alert=False, notify=False; test_litigante_creates_no_alert confirms |
| ADR-005 | Shared notification budget across all entity types | HONORED: NotifyBudget class, single budget threaded through all 5 sync calls, priority-order drain |

---

## Archive Metadata

**Artifacts Included**:
- proposal.md: Intent, scope, approach, affected areas, risks, rollback plan
- specs/case-detail-entities/spec.md: Full spec for new capability (CORRECTED)
- specs/pjud-civil/spec.md: Delta spec merged to main (UPDATED)
- design.md: Architecture, component map, data flow, ADRs, test strategy, slice plan
- tasks.md: 15-task checklist (all complete)
- (Source specs merged to main: openspec/specs/case-detail-entities/, openspec/specs/pjud-civil/)

**Engram Observation IDs** (for traceability):
- Proposal: #517
- Spec: #518
- Design: #519
- Tasks: #520
- Verify Report: #525

**Branch**: feat/causa-detail-full-scrape (tracker branch, 3 slices integrated)

---

## Follow-Up Items

### Accepted (GitHub Issue #21)
- [ ] Validate notificaciones/escritos parsers against a real PJUD case with populated rows before shipping Slice 2 to production
- [ ] Manual integration smoke test: run full sync against test case C-1253-2015; verify 1 exhorto alert, 0 duplicate on re-run, 0 litigante alerts

### Out of Scope (Phase 2 / Future)
- [ ] Multi-cuaderno movement scraping (requires separate historiaCausaCuaderno(token) AJAX calls)
- [ ] AJAX-loaded secondary content (anexoCausaCivil, receptorCivil, detalleExhortosCivil JWT tokens)
- [ ] Laboral and penal competencias (civil-only in this change)

---

## SDD Cycle Complete

The change is now closed in the artifact store:
- **Planning** ✓ (proposal, spec, design)
- **Implementation** ✓ (15 tasks, 3 slices, strict TDD)
- **Verification** ✓ (546 tests, 0 CRITICAL, mypy clean)
- **Archival** ✓ (specs synced to main, archive report generated)

Ready for orchestrator to commit, merge, and prepare the next change.
