from typing import Union

from .structure.br.br_cmt import *
from .structure.br.br_gmt import *
from .structure.br.br_ifa import *
from .structure.cmt import *
from .structure.gmt import *
from .structure.ifa import *
from .util import *


def read_gmt(file: Union[str, bytearray]) -> GMT:
    """Reads a GMT file and returns a GMT object.
    :param file: Path to file as a string, or bytes-like object containing the file
    :return: The GMT object
    """

    if isinstance(file, str):
        with open(file, 'rb') as f:
            file_bytes = f.read()
    else:
        file_bytes = file

    with BinaryReader(file_bytes) as br:
        br_gmt: BrGMT = br.read_struct(BrGMT)

    gmt = GMT(br_gmt.header.file_name.data, br_gmt.header.version)
    gmt.is_face_gmt = br_gmt.header.flags[0:2] == (0x7, 0x21)

    # Get bone names from groups
    bone_names: List[List[str]] = list(
        map(lambda x: br_gmt.strings[x.index: x.index + x.count], br_gmt.bone_groups))

    for br_anm in br_gmt.animations:
        br_anm: BrGMTAnimation

        anm = GMTAnimation(br_gmt.strings[br_anm.name_index].data, br_anm.frame_rate, br_anm.end_frame)
        anm_bone_names = bone_names[br_anm.bone_group_index]  # Get bone names for this animation

        for i, br_group in enumerate(br_gmt.curve_groups[br_anm.curve_groups_index: br_anm.curve_groups_index + br_anm.curve_groups_count]):
            bone = GMTBone(anm_bone_names[i].data)
            curves = list()

            for br_curve in br_gmt.curves[br_group.index: br_group.index + br_group.count]:
                br_curve: BrGMTCurve
                curve = GMTCurve(br_curve.type, br_curve.channel)
                curve.keyframes = list(map(lambda k, v: GMTKeyframe(k, v), br_curve.graph.values, br_curve.values))

                curves.append(curve)

            bone.curves = curves
            anm.bones[bone.name] = bone

        gmt.animation_list.append(anm)

    return gmt


def read_cmt(file: Union[str, bytearray]) -> CMT:
    """Reads a CMT file and returns a CMT object.
    :param file: Path to file as a string, or bytes-like object containing the file
    :return: The CMT object
    """

    if isinstance(file, str):
        with open(file, 'rb') as f:
            file_bytes = f.read()
    else:
        file_bytes = file

    with BinaryReader(file_bytes) as br:
        br_cmt: BrCMT = br.read_struct(BrCMT)

    cmt = CMT(br_cmt.header.version)

    for br_anm in br_cmt.animations:
        br_anm: BrCMTAnimation

        anm = CMTAnimation(br_anm.frame_rate)
        anm.frames = list(map(lambda x: CMTFrame(Vector(x.location), x.fov), br_anm.frames))

        if br_anm.frame_count:
            if isinstance(br_anm.frames[0], BrCMTFrameRotFloat):
                list(map(lambda x, y: x.from_dist_rotation(1.0, Quaternion(
                    (y.rotation[3],) + y.rotation[:3])), anm.frames, br_anm.frames))
            elif isinstance(br_anm.frames[0], BrCMTFrameDistRotShort):
                list(map(lambda x, y: x.from_dist_rotation(y.distance, Quaternion(
                    (y.rotation[3],) + y.rotation[:3])), anm.frames, br_anm.frames))
            elif isinstance(br_anm.frames[0], BrCMTFrameDistRotXYZ):
                list(map(lambda x, y: x.from_dist_rotation(y.distance, Quaternion(
                    (y.rotation[3],) + y.rotation[:3])), anm.frames, br_anm.frames))
            elif isinstance(br_anm.frames[0], BrCMTFrameFocRoll):
                for frame, br_frame in zip(anm.frames, br_anm.frames):
                    frame.focus_point = Vector(br_frame.focus_point)
                    frame.roll = br_frame.roll

        if CMTFormat.CLIP_RANGE in br_anm.format:
            for frame, clip_range in zip(anm.frames, br_anm.clip_ranges):
                frame.clip_range = clip_range

        cmt.animation_list.append(anm)

    return cmt


def read_ifa(file: Union[str, bytearray]) -> IFA:
    """Reads an IFA file and returns an IFA object.
    :param file: Path to file as a string, or bytes-like object containing the file
    :return: The IFA object
    """

    if isinstance(file, str):
        with open(file, 'rb') as f:
            file_bytes = f.read()
    else:
        file_bytes = file

    with BinaryReader(file_bytes) as br:
        br_ifa: BrIFA = br.read_struct(BrIFA)

    return IFA(list(map(lambda x: IFABone(x.name.data, x.parent_name.data, x.location, x.rotation), br_ifa.bones)))
