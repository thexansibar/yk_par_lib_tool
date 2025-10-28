from enum import IntFlag


class FlagEnum(IntFlag):
    def __contains__(self, other) -> bool:
        if isinstance(other, (int, IntFlag)):
            return (self & other) == other

        return super().__contains__(other)
