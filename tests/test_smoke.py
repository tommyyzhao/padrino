"""Baseline smoke test. Ensures the package imports cleanly and CI is wired."""

from __future__ import annotations


def test_package_imports() -> None:
    import padrino

    assert padrino.__version__
    assert padrino.__version__ != "0.0.0+unknown"


def test_cli_version_callable() -> None:
    from padrino.cli import app

    assert app is not None
