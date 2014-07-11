from rpython.rlib.rstring import check_ascii
from rpython.rlib.runicode import str_decode_utf_8


def syntax_error_utf8(fmt, s, lineno=0, offset=0, text=None, filename=None,
                      lastlineno=0):
    return SyntaxError(fmt % str_decode_utf_8(s, len(s), 'replace')[0],
                       lineno=lineno, offset=offset, text=text,
                       filename=filename, lastlineno=lastlineno)


def syntax_error_ascii(fmt, s, lineno=0, offset=0, text=None, filename=None,
                       lastlineno=0):
    check_ascii(s)
    return SyntaxError(fmt % s.decode('ascii'), lineno=lineno, offset=offset,
                       text=text, filename=filename, lastlineno=lastlineno)


class SyntaxError(Exception):
    """Base class for exceptions raised by the parser."""

    def __init__(self, msg, lineno=0, offset=0, text=None, filename=None,
                 lastlineno=0):
        assert isinstance(msg, unicode)
        self.msg = msg
        self.lineno = lineno
        self.offset = offset
        self.text = text
        self.filename = filename
        self.lastlineno = lastlineno

    def wrap_info(self, space):
        w_text = w_filename = space.w_None
        if self.text is not None:
            from rpython.rlib.runicode import str_decode_utf_8
            # self.text may not be UTF-8 in case of decoding errors
            w_text = space.wrap(str_decode_utf_8(self.text, len(self.text),
                                                 'replace')[0])
        if self.filename is not None:
            w_filename = space.fsdecode(space.wrapbytes(self.filename))
        if isinstance(self.lineno, str):
            check_ascii(self.lineno)
        if isinstance(self.offset, str):
            check_ascii(self.offset)
        if isinstance(self.lastlineno, str):
            check_ascii(self.lastlineno)
        return space.newtuple([space.wrap(self.msg),
                               space.newtuple([w_filename,
                                               space.wrap(self.lineno),
                                               space.wrap(self.offset),
                                               w_text,
                                               space.wrap(self.lastlineno)])])

    def __str__(self):
        return "%s at pos (%d, %d) in %r" % (self.__class__.__name__,
                                             self.lineno,
                                             self.offset,
                                             self.text)

class IndentationError(SyntaxError):
    pass

class TabError(IndentationError):
    def __init__(self, lineno=0, offset=0, text=None, filename=None,
                 lastlineno=0):
        msg = u"inconsistent use of tabs and spaces in indentation"
        IndentationError.__init__(self, msg, lineno, offset, text,
                                  filename, lastlineno)

class ASTError(Exception):
    def __init__(self, msg, ast_node ):
        self.msg = msg
        self.ast_node = ast_node


class TokenError(SyntaxError):

    def __init__(self, msg, line, lineno, column, tokens, lastlineno=0):
        assert isinstance(msg, unicode)
        SyntaxError.__init__(self, msg, lineno, column, line,
                             lastlineno=lastlineno)
        self.tokens = tokens

class TokenIndentationError(IndentationError):

    def __init__(self, msg, line, lineno, column, tokens):
        assert isinstance(msg, unicode)
        SyntaxError.__init__(self, msg, lineno, column, line)
        self.tokens = tokens
