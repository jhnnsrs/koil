from contextvars import ContextVar




class Context:
    def __init__(self, value):
        self.__value: ContextVar = value

    def set(self, value):
        return self.__value.set(value)

    def reset(self, token):
        self.__value.reset(token)
        return

    @classmethod
    def __get_validators__(cls):
        # one or more validators may be yielded which will be called in the
        # order to validate the input, each validator will receive as an input
        # the value returned from the previous validator
        yield cls.validate

    @classmethod
    def validate(cls, v):
        if isinstance(v, ContextVar):
            return cls(v)

        raise TypeError("Needs to be either a instance of ContextVar or a string")
