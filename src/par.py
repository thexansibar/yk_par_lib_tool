from typing import List


class Header:
    big_endian: bool
    version: int

    folder_count: int
    folder_offset: int
    file_count: int
    file_offset: int


class File:
    name: str

    compression: int
    size: int
    compressed_size: int
    base_offset: int

    attributes: int
    extended_offset: int
    timestamp: int

    data: bytearray


class Directory:
    files: List[File]
    folders: List['Folder']

    def get_file(self, file_name: str) -> File:
        """searches for the file in this directory.
        if the directory is a par, searches in all files of the par."""

        file = [f for f in self.files if f.name == file_name]
        if len(file):
            return file[0]
        return None

    def get_folder(self, folder_name: str) -> 'Folder':
        """searches for the folder in this directory.
        if the directory is a par, searches in all folders of the par."""

        folder = [f for f in self.folders if f.name == folder_name]
        if len(folder):
            return folder[0]
        return None

    def __file_from_path(self, paths: List[str], file_name: str) -> File:
        if len(paths):
            folder = self.get_folder(paths.pop(0))
            if folder:
                return folder.__file_from_path(paths, file_name)
        else:
            return self.get_file(file_name)
        return None

    def get_file_from_path(self, path: str, par_root=False) -> File:
        """path is the path to the file relative to the current directory (folder/par),
        separated by forward slashes (/). if par_root is true, will start searching
        inside the root folder (.) of the par.
        """

        paths = [p for p in path.split("/") if p != ""]
        file_name = paths.pop()

        if par_root and len(self.folders):
            return self.folders[0].__file_from_path(paths, file_name)

        return self.__file_from_path(paths, file_name)


class Folder(Directory):
    name: str

    folder_count: int
    folder_start: int
    file_count: int
    file_start: int

    attributes: int


class Par(Directory):
    header: Header
