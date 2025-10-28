from .par import File, Par
from .util.binary import BinaryReader


def decompress_v1(reader: BinaryReader, decompressed_size: int) -> bytearray:
    in_buf = bytearray(reader.buffer()[reader.pos():])
    out_buf = bytearray(decompressed_size)

    in_pos = 0
    out_pos = 0

    flag = in_buf[in_pos]
    in_pos += 1
    flag_count = 8

    while True:
        if flag & 0x80 == 0x80:
            flag = flag << 1
            flag_count -= 1

            if flag_count == 0:
                flag = in_buf[in_pos]
                in_pos += 1
                flag_count = 8

            copy_flags = in_buf[in_pos] | in_buf[in_pos + 1] << 8
            in_pos += 2

            copy_distance = 1 + (copy_flags >> 4)
            copy_count = 3 + (copy_flags & 0xF)

            i = 0
            while True:
                out_buf[out_pos] = out_buf[out_pos - copy_distance]
                out_pos += 1

                i += 1
                if i >= copy_count:
                    break

        else:
            flag = flag << 1
            flag_count -= 1

            if flag_count == 0:
                flag = in_buf[in_pos]
                in_pos += 1
                flag_count = 8

            out_buf[out_pos] = in_buf[in_pos]
            in_pos += 1
            out_pos += 1

        if out_pos >= decompressed_size:
            break

    return out_buf.copy()


def decompress_sllz(buf: bytearray) -> bytearray:
    reader = BinaryReader(buf)

    if reader.read_str(4) != "SLLZ":
        raise("Invalid magic")

    reader.set_endian(bool(reader.read_uint8()))

    version = reader.read_uint8()
    header_size = reader.read_uint16()

    decompressed_size = reader.read_uint32()
    compressed_size = reader.read_uint32()

    reader.seek(header_size)

    if version == 1:
        return decompress_v1(reader, decompressed_size)
    elif version == 2:
        raise("Unimplement compression version: 2")
    else:
        raise(f"Unknown compression version: {version}")


def decompress_file(file: File) -> bytearray:
    if file.compression:
        return decompress_sllz(file.data)
    else:
        return file.data


def decompress_par(par: Par) -> None:
    for file in par.files:
        if file.compression:
            file.data = decompress_sllz(file.data)
            file.compression = 0
