from enum import Enum, IntFlag


class GMTVersion(IntFlag):
    KENZAN = 0x10001
    YAKUZA3 = 0x20000
    YAKUZA5 = 0x20001
    ISHIN = 0x20002
    DE2 = 0x20003    


class GMTVectorVersion(Enum):
    NO_VECTOR = 0
    OLD_VECTOR = 1
    DRAGON_VECTOR = 2

    @classmethod
    def from_GMTVersion(cls, version: GMTVersion):
        if version >= GMTVersion.ISHIN:
            # This can be OLD_VECTOR as well, but that can't be determined with the version alone
            return GMTVectorVersion.DRAGON_VECTOR

        return GMTVectorVersion.NO_VECTOR


# Using IntFlag to support undocumented formats (patterns etc)
class GMTCurveType(IntFlag):
    ROTATION = 0
    LOCATION = 1
    PATTERN_HAND = 4
    PATTERN_UNK = 5
    PATTERN_FACE = 6


class GMTCurveChannel(IntFlag):
    """X, Y, Z, and ALL are for LOCATION

    XW, YW, ZW, and ALL are for ROTATION

    LEFT_HAND, RIGHT_HAND and UNK_HAND are for PATTERN_HAND

    PATTERN_FACE has unknown values ranging from 0 to more than 5
    """

    ALL = LEFT_HAND = 0
    X = XW = RIGHT_HAND = 1
    Y = YW = UNK_HAND = 2
    ZW = 3
    Z = 4
    FACE = 2


class GMTCurveFormat(IntFlag):
    ROT_QUAT_XYZ_FLOAT = 1

    # Half float before version 0x20000, scaled shorts everywhere else
    ROT_XYZW_SHORT = 2

    LOC_CHANNEL = 4
    LOC_XYZ = 6

    ROT_XW_FLOAT = 0x10
    ROT_YW_FLOAT = 0x11
    ROT_ZW_FLOAT = 0x12

    # Half float before version 0x20000, scaled shorts everywhere else
    ROT_XW_SHORT = 0x13
    ROT_YW_SHORT = 0x14
    ROT_ZW_SHORT = 0x15

    PATTERN_HAND = 0x1C
    PATTERN_UNK = 0x1D

    ROT_QUAT_XYZ_INT = 0x1E


class OEDEFaceTarget(IntFlag):
    a_l = 0
    a_r = 1
    i_l = 2
    i_r = 3
    u_l = 4
    u_r = 5
    e_l = 6
    e_r = 7
    o_l = 8
    o_r = 9
    open_a_l = 10
    open_a_r = 11
    open_i_l = 12
    open_i_r = 13
    open_u_l = 14
    open_u_r = 15
    open_e_l = 16
    open_e_r = 17
    open_o_l = 18
    open_o_r = 19
    eye_anger_l = 20
    eye_anger_r = 21
    eye_pain_l = 22
    eye_pain_r = 23
    eye_sad_l = 24
    eye_sad_r = 25
    eye_sup_l = 26
    eye_sup_r = 27
    eye_open_l = 28
    eye_open_r = 29
    eye_close_l = 30
    eye_close_r = 31
    eye_up_l = 32
    eye_up_r = 33
    eye_down_l = 34
    eye_down_r = 35
    eye_left_l = 36
    eye_left_r = 37
    eye_right_l = 38
    eye_right_r = 39
    joy_l = 40
    jaw_down = 41
    jaw_left = 42
    jaw_right = 43
    jaw_front = 44
    joy_r = 45
    lip_up = 46
    lip_down = 47
    lip_left = 48
    lip_right = 49
    lip_bp = 50
    lip_gi = 51
    lip_woo = 52
    lipside_up_l = 53
    lipside_up_r = 54
    lipside_down_l = 55
    lipside_down_r = 56
    lipside_center_l = 57
    lipside_center_r = 58
    lipside_wide_l = 59
    lipside_wide_r = 60
    lip_gin = 61
    liptwist_left = 62
    liptwist_right = 63
