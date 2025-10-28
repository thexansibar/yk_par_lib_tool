from ...util import *
from ..ifa import *
from .br_rgg import BrRGGString


class BrIFA(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.header = br.read_struct(BrIFAHeader)
        header: BrIFAHeader = self.header

        self.bones: List[BrIFABone] = br.read_struct(BrIFABone, header.bone_count)

    def __br_write__(self, br: BinaryReader, ifa: IFA):
        br.set_endian(Endian.BIG)

        br.write_struct(BrIFAHeader(), len(ifa.bone_list))

        for bone in ifa.bone_list:
            br.write_struct(BrIFABone(), bone)


class BrIFAHeader(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.magic = br.read_str(4)

        if self.magic != '':
            raise Exception(f'Invalid magic: Expected an empty string, got {self.magic}')

        br.read_uint8()  # 0x02 for big, 0x21 for little endian
        self.endianness = br.read_uint8() == 1

        br.set_endian(self.endianness)

        br.seek(10, Whence.CUR)
        self.bone_count = br.read_uint32()
        br.seek(12, Whence.CUR)

    def __br_write__(self, br: 'BinaryReader', bone_count: int):
        br.write_str_fixed('', 4)

        # Endianness (big)
        br.write_uint8(2)
        br.write_uint8(1)

        br.pad(10)
        br.write_uint32(bone_count)
        br.pad(12)


class BrIFABone(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.name: BrRGGString = br.read_struct(BrRGGString)
        self.parent_name: BrRGGString = br.read_struct(BrRGGString)

        self.rotation = br.read_float(4)
        self.location = br.read_float(3)

        br.seek(20, Whence.CUR)

    def __br_write__(self, br: 'BinaryReader', bone: IFABone):
        br.write_struct(BrRGGString(bone.name))
        br.write_struct(BrRGGString(bone.parent_name))

        br.write_float(bone.rotation[:4])
        br.write_float(bone.location[:3])

        br.pad(20)
