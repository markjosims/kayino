from pathlib import Path

from attrs import Factory, frozen

from kayino.models import KayinoError, NormalizationSpec, Registry
from kayino.structs import connect, fetch


@frozen
class ReplState:
    # mutable: commands change it in place, atomically (see structs.change)
    db: Registry = Factory(connect)
    texts: dict[str, str] = Factory(dict)
    active_spec: str | None = None
    # Project folder this session was loaded from / last saved to
    project_dir: Path | None = None


class NoActiveSpecError(KayinoError):
    pass


def get_active_spec(state: ReplState) -> NormalizationSpec | None:
    if spec_name := state.active_spec:
        return fetch(state.db, NormalizationSpec.__name__, name=spec_name)
