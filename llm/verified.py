"""The `verified` flag on model output belongs to code, never to the model.

A field named `verified` on any Pydantic model means "checked by code". The gateway calls
`reset_verified` on every validated model output, so whatever a model wrote there is gone
before any rule reads it. This module knows no schema: it works on any Pydantic model, which
keeps `llm/` free of imports from `pipeline/`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

VERIFIED_FIELD = "verified"


def map_models(value: Any, change: Callable[[BaseModel], BaseModel]) -> Any:
    """`value` with `change` applied to every Pydantic model inside it, innermost first.

    Walks model fields, lists, tuples and dict values. Nothing is mutated: changed parts are
    copies, unchanged parts are reused.
    """
    if isinstance(value, BaseModel):
        updates = {}
        for name in type(value).model_fields:
            old = getattr(value, name)
            new = map_models(old, change)
            if new is not old:
                updates[name] = new
        return change(value.model_copy(update=updates) if updates else value)
    if isinstance(value, list | tuple):
        items = [map_models(item, change) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return type(value)(items)
    if isinstance(value, dict):
        items = {key: map_models(item, change) for key, item in value.items()}
        return items if any(items[key] is not value[key] for key in value) else value
    return value


def set_verified[M: BaseModel](model: M, verified: bool | None) -> M:
    """`model` with its `verified` field set; the same object when nothing changes."""
    if getattr(model, VERIFIED_FIELD) is verified:
        return model
    return model.model_copy(update={VERIFIED_FIELD: verified})


def _clear(model: BaseModel) -> BaseModel:
    if VERIFIED_FIELD not in type(model).model_fields:
        return model
    return set_verified(model, None)


def reset_verified[T](output: T) -> T:
    """A copy of a model output with every `verified` field, at any depth, set to None."""
    return map_models(output, _clear)
