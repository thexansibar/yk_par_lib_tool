from .structure.br.br_cmt import *
from .structure.br.br_gmt import *
from .structure.br.br_ifa import *
from .structure.cmt import *
from .structure.gmt import *
from .structure.ifa import *
from .util import *


def write_gmt(gmt: GMT) -> bytearray:
    """Writes a GMT object to a buffer and returns the buffer as a bytearray
    :param gmt: The GMT object
    :return: Bytearray containing the written GMT file
    """

    with BinaryReader() as br:
        br.write_struct(BrGMT(), gmt)
        return br.buffer()


def write_gmt_to_file(gmt: GMT, path: str) -> None:
    """Writes a GMT object to a file
    :param gmt: The GMT object
    :param path: Path to target file as a string
    """

    with open(path, 'wb') as f:
        f.write(write_gmt(gmt))


def write_cmt(cmt: CMT) -> bytearray:
    """Writes a CMT object to a buffer and returns the buffer as a bytearray
    :param cmt: The CMT object
    :return: Bytearray containing the written CMT file
    """

    with BinaryReader() as br:
        br.write_struct(BrCMT(), cmt)
        return br.buffer()


def write_cmt_to_file(cmt: CMT, path: str) -> None:
    """Writes a CMT object to a file
    :param cmt: The CMT object
    :param path: Path to target file as a string
    """

    with open(path, 'wb') as f:
        f.write(write_cmt(cmt))


def write_ifa(ifa: IFA) -> bytearray:
    """Writes an IFA object to a buffer and returns the buffer as a bytearray
    :param ifa: The IFA object
    :return: Bytearray containing the written IFA file
    """

    with BinaryReader() as br:
        br.write_struct(BrIFA(), ifa)
        return br.buffer()


def write_ifa_to_file(ifa: IFA, path: str) -> None:
    """Writes an IFA object to a file
    :param ifa: The IFA object
    :param path: Path to target file as a string
    """

    with open(path, 'wb') as f:
        f.write(write_ifa(ifa))
