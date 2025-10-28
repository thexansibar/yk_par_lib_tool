from .par import File, Par
from .util.binary import BinaryReader
import zlib

# Toggle to True for verbose decompression diagnostics (prints sizes and header hex)
# Enabled by default to aid debugging of small/malformed DDS outputs during imports.
DEBUG_SLLZ = True


def enable_sllz_debug(enable: bool = True) -> None:
    """Enable or disable verbose SLLZ debug printing at runtime.

    Usage: from src.sllz import enable_sllz_debug; enable_sllz_debug(True)
    """
    global DEBUG_SLLZ
    DEBUG_SLLZ = bool(enable)


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

    result = out_buf.copy()
    if DEBUG_SLLZ:
        try:
            sample = ' '.join(f"{b:02X}" for b in result[:32])
        except Exception:
            sample = '<unavailable>'
        print(f"[sllz] decompress_v1: in={len(in_buf)} bytes -> out={len(result)} bytes; header={sample}")
    return result


def decompress_sllz(buf: bytearray) -> bytearray:
    reader = BinaryReader(buf)

    if reader.read_str(4) != "SLLZ":
        raise Exception("Invalid magic")

    reader.set_endian(bool(reader.read_uint8()))

    version = reader.read_uint8()
    header_size = reader.read_uint16()

    decompressed_size = reader.read_uint32()
    compressed_size = reader.read_uint32()

    if DEBUG_SLLZ:
        print(f"[sllz] header: version={version} header_size={header_size} decompressed_size={decompressed_size} compressed_size={compressed_size}")

    reader.seek(header_size)

    if version == 1:
        out = decompress_v1(reader, decompressed_size)
    elif version == 2:
        out = decompress_v2(reader, compressed_size, decompressed_size)
    else:
        raise Exception(f"Unknown compression version: {version}")

    if DEBUG_SLLZ:
        try:
            sample = ' '.join(f"{b:02X}" for b in out[:32])
        except Exception:
            sample = '<unavailable>'
        print(f"[sllz] decompress_sllz: produced {len(out)} bytes; sample={sample}")

    return out


def decompress_file(file: File) -> bytearray:
    """Decompress a file from a PAR archive, loading data lazily if needed."""
    # Ensure file data is loaded before decompression
    if hasattr(file, '_ensure_data_loaded'):
        file._ensure_data_loaded()
    
    if file.compression:
        out = decompress_sllz(file.data)
        if DEBUG_SLLZ:
            try:
                sample = ' '.join(f"{b:02X}" for b in out[:16])
            except Exception:
                sample = '<unavailable>'
            print(f"[sllz] decompress_file: file={getattr(file, 'name', '<unknown>')} out_len={len(out)} sample={sample}")
        return out
    else:
        return file.data


def decompress_par(par: Par) -> None:
    """Decompress all files in a PAR archive (in-place)."""
    for file in par.files:
        # Ensure data is loaded before attempting decompression
        if hasattr(file, '_ensure_data_loaded'):
            file._ensure_data_loaded()
        
        if file.compression:
            file.data = decompress_sllz(file.data)
            file.compression = 0


def decompress_v2(reader: BinaryReader, compressed_size: int, decompressed_size: int) -> bytearray:
    """SLLZ v2 decompression: chunked format where each chunk has a 3-byte compressed size and
    2-byte decompressed size-1; compressed chunks are zlib-compressed, uncompressed chunks are raw.
    Mirrors the logic in ParManager's DecompressV2 implementation.
    """
    # read the compressed payload area (skip header already handled by caller)
    # Many implementations read (compressed_size - 0x10) bytes into the processing buffer
    start = reader.pos()
    in_buf = bytearray(reader.buffer()[start: start + max(0, compressed_size - 0x10)])
    out_buf = bytearray(decompressed_size)

    in_pos = 0
    out_pos = 0

    while out_pos < decompressed_size:
        if in_pos + 5 > len(in_buf):
            raise Exception("SLLZ v2: Corrupt chunk header (unexpected end of input)")

        compressed_chunk_size = (in_buf[in_pos] << 16) | (in_buf[in_pos + 1] << 8) | in_buf[in_pos + 2]
        decompressed_chunk_size = ((in_buf[in_pos + 3] << 8) | in_buf[in_pos + 4]) + 1

        is_compressed = (compressed_chunk_size & 0x00800000) == 0x00000000

        if is_compressed:
            comp_start = in_pos + 5
            comp_len = compressed_chunk_size - 5
            if comp_start + comp_len > len(in_buf):
                raise Exception("SLLZ v2: Compressed chunk extends beyond input buffer")
            comp_slice = bytes(in_buf[comp_start: comp_start + comp_len])
            try:
                decompressed_data = zlib.decompress(comp_slice)
            except Exception as e:
                raise Exception(f"SLLZ v2: zlib decompression failed: {e}")

            if decompressed_chunk_size != len(decompressed_data):
                raise Exception("SLLZ v2: Wrong decompressed data length for chunk")

            out_buf[out_pos: out_pos + len(decompressed_data)] = decompressed_data
            in_pos += compressed_chunk_size
        else:
            # not compressed - copy raw bytes
            compressed_chunk_size = compressed_chunk_size & 0xFF7FFFFF
            copy_start = in_pos + 5
            if copy_start + decompressed_chunk_size > len(in_buf):
                raise Exception("SLLZ v2: Uncompressed chunk extends beyond input buffer")
            out_buf[out_pos: out_pos + decompressed_chunk_size] = in_buf[copy_start: copy_start + decompressed_chunk_size]
            in_pos += compressed_chunk_size

        out_pos += decompressed_chunk_size

    if DEBUG_SLLZ:
        try:
            sample = ' '.join(f"{b:02X}" for b in out_buf[:32])
        except Exception:
            sample = '<unavailable>'
        print(f"[sllz] decompress_v2: in={len(in_buf)} bytes -> out={len(out_buf)} bytes; sample={sample}")

    return out_buf
