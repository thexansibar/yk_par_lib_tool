about = """
Yakuza-PAR.py v0.1
An incomplete rewrite of Kaplas80's Par Manager in python
"""

help = """
Supported commands:

    list       Show contents from a Yakuza PAR archive.

    extract    Extract contents from a Yakuza PAR archive.
"""

invalid_command = "\nERROR: Invalid command."

invalid_args_list = """
ERROR: Invalid arguments.
Arguments for command "list":

    archive (pos. 0)    Yakuza PAR archive path.
"""

invalid_args_extract = """
ERROR: Invalid arguments.
Arguments for command "extract":

    archive (pos. 0)             Yakuza PAR archive path.

    path_to_extract\ (pos. 1)    Output directory.
"""
