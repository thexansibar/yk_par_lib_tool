from .par import Par, Header, Folder, File
from .util.binary import BinaryReader
from typing import List
from functools import lru_cache
import os

# Optional: Import profiling utilities (can be disabled)
try:
    from .performance import profile, Timer
    _HAS_PROFILING = True
except ImportError:
    _HAS_PROFILING = False
    # Dummy implementations if profiling not available
    def profile(func):
        return func
    class Timer:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass


# LRU cache for parsed PAR structures
# Cache key: (file_path, file_size, modification_time)
# This avoids re-parsing the same PAR file while invalidating cache if file changes
@lru_cache(maxsize=32)
def _read_par_cached(path: str, file_size: int, mtime: float) -> Par:
    """Internal cached PAR reader. Cache key includes file metadata to detect changes."""
    return _read_par_impl(path)


def read_par(path: str) -> Par:
    """Read a .par file and return a populated `Par` object with caching."""
    try:
        stat = os.stat(path)
        file_size = stat.st_size
        mtime = stat.st_mtime
        return _read_par_cached(path, file_size, mtime)
    except Exception:
        # If caching fails, fall back to direct read
        return _read_par_impl(path)


def _read_par_impl(path: str) -> Par:
    """Read a .par file and return a populated `Par` object (implementation)."""
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

    # names table: folders first then files - batch read all names at once
    names: List[str] = []
    names_count = header.folder_count + header.file_count
    for i in range(names_count):
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
        
        # Defer reading file data - only read when needed for decompression/import
        # This massively improves initial PAR parsing speed
        file.data = None  # Will be loaded on-demand
        file._reader = reader  # Store reference for lazy loading
        file._data_offset = ((file.extended_offset << 32) | file.base_offset) & 0x00FFFFFFFFFFFFFF
        file._data_size = file.compressed_size
        
        files.append(file)
    reader.pop()

    par.folders = folders
    par.files = files

    # fill folder references
    for f in par.folders:
        f.files = par.files[f.file_start: f.file_start + f.file_count]
        f.folders = par.folders[f.folder_start: f.folder_start + f.folder_count]
        # Prebuild indexes so first lookup is O(1)
        if hasattr(f, "_ensure_name_indexes"):
            f._ensure_name_indexes()

    # Prebuild top-level indexes so search paths don't pay first-call setup cost
    if hasattr(par, "_ensure_name_indexes"):
        par._ensure_name_indexes()

    return par
