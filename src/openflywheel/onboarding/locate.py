"""Deterministic boundary scanner."""

from __future__ import annotations

from pathlib import Path

from openflywheel.contracts.boundary import BoundaryCandidate
from openflywheel.contracts.enums import SystemShape, TruthSection


def _has_pyproject(repo_path: Path) -> bool:
    return (repo_path / "pyproject.toml").is_file()


def _package_dirs(repo_path: Path) -> tuple[str, ...]:
    src = repo_path / "src"
    if not src.is_dir():
        return tuple()
    return tuple(p.name for p in src.iterdir() if p.is_dir())


def scan_fixture_root(fixture_root: Path) -> tuple[BoundaryCandidate, ...]:
    candidates: list[BoundaryCandidate] = []
    for repo_path in sorted(fixture_root.iterdir()):
        if not repo_path.is_dir():
            continue
        if not _has_pyproject(repo_path):
            continue
        packages = _package_dirs(repo_path)
        readme = repo_path / "README.md"
        rationale = "pyproject + src package"
        if readme.is_file():
            rationale = f"{rationale}; readme present"
        shape = SystemShape.MULTI_REPO if len(packages) > 0 else SystemShape.LIBRARY
        candidates.append(
            BoundaryCandidate(
                name=repo_path.name.replace("-", " ").title(),
                slug=repo_path.name,
                system_shape=shape,
                component_paths=(repo_path.name,),
                rationale=rationale,
                suggested_kpi_section=TruthSection.U3,
            )
        )
    return tuple(candidates)
