from .par import Par, Header, Folder, File
from .util.binary import BinaryReader
from typing import List


def read_par(path: str) -> Par:
    """Read a .par file and return a populated `Par` object."""
    with open(path, "rb") as fh:
        data = fh.read()

    reader = BinaryReader(bytearray(data))

    magic = reader.read_str(4)
    if magic != "PARC":
        raise ValueError("Invalid PAR: wrong magic")

    # header
    header = Header()
    reader.skip(1)
    header.big_endian = bool(reader.read_uint8())
    reader.set_endian(header.big_endian)
    reader.skip(2)
    header.version = reader.read_uint32()
    reader.skip(4)

    header.folder_count = reader.read_uint32()
    header.folder_offset = reader.read_uint32()

    header.file_count = reader.read_uint32()
    header.file_offset = reader.read_uint32()

    par = Par()
    par.header = header

    # names table: folders first then files
    names: List[str] = []
    for i in range(header.folder_count + header.file_count):
        names.append(reader.read_str(64))

    # folders
    folders: List[Folder] = []
    reader.push()
    reader.seek(header.folder_offset)
    for i in range(header.folder_count):
        folder = Folder()
        folder.name = names[i]
        folder.folder_count = reader.read_uint32()
        folder.folder_start = reader.read_uint32()
        folder.file_count = reader.read_uint32()
        folder.file_start = reader.read_uint32()
        folder.attributes = reader.read_uint32()
        reader.skip(0xC)
        folders.append(folder)
    reader.pop()

    # files
    files: List[File] = []
    reader.push()
    reader.seek(header.file_offset)
    for i in range(header.file_count):
        file = File()
        file.name = names[header.folder_count + i]
        file.compression = reader.read_uint32()
        file.size = reader.read_uint32()
        file.compressed_size = reader.read_uint32()
        file.base_offset = reader.read_uint32()
        file.attributes = reader.read_uint32()
        file.extended_offset = reader.read_uint32()
        file.timestamp = reader.read_uint64()
        # read file data from extended offset/base offset pair
        reader.push()
        # Newer PAR/GMD variants store a 56-bit offset split across extended_offset (high) and base_offset (low).
        # Construct the full 64-bit value then mask to 56 bits to match implementations that encode offsets as:
        #   long offset = ((long)extendedOffset << 32) | baseOffset;
        #   offset &= 0x00FFFFFFFFFFFFFF;
        absolute_offset = ((file.extended_offset << 32) | file.base_offset) & 0x00FFFFFFFFFFFFFF
        reader.seek(absolute_offset)
        file.data = bytearray(reader.read_bytes(file.compressed_size))
        reader.pop()

        files.append(file)
    reader.pop()

    par.folders = folders
    par.files = files

    # fill folder references
    for f in par.folders:
        f.files = par.files[f.file_start: f.file_start + f.file_count]
        f.folders = par.folders[f.folder_start: f.folder_start + f.folder_count]

    return par
