# book/ — Agent Guidelines

Verification, coverage, pins. Human-only claim activation.

## Modules

- `ontology.py` — generic U1–U7 slot templates per `SystemShape`
- `coverage.py` — seed requirements, section/org macro-average reports
- `verify.py` / `verify_uow.py` — atomic promote/reject/tension transactions
- `pin.py` — immutable claim-id snapshots

## Edges

- `derived_from`, `in_tension_with`, `supersedes`
- Supersede closes `valid_to`; history never deleted
- Recency alone never decides truth

## CLI

- `ofw book verify`
- `ofw book pin`
- `ofw coverage`

## Tests

- `tests/integration/test_book_verify.py`
