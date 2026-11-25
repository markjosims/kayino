from pathlib import Path

from attrs import Factory, frozen

from kayino.models import KayinoError, NormalizationSpec, Registry
from kayino.structs import fetch_struct, init_registry


@frozen
class ReplState:
    registry: Registry = Factory(init_registry)
    texts: dict[str, str] = Factory(dict)
    active_spec: str | None = None
    # Project folder this session was loaded from / last saved to
    project_dir: Path | None = None


class NoActiveSpecError(KayinoError):
    pass


def get_active_spec(state: ReplState) -> NormalizationSpec | None:
    if spec_name := state.active_spec:
        return fetch_struct(
            struct_type_name=NormalizationSpec.__name__,
            field="name",
            value=spec_name,
            registry=state.registry,
        )
