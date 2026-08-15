# ingest/sao/ — Agent Guidelines

Pure extractors + orchestration service. No LLM.

## Extractors (`extractors.py`)

| Extractor | Input | Section |
|-----------|-------|---------|
| `constants` | `*.py` assignment lines | U3 |
| `pyproject` | `name`, `requires-python`, `testpaths` | U3/U4 |

## Service (`service.py`)

`SaOExtractService.extract_for_workspace` scans episodes per locked boundary component paths.

## Tests

- `tests/unit/test_sao_extractors.py`
- `tests/integration/test_sao_extract.py`
