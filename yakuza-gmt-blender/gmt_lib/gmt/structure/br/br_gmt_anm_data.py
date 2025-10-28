import struct
from itertools import chain
from math import sqrt
from typing import List, Tuple

from ...util import *


# Common
def __write_float_tuples(br: BinaryReader, values: List[Tuple[float]]):
    br.write_float(list(chain(*values)))


def __write_half_float_tuples(br: BinaryReader, values: List[Tuple[float]]):
    br.write_half_float(list(chain(*values)))


def __write_quat_scaled(br: BinaryReader, values: List[Tuple[float]]):
    br.write_int16(list(map(lambda x: int(x * 16_384), chain(*values))))


# LOC_XYZ
def read_loc_all(br: BinaryReader, count):
    return list(map(lambda _: br.read_float(3), range(count)))


def write_loc_all(br: BinaryReader, values: List[Tuple[float]]):
    __write_float_tuples(br, values)


# LOC_CHANNEL
def read_loc_channel(br: BinaryReader, count):
    return list(map(lambda _: br.read_float(1), range(count)))


def write_loc_channel(br: BinaryReader, values: List[Tuple[float]]):
    __write_float_tuples(br, values)


# ROT_QUAT_XYZ_FLOAT
def read_quat_xyz_float(br: BinaryReader, count):
    values = [None] * count

    for i in range(count):
        xyz = br.read_float(3)
        w = 1.0 - sum(map(lambda a: a ** 2, xyz))
        values[i] = (*xyz, (sqrt(w) if w > 0 else 0))

    return values


# ROT_XYZW_SHORT (KENZAN)
def read_quat_half_float(br: BinaryReader, count):
    return list(map(lambda _: br.read_half_float(4), range(count)))


def write_quat_half_float(br: BinaryReader, values: List[Tuple[float]]):
    __write_half_float_tuples(br, values)


# ROT_XYZW_SHORT
def read_quat_scaled(br: BinaryReader, count):
    return list(map(lambda _: tuple([(x / 16_384) for x in br.read_int16(4)]), range(count)))


def write_quat_scaled(br: BinaryReader, values: List[Tuple[float]]):
    __write_quat_scaled(br, values)


# ROT_XW_FLOAT
def read_quat_channel_float(br: BinaryReader, count):
    return list(map(lambda _: br.read_float(2), range(count)))


# ROT_XW_SHORT (KENZAN)
def read_quat_channel_half_float(br: BinaryReader, count):
    return list(map(lambda _: br.read_half_float(2), range(count)))


def write_quat_channel_half_float(br: BinaryReader, values: List[Tuple[float]]):
    __write_half_float_tuples(br, values)


# ROT_XW_SHORT
def read_quat_channel_scaled(br: BinaryReader, count):
    return list(map(lambda _: tuple([(x / 16_384) for x in br.read_int16(2)]), range(count)))


def write_quat_channel_scaled(br: BinaryReader, values: List[Tuple[float]]):
    __write_quat_scaled(br, values)


# ROT_QUAT_XYZ_INT
def read_quat_xyz_int(br: BinaryReader, count):
    base_quaternion = [(x / 32_768) for x in br.read_int16(4)]
    scale_quaternion = [(x / 32_768) for x in br.read_uint16(4)]

    values = [None] * count

    for i in range(count):
        f = br.read_uint32()
        axis_index = f & 3
        f = f >> 2

        indices = [0, 1, 2, 3]
        indices.pop(axis_index)

        v123 = (0x3FF00000, 0x000FFC00, 0x000003FF)
        m123 = struct.unpack(">fff", b'\x30\x80\x00\x00\x35\x80\x00\x00\x3A\x80\x00\x00')

        # A lengthy calculation taken straight out of decompiled code
        a123 = list(map(lambda v, m, l: (float(f & v) * m *
                                         scale_quaternion[l]) + base_quaternion[l], v123, m123, indices))
        a4 = 1.0 - sum(map(lambda a: a ** 2, a123))

        a123.insert(axis_index, sqrt(a4) if a4 > 0 else 0)
        values[i] = tuple(a123)

    return values


# PATTERN_HAND
def read_pattern_short(br: BinaryReader, count):
    return list(map(lambda _: br.read_int16(2), range(count)))


def write_pattern_short(br: BinaryReader, values: List[Tuple[float]]):
    br.write_int16(list(chain(*values)))


# PATTERN_UNK
def read_bytes(br: BinaryReader, count):
    return list(map(lambda _: br.read_int8(1), range(count)))


def write_bytes(br: BinaryReader, values: List[Tuple[float]]):
    br.write_int8(list(chain(*values)))
    br.align(4)
