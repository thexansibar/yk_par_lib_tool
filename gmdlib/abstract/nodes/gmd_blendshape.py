from dataclasses import dataclass

from .gmd_node import GMDNode


@dataclass(init=False, repr=False)
class GMDBlendShape(GMDNode):
    pass
