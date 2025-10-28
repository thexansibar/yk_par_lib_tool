from ...util import *

RGG_ENCODING = 'cp932'

class BrRGGString(BrStruct):
    data: str

    def __init__(self, string=''):
        self.data = string

    def __br_read__(self, br: BinaryReader):
        self.checksum = br.read_uint16()
        self.data = br.read_str(30, RGG_ENCODING)

    def __br_write__(self, br: BinaryReader):
        string = self.data[:30].encode(RGG_ENCODING)
        br.write_uint16(sum(string))
        br.write_str_fixed(self.data, 30, RGG_ENCODING)
