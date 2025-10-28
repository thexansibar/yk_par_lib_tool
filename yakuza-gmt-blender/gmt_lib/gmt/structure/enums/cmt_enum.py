from .common_enum import FlagEnum


class CMTVersion(FlagEnum):
    KENZAN = 0x010001
    YAKUZA3 = 0x020000
    YAKUZA4 = 0x030000
    YAKUZA5 = 0x040000


class CMTFormat(FlagEnum):
    ROT_FLOAT = 0x00
    DIST_ROT_SHORT = 0x01
    DIST_ROT_XYZ = 0x02
    FOC_ROLL = 0x04
    CLIP_RANGE = 0x010000
