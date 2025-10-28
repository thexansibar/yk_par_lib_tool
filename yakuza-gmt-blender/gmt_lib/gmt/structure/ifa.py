from typing import List, Tuple


class IFA:
    bone_list: List['IFABone']

    def __init__(self, bone_list=None):
        self.bone_list = list() if bone_list is None else bone_list


class IFABone:
    name: str
    parent_name: str

    location: Tuple[float]
    rotation: Tuple[float]

    def __init__(self, name, parent_name, location=None, rotation=None):
        self.name = name
        self.parent_name = parent_name
        self.location = location
        self.rotation = rotation
