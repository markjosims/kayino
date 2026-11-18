from collections import defaultdict

from attrs import Factory, define

from kayino.models import RegistryType


@define
class ReplState:
    registry: RegistryType = Factory(lambda: defaultdict(list))
    texts: dict[str, str] = Factory(dict)
