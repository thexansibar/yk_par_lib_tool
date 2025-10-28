from ...util import *
from ..cmt import CMT, CMTAnimation, CMTFrame
from ..enums.cmt_enum import *
from .br_gmt_anm_data import *


class BrCMT(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.header = br.read_struct(BrCMTHeader)
        self.animations = br.read_struct(BrCMTAnimation, self.header.animations_count)

    def __br_write__(self, br: BinaryReader, cmt: CMT):
        anm_data_br = BinaryReader(endianness=Endian.BIG)

        br_anm_list = list()
        for anm in cmt.animation_list:
            br_anm = BrCMTAnimation()
            anm_data_br.write_struct(br_anm, anm, cmt.version)
            br_anm_list.append(br_anm)

        br.set_endian(Endian.BIG)

        br.write_str_fixed('CMTP', 4)

        br.write_int8(-1)
        br.write_uint8(1)   # Endianness (big)
        br.write_uint16(0)  # Padding

        br.write_uint32(cmt.version.value)

        anm_count = len(cmt.animation_list)
        header_size = 0x20 + (0x10 * anm_count)

        # File size (with clip_range padding)
        br.write_uint32(header_size + anm_data_br.size())

        br.write_uint32(anm_count)
        br.pad(12)

        for anm in cmt.animation_list:
            br_anm = br_anm_list.pop(0)

            br.write_float(anm.frame_rate)
            br.write_uint32(len(anm.frames))
            br.write_uint32(header_size + br_anm.anm_data_offset)
            br.write_uint32(br_anm.anm_data_format)

        br.extend(anm_data_br.buffer())


class BrCMTHeader(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.magic = br.read_str(4)

        if self.magic != 'CMTP':
            raise Exception(f'Invalid magic: Expected CMTP, got {self.magic}')

        br.read_uint8()  # -1
        self.endianness = br.read_uint8() == 1

        br.set_endian(self.endianness)

        # Padding
        br.read_uint16()

        self.version = CMTVersion(br.read_uint32())

        # File size without padding
        self.data_size = br.read_uint32()

        self.animations_count = br.read_uint32()

        # Padding
        br.read_uint32(3)


class BrCMTAnimation(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.frame_rate = br.read_float()
        self.frame_count = br.read_uint32()
        self.animation_data_offset = br.read_uint32()
        self.format = CMTFormat(br.read_uint32())

        target_format = (self.format & 0xFFFF)
        if target_format == CMTFormat.ROT_FLOAT:
            br_frame_struct = BrCMTFrameRotFloat
        elif target_format == CMTFormat.DIST_ROT_SHORT:
            br_frame_struct = BrCMTFrameDistRotShort
        elif target_format == CMTFormat.DIST_ROT_XYZ:
            br_frame_struct = BrCMTFrameDistRotXYZ
        elif target_format == CMTFormat.FOC_ROLL:
            br_frame_struct = BrCMTFrameFocRoll
        else:
            raise Exception(f'Unexpected CMTFormat: {self.format}')

        with br.seek_to(self.animation_data_offset):
            self.frames = br.read_struct(br_frame_struct, self.frame_count)

            if CMTFormat.CLIP_RANGE in self.format:
                self.clip_ranges = list(map(lambda _: br.read_float(2), range(self.frame_count)))

    def __br_write__(self, br: 'BinaryReader', anm: CMTAnimation, version: CMTVersion):
        self.anm_data_offset = br.pos()

        if version == CMTVersion.KENZAN:
            self.anm_data_format = CMTFormat.ROT_FLOAT
            br_cmt_frame_cls = BrCMTFrameRotFloat
        elif version == CMTVersion.YAKUZA3:
            self.anm_data_format = CMTFormat.DIST_ROT_SHORT
            br_cmt_frame_cls = BrCMTFrameDistRotShort
        elif version == CMTVersion.YAKUZA4:
            self.anm_data_format = CMTFormat.DIST_ROT_XYZ
            br_cmt_frame_cls = BrCMTFrameDistRotXYZ
        else:
            self.anm_data_format = CMTFormat.FOC_ROLL
            br_cmt_frame_cls = BrCMTFrameFocRoll

        for frame in anm.frames:
            br.write_struct(br_cmt_frame_cls(), frame)

        if anm.has_clip_range():
            self.anm_data_format |= CMTFormat.CLIP_RANGE
            list(map(lambda x: br.write_float(x.clip_range or (0.1, 10000)), anm.frames))
            br.align(0x10)


class BrCMTFrame(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.location = br.read_float(3)
        self.fov = br.read_float()

    def __br_write__(self, br: 'BinaryReader', frame: CMTFrame):
        br.write_float(frame.location[:])
        br.write_float(frame.fov)


class BrCMTFrameRotFloat(BrCMTFrame):
    def __br_read__(self, br: BinaryReader):
        super().__br_read__(br)

        self.rotation = br.read_float(4)

    def __br_write__(self, br: 'BinaryReader', frame: CMTFrame):
        super().__br_write__(br, frame)

        _, rotation = frame.to_dist_rotation()
        br.write_float(rotation[1:] + (rotation[0],))


class BrCMTFrameDistRotShort(BrCMTFrame):
    def __br_read__(self, br: BinaryReader):
        super().__br_read__(br)

        self.distance = br.read_float()
        br.read_uint32()  # Padding
        self.rotation = read_quat_scaled(br, 1)[0]

    def __br_write__(self, br: 'BinaryReader', frame: CMTFrame):
        super().__br_write__(br, frame)

        dist, rotation = frame.to_dist_rotation()
        br.write_float(dist)
        br.pad(4)  # Padding
        write_quat_scaled(br, [rotation[1:] + (rotation[0],)])


class BrCMTFrameDistRotXYZ(BrCMTFrame):
    def __br_read__(self, br: BinaryReader):
        super().__br_read__(br)

        self.distance = br.read_float()
        self.rotation = read_quat_xyz_float(br, 1)[0]

    def __br_write__(self, br: 'BinaryReader', frame: CMTFrame):
        super().__br_write__(br, frame)

        dist, rotation = frame.to_dist_rotation()
        br.write_float(dist)
        br.write_float(rotation[1:4])


class BrCMTFrameFocRoll(BrCMTFrame):
    def __br_read__(self, br: BinaryReader):
        super().__br_read__(br)

        self.focus_point = br.read_float(3)
        self.roll = br.read_float()

    def __br_write__(self, br: 'BinaryReader', frame: CMTFrame):
        super().__br_write__(br, frame)

        br.write_float(frame.focus_point[:])
        br.write_float(frame.roll)
