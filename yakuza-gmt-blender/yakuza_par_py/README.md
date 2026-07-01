# Yakuza-PAR-py
An incomplete rewrite of Kaplas80's [ParManager](https://github.com/Kaplas80/ParManager) in python.

Can only list and extract PARs, no support for extracting nested PARs, and no rebuilding. This was made to be integrated into other python modules for extraction, though it can still be used as a CLI application.

Supported commands:

    list       Show contents from a Yakuza PAR archive.
    
      args:
        archive (pos. 0)    Yakuza PAR archive path.

    extract    Extract contents from a Yakuza PAR archive.
    
      args:
        archive (pos. 0)             Yakuza PAR archive path.
        path_to_extract\ (pos. 1)    Output directory.
      
