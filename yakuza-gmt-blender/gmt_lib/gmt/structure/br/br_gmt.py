from ...util import *
from ..enums.gmt_enum import *
from ..gmt import GMT, GMTCurve
from .br_gmt_anm_data import *
from .br_rgg import BrRGGString


class BrGMT(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.header: BrGMTHeader = br.read_struct(BrGMTHeader)
        header: BrGMTHeader = self.header

        br.seek(header.animations_offset)
        self.animations = br.read_struct(BrGMTAnimation, header.animations_count)

        self.graphs = [None] * header.graphs_count
        br.seek(header.graphs_offset)
        for i, offset in enumerate(br.read_uint32(header.graphs_count)):
            br.seek(offset)
            self.graphs[i] = br.read_struct(BrGMTGraph)

        br.seek(header.strings_offset)
        self.strings = br.read_struct(BrRGGString, header.strings_count)

        br.seek(header.bone_groups_offset)
        self.bone_groups = br.read_struct(BrGMTGroup, header.bone_groups_count, None)

        br.seek(header.curve_groups_offset)
        self.curve_groups = br.read_struct(BrGMTGroup, header.curve_groups_count, header.version)

        br.seek(header.curves_offset)
        self.curves = br.read_struct(BrGMTCurve, header.curves_count, self.graphs, header.version)

    def __br_write__(self, br: BinaryReader, gmt: GMT):
        br.set_endian(Endian.BIG)

        graphs, bone_groups, curve_groups = list(), list(), list()
        curve_groups_index = 0
        bone_strings = list()

        # The file is being written backwards
        # First thing after the header is the animation data, then the curves
        # Then comes the curve groups, the bone groups, and the strings
        # And then the graphs, the graph offsets, and finally, the animation structs

        # Header size
        anm_data_start = 0x80

        # Curves and animation data
        curve_br, anm_data_br = BinaryReader(endianness=Endian.BIG), BinaryReader(endianness=Endian.BIG)
        anm_data_size_offset, graphs_index_count = list(), list()
        curves_index = 0
        for anm in gmt.animation_list:
            # Add the bone group (index, count)
            bone_groups.append(BrGMTGroup(len(bone_strings) + len(gmt.animation_list), len(anm.bones)))

            # Add the bone names
            bone_strings.extend(list(anm.bones.keys()))

            anm_data_offset = anm_data_br.pos()
            graphs_index = len(graphs)

            # This allows us to reuse graphs when possible
            graphs_dict: IterativeDict = IterativeDict()

            for bone in anm.bones.values():
                print("Writing bone " + bone.name)
                
                # Add the curve group (index, count)
                curve_groups.append(BrGMTGroup(curves_index, len(bone.curves)))
                curves_index += len(bone.curves)
                for curve in bone.curves:
                    curve_br.write_struct(BrGMTCurve(), curve, graphs_dict, anm_data_br, anm_data_start, gmt.version)

            # Since graphs are unique per animation, we reset the dictionary and add its items to the graphs list
            graphs.extend(graphs_dict)

            anm_data_size_offset.append((anm_data_br.pos() - anm_data_offset, anm_data_start + anm_data_offset))
            graphs_index_count.append((graphs_index, len(graphs) - graphs_index))

        # Align animation data buffer
        anm_data_br.align(0x20)

        # Curve groups
        curve_groups_br = BinaryReader(endianness=Endian.BIG)
        for g in curve_groups:

            if(gmt.version > GMTVersion.ISHIN):
                g.count = int(g.count * 1024)

            curve_groups_br.write_struct(g)

        curve_groups_br.align(0x20)

        # Bone groups
        bone_groups_br = BinaryReader(endianness=Endian.BIG)
        for g in bone_groups:
            bone_groups_br.write_struct(g)

        bone_groups_br.align(0x20)

        # Add all anm and bone names to the strings list
        strings = list(map(lambda x: BrRGGString(x.name), gmt.animation_list))
        strings.extend(map(BrRGGString, bone_strings))

        # Strings
        strings_br = BinaryReader(endianness=Endian.BIG)
        for s in strings:
            strings_br.write_struct(s)

        graph_data_start = anm_data_start + anm_data_br.size() + curve_br.size() + curve_groups_br.size() + \
            bone_groups_br.size() + strings_br.size()

        # Graph data
        graphs_offsets_br, graphs_data_br = BinaryReader(endianness=Endian.BIG), BinaryReader(endianness=Endian.BIG)
        graph_data_size_offset = list()

        for index, count in graphs_index_count:
            graph_data_offset = graphs_data_br.pos()
            for i in range(index, index + count):
                graphs_offsets_br.write_uint32(graph_data_start + graphs_data_br.size())
                graphs_data_br.write_struct(graphs[i])

            graph_data_size_offset.append((graphs_data_br.pos() - graph_data_offset,
                                           graph_data_start + graph_data_offset))

        graphs_offsets_br.align(0x20)
        graphs_data_br.align(0x20)

        # Animations
        anm_br = BinaryReader(endianness=Endian.BIG)
        for i, anm in enumerate(gmt.animation_list):
            # start_frame
            anm_br.write_uint32(anm.get_start_frame())

            # end_frame
            anm_br.write_uint32(anm.get_end_frame())

            # index
            anm_br.write_uint32(i)

            # frame_rate
            anm_br.write_float(anm.frame_rate)

            # name_index
            anm_br.write_uint32(i)

            # bone_group_index
            anm_br.write_uint32(i)

            # curve_groups_index
            anm_br.write_uint32(curve_groups_index)
            curve_groups_index += len(anm.bones)

            # curve_groups_count
            anm_br.write_uint32(len(anm.bones))

            # curves_count
            anm_br.write_uint32(sum(map(lambda x: len(x.curves), anm.bones.values())))

            # graphs_index and graphs_count
            anm_br.write_uint32(graphs_index_count[i])

            # animation_data_size and animation_data_offset
            anm_br.write_uint32(anm_data_size_offset[i])

            # graph_data_size and graph_data_size
            anm_br.write_uint32(graph_data_size_offset[i])

            # Padding
            anm_br.write_uint32(0)

        # Calculate the section start offsets
        curves_start = anm_data_start + anm_data_br.size()
        curve_groups_start = curves_start + curve_br.size()
        bone_groups_start = curve_groups_start + curve_groups_br.size()
        strings_start = bone_groups_start + bone_groups_br.size()
        graphs_data_start = strings_start + strings_br.size()
        graphs_offsets_start = graphs_data_start + graphs_data_br.size()
        anm_start = graphs_offsets_start + graphs_offsets_br.size()

        file_size = anm_start + anm_br.size()

        # Header
        br.write_str('GSGT')

        # Use big endian by default (because it is guaranteed to be supported by all versions)
        br.write_uint8(2)
        br.write_uint8(1)

        # Padding
        br.write_uint16(0)

        br.write_uint32(int(gmt.version))

        # File size without padding
        br.write_uint32(file_size)

        br.write_struct(BrRGGString(gmt.name))

        br.write_uint32(len(gmt.animation_list))
        br.write_uint32(anm_start)
        br.write_uint32(len(graphs))
        br.write_uint32(graphs_offsets_start)
        br.write_uint32(graphs_data_br.size())
        br.write_uint32(graphs_data_start)
        br.write_uint32(len(strings))
        br.write_uint32(strings_start)
        br.write_uint32(len(bone_groups))
        br.write_uint32(bone_groups_start)
        br.write_uint32(len(curve_groups))
        br.write_uint32(curve_groups_start)
        br.write_uint32(curves_index)
        br.write_uint32(curves_start)
        br.write_uint32(anm_data_br.size())
        br.write_uint32(anm_data_start)

        # Padding
        br.pad(0xC)

        # Flags
        if gmt.is_face_gmt:
            br.write_uint32(0x07_21_03_01)
        else:
            br.write_uint32(0)

        # Merge all of the buffers
        br.extend(anm_data_br.buffer())
        br.extend(curve_br.buffer())
        br.extend(curve_groups_br.buffer())
        br.extend(bone_groups_br.buffer())
        br.extend(strings_br.buffer())
        br.extend(graphs_data_br.buffer())
        br.extend(graphs_offsets_br.buffer())
        br.extend(anm_br.buffer())

        br.seek(0, Whence.END)

        # Align
        br.align(0x1000)


class BrGMTHeader(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.magic = br.read_str(4)

        if self.magic != 'GSGT':
            raise Exception(f'Invalid magic: Expected GSGT, got {self.magic}')

        br.read_uint8()  # 0x02 for big, 0x21 for little endian
        self.endianness = br.read_uint8() == 1

        br.set_endian(self.endianness)

        # Padding
        br.read_uint16()

        self.version = GMTVersion(br.read_uint32())

        # File size without padding
        self.data_size = br.read_uint32()

        self.file_name: BrRGGString = br.read_struct(BrRGGString)

        self.animations_count = br.read_uint32()
        self.animations_offset = br.read_uint32()
        self.graphs_count = br.read_uint32()
        self.graphs_offset = br.read_uint32()
        self.graph_data_size = br.read_uint32()
        self.graph_data_offset = br.read_uint32()
        self.strings_count = br.read_uint32()
        self.strings_offset = br.read_uint32()
        self.bone_groups_count = br.read_uint32()
        self.bone_groups_offset = br.read_uint32()
        self.curve_groups_count = br.read_uint32()
        self.curve_groups_offset = br.read_uint32()
        self.curves_count = br.read_uint32()
        self.curves_offset = br.read_uint32()
        self.animation_data_size = br.read_uint32()
        self.animation_data_offset = br.read_uint32()

        # Padding
        br.read_uint32(3)

        # Unknown functionality
        self.flags = br.read_uint8(4)


class BrGMTAnimation(BrStruct):
    def __br_read__(self, br: BinaryReader):
        self.start_frame = br.read_uint32()
        self.end_frame = br.read_uint32()
        self.index = br.read_uint32()
        self.frame_rate = br.read_float()
        self.name_index = br.read_uint32()
        self.bone_group_index = br.read_uint32()
        self.curve_groups_index = br.read_uint32()
        self.curve_groups_count = br.read_uint32()
        self.curves_count = br.read_uint32()
        self.graphs_index = br.read_uint32()
        self.graphs_count = br.read_uint32()
        self.animation_data_size = br.read_uint32()
        self.animation_data_offset = br.read_uint32()
        self.graph_data_size = br.read_uint32()
        self.graph_data_offset = br.read_uint32()
        br.read_uint32()


class BrGMTGraph(BrStruct):
    def __init__(self, values=None):
        self.values = list() if values is None else values

    def __br_read__(self, br: BinaryReader):
        self.count = br.read_uint16()
        self.values = br.read_uint16(self.count)
        br.read_int16()  # Delimiter (0xFFFF)

    def __br_write__(self, br: BinaryReader):
        br.write_uint16(len(self.values))
        br.write_uint16(self.values)
        br.write_int16(-1)

    def __hash__(self) -> int:
        return len(self.values) ^ sum(map(hash, self.values))

    def __eq__(self, o: object) -> bool:
        return isinstance(o, BrGMTGraph) and len(o.values) == len(self.values) and all(map(lambda x, y: x == y, o.values, self.values))


class BrGMTGroup(BrStruct):
    index: int
    count: int

    def __init__(self, index=0, count=0):
        self.index = index
        self.count = count

    def __br_read__(self, br: BinaryReader, version):
        self.index = br.read_uint16()
        self.count = br.read_uint16()

        # Version only relevant for GMT Bone Curve maps since Gaiden
        if(version != None and version > GMTVersion.ISHIN):
            self.count = int(self.count / 1024)

    def __br_write__(self, br: BinaryReader):
        br.write_uint16(self.index)
        br.write_uint16(self.count)


class BrGMTCurve(BrStruct):
    def __br_read__(self, br: BinaryReader, graphs, version):
        self.graph_index = br.read_uint32()
        self.animation_data_offset = br.read_uint32()
        self.format = GMTCurveFormat(br.read_uint32())

        # This value has to be read as a single uint32
        channel_type = br.read_uint32()

        self.channel = GMTCurveChannel(channel_type >> 16)
        self.type = GMTCurveType(channel_type & 0xFFFF)

        self.graph = graphs[self.graph_index]
        count = self.graph.count
        with br.seek_to(self.animation_data_offset):
            if self.format == GMTCurveFormat.ROT_QUAT_XYZ_FLOAT:
                self.values = read_quat_xyz_float(br, count)
            elif self.format == GMTCurveFormat.ROT_XYZW_SHORT:
                if version > GMTVersion.KENZAN:
                    self.values = read_quat_scaled(br, count)
                else:
                    self.values = read_quat_half_float(br, count)
            elif self.format == GMTCurveFormat.LOC_CHANNEL:
                self.values = read_loc_channel(br, count)
            elif self.format == GMTCurveFormat.LOC_XYZ:
                self.values = read_loc_all(br, count)
            elif self.format in [GMTCurveFormat.ROT_XW_FLOAT, GMTCurveFormat.ROT_YW_FLOAT, GMTCurveFormat.ROT_ZW_FLOAT]:
                self.values = read_quat_channel_float(br, count)
            elif self.format in [GMTCurveFormat.ROT_XW_SHORT, GMTCurveFormat.ROT_YW_SHORT, GMTCurveFormat.ROT_ZW_SHORT]:
                if version > GMTVersion.KENZAN:
                    self.values = read_quat_channel_scaled(br, count)
                else:
                    self.values = read_quat_channel_half_float(br, count)
            elif self.format == GMTCurveFormat.PATTERN_HAND:
                self.values = read_pattern_short(br, count)
            elif self.format == GMTCurveFormat.PATTERN_UNK:
                self.values = read_bytes(br, count)
            elif self.format == GMTCurveFormat.ROT_QUAT_XYZ_INT:
                self.values = read_quat_xyz_int(br, count)
            else:
                self.values = read_bytes(br, count)

    #Known as Animation Segment in the template
    def __br_write__(self, br: BinaryReader, curve: GMTCurve, graphs_dict: IterativeDict, anm_data_br: BinaryReader, anm_data_start: int, version: GMTVersion):
        frames, values = zip(*map(lambda x: (x.frame, x.value), curve.keyframes))

        # graph_index
        br.write_uint32(graphs_dict.get_or_next(BrGMTGraph(frames)))

        # animation_data_offset
        br.write_uint32(anm_data_start + anm_data_br.size())

        # format
        if curve.type == GMTCurveType.LOCATION:
            if curve.channel == GMTCurveChannel.ALL:
                br.write_uint32(int(GMTCurveFormat.LOC_XYZ))
                write_loc_all(anm_data_br, values)
            else:
                br.write_uint32(int(GMTCurveFormat.LOC_CHANNEL))
                write_loc_channel(anm_data_br, values)
        elif curve.type == GMTCurveType.ROTATION:
            if curve.channel == GMTCurveChannel.ALL:
                br.write_uint32(int(GMTCurveFormat.ROT_XYZW_SHORT))
                if version > GMTVersion.KENZAN:
                    write_quat_scaled(anm_data_br, values)
                else:
                    write_quat_half_float(anm_data_br, values)
            else:
                if curve.channel == GMTCurveChannel.XW:
                    br.write_uint32(int(GMTCurveFormat.ROT_XW_SHORT))
                elif curve.channel == GMTCurveChannel.YW:
                    br.write_uint32(int(GMTCurveFormat.ROT_YW_SHORT))
                elif curve.channel == GMTCurveChannel.ZW:
                    br.write_uint32(int(GMTCurveFormat.ROT_ZW_SHORT))
                else:
                    pass

                if version > GMTVersion.KENZAN:
                    write_quat_channel_scaled(anm_data_br, values)
                else:
                    write_quat_channel_half_float(anm_data_br, values)
        elif curve.type == GMTCurveType.PATTERN_HAND:
            br.write_uint32(int(GMTCurveFormat.PATTERN_HAND))
            write_pattern_short(anm_data_br, values)
        elif curve.type in [GMTCurveType.PATTERN_UNK, GMTCurveType.PATTERN_FACE]:
            br.write_uint32(int(GMTCurveFormat.PATTERN_UNK))
            write_bytes(anm_data_br, values)
        else:
            pass

        value = (curve.channel << 16) | int(curve.type)
        value = int(value)
        # channel_type
        br.write_uint32(value)
