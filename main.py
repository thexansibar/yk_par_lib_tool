import sys
from os import mkdir, path
from typing import List

from parser_strings import *
from src import *


def list_folder(f: Folder, prefix: str):
    prefix += f.name + "/"
    print(prefix)

    for file in f.files:
        print(prefix + file.name)

    for folder in f.folders:
        list_folder(folder, prefix)


def list_par(inpath: str):
    par = read_par(inpath)

    if len(par.folders):
        list_folder(par.folders[0], "/" + path.basename(inpath) + "/")


def extract_folder(f: Folder, current_path: str, relative_par_path: str):
    relative_par_path += f.name + "/"
    if f.name != "." and f.name != "pxdArchiver":
        current_path = path.join(current_path, f.name)
        if not path.exists(current_path):
            mkdir(current_path)

    for file in f.files:
        print(f"Extracting {relative_par_path + file.name}... ", end='')
        with open(path.join(current_path, file.name), 'wb') as new_file:
            new_file.write(decompress_file(file))
        print("DONE!")

    for folder in f.folders:
        extract_folder(folder, current_path, relative_par_path)


def extract_par(inpath: str, outpath: str):
    par = read_par(inpath)

    extract_folder(par.folders[0], outpath, "/" + path.basename(inpath) + "/")


def parse_args(args: List[str]):
    if len(args) > 1:
        if args[1] == 'list':
            if len(args) == 3 and path.isfile(args[2]):
                list_par(args[2])

            else:
                print(about + invalid_args_list)

        elif args[1] == 'extract':
            if len(args) == 4 and path.isfile(args[2]):
                if not path.exists(args[3]):
                    mkdir(args[3])

                elif not path.isdir(args[3]):
                    print(about + invalid_args_extract)

                extract_par(args[2], args[3])
            else:
                print(about + invalid_args_extract)

        else:
            print(about + invalid_command + help)

    else:
        print(about + help)


def main():
    parse_args(sys.argv)


if __name__ == "__main__":
    main()
