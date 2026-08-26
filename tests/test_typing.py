"""Installed-package typing contract."""

from pathlib import Path

import ofw as package
from ofw import ofw


def test_package_declares_inline_types_and_namespace_methods() -> None:
    package_file = package.__file__
    assert package_file is not None
    assert Path(package_file).with_name("py.typed").is_file()
    assert callable(ofw.collect)
    assert callable(package.process_repository)
    assert callable(ofw.E2BSandbox)
    assert callable(ofw.ProcessLimits)
