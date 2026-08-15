"""Typed Typer option helpers isolating third-party inference gaps."""

from __future__ import annotations

from enum import Enum
from typing import TypeVar, cast

import typer

E = TypeVar("E", bound=Enum)


def required_str(*param_decls: str, help: str | None = None) -> str:
    return typer.Option(..., *param_decls, help=help)


def required_list(*param_decls: str, help: str | None = None) -> list[str]:
    return cast(list[str], typer.Option(..., *param_decls, help=help))


def optional_str(
    default: None = None,
    *param_decls: str,
    help: str | None = None,
) -> None:
    return typer.Option(default, *param_decls, help=help)


def optional_str_value(
    default: str | None = None,
    *param_decls: str,
    help: str | None = None,
) -> str | None:
    return typer.Option(default, *param_decls, help=help)


def flag_bool(
    default: bool = False,
    *param_decls: str,
    help: str | None = None,
) -> bool:
    return typer.Option(default, *param_decls, help=help)


def toggle_bool(
    default: bool = True,
    *param_decls: str,
) -> bool:
    return typer.Option(default, *param_decls)


def default_str(
    default: str,
    *param_decls: str,
    help: str | None = None,
) -> str:
    return typer.Option(default, *param_decls, help=help)


def default_int(
    default: int,
    *param_decls: str,
    help: str | None = None,
) -> int:
    return typer.Option(default, *param_decls, help=help)


def default_list(
    default: list[str],
    *param_decls: str,
    help: str | None = None,
) -> list[str]:
    return typer.Option(default, *param_decls, help=help)


def enum_default(default: E, *param_decls: str, help: str | None = None) -> E:
    return typer.Option(default, *param_decls, help=help)
