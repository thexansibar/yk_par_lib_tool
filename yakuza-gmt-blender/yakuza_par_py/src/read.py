from .par import *
from .util.binary import BinaryReader

par: Par()
reader: BinaryReader


def read_header() -> Header:
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

    return header


def read_names() -> List[str]:
    names = []
    for i in range(par.header.folder_count + par.header.file_count):
        names.append(reader.read_str(64))

    return names


def read_folders(names: List[str]) -> List[Folder]:
    folders = []

    reader.push()
    reader.seek(par.header.folder_offset)

    for i in range(par.header.folder_count):
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
    return folders


def read_files(names: List[str]) -> List[File]:
    files = []

    reader.push()
    reader.seek(par.header.file_offset)

    for i in range(par.header.file_count):
        file = File()
        file.name = names[i]

        file.compression = reader.read_uint32()
        file.size = reader.read_uint32()
        file.compressed_size = reader.read_uint32()
        file.base_offset = reader.read_uint32()

        file.attributes = reader.read_uint32()
        file.extended_offset = reader.read_uint32()
        file.timestamp = reader.read_uint64()

        reader.push()
        reader.seek((file.extended_offset << 32) | file.base_offset)

        file.data = bytearray(reader.read_bytes(file.compressed_size))

        reader.pop()

        files.append(file)

    reader.pop()
    return files


def fill_folders() -> None:
    for f in par.folders:
        f.files = par.files[f.file_start: f.file_start + f.file_count]
        f.folders = par.folders[f.folder_start: f.folder_start + f.folder_count]


def read_par(path: str) -> Par:
    global par, reader

    par = Par()

    f = open(path, "rb")
    reader = BinaryReader(f.read())
    f.close()

    if reader.read_str(4) != "PARC":
        raise("Invalid magic")

    par.header = read_header()

    names = read_names()

    par.folders = read_folders(names[: par.header.folder_count + 1])
    par.files = read_files(names[par.header.folder_count:])

    fill_folders()

    return par
