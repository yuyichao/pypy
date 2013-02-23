from pypy.interpreter.mixedmodule import MixedModule
from pypy.module.micronumpy.interp_boxes import long_double_size


class Module(MixedModule):
    applevel_name = '_numpypy'

    interpleveldefs = {
        'ndarray': 'interp_numarray.W_NDimArray',
        'dtype': 'interp_dtype.W_Dtype',
        'ufunc': 'interp_ufuncs.W_Ufunc',

        'array': 'interp_numarray.array',
        'zeros': 'interp_numarray.zeros',
        'empty': 'interp_numarray.zeros',
        'ones': 'interp_numarray.ones',
        'dot': 'interp_arrayops.dot',
        'fromstring': 'interp_support.fromstring',
        'flatiter': 'interp_flatiter.W_FlatIterator',
        'concatenate': 'interp_arrayops.concatenate',
        'repeat': 'interp_arrayops.repeat',
        'where': 'interp_arrayops.where',
        'count_nonzero': 'interp_arrayops.count_nonzero',

        'set_string_function': 'appbridge.set_string_function',

        'True_': 'types.Bool.True',
        'False_': 'types.Bool.False',

        'typeinfo': 'interp_dtype.get_dtype_cache(space).w_typeinfo',

        'generic': 'interp_boxes.W_GenericBox',
        'number': 'interp_boxes.W_NumberBox',
        'integer': 'interp_boxes.W_IntegerBox',
        'signedinteger': 'interp_boxes.W_SignedIntegerBox',
        'unsignedinteger': 'interp_boxes.W_UnsignedIntegerBox',
        'bool_': 'interp_boxes.W_BoolBox',
        'bool8': 'interp_boxes.W_BoolBox',
        'int8': 'interp_boxes.W_Int8Box',
        'byte': 'interp_boxes.W_Int8Box',
        'uint8': 'interp_boxes.W_UInt8Box',
        'ubyte': 'interp_boxes.W_UInt8Box',
        'int16': 'interp_boxes.W_Int16Box',
        'short': 'interp_boxes.W_Int16Box',
        'uint16': 'interp_boxes.W_UInt16Box',
        'ushort': 'interp_boxes.W_UInt16Box',
        'int32': 'interp_boxes.W_Int32Box',
        'intc': 'interp_boxes.W_Int32Box',
        'uint32': 'interp_boxes.W_UInt32Box',
        'uintc': 'interp_boxes.W_UInt32Box',
        'int64': 'interp_boxes.W_Int64Box',
        'uint64': 'interp_boxes.W_UInt64Box',
        'longlong': 'interp_boxes.W_LongLongBox',
        'ulonglong': 'interp_boxes.W_ULongLongBox',
        'int_': 'interp_boxes.W_LongBox',
        'inexact': 'interp_boxes.W_InexactBox',
        'floating': 'interp_boxes.W_FloatingBox',
        'float_': 'interp_boxes.W_Float64Box',
        'float16': 'interp_boxes.W_Float16Box',
        'float32': 'interp_boxes.W_Float32Box',
        'float64': 'interp_boxes.W_Float64Box',
        # 'longdouble': 'interp_boxes.W_LongDoubleBox',
        # 'longfloat': 'interp_boxes.W_LongDoubleBox',
        'intp': 'types.IntP.BoxType',
        'uintp': 'types.UIntP.BoxType',
        'flexible': 'interp_boxes.W_FlexibleBox',
        'character': 'interp_boxes.W_CharacterBox',
        'str_': 'interp_boxes.W_StringBox',
        'string_': 'interp_boxes.W_StringBox',
        'unicode_': 'interp_boxes.W_UnicodeBox',
        'void': 'interp_boxes.W_VoidBox',
        'complexfloating': 'interp_boxes.W_ComplexFloatingBox',
        'complex_': 'interp_boxes.W_Complex128Box',
        'complex128': 'interp_boxes.W_Complex128Box',
        'complex64': 'interp_boxes.W_Complex64Box',
        # 'clongdouble': 'interp_boxes.W_CLongDoubleBox',
        # 'clongfloat': 'interp_boxes.W_CLongDoubleBox',
    }

    # ufuncs
    for exposed, impl in [
        ("absolute", "absolute"),
        ("add", "add"),
        ("arccos", "arccos"),
        ("arcsin", "arcsin"),
        ("arctan", "arctan"),
        ("arctan2", "arctan2"),
        ("arccosh", "arccosh"),
        ("arcsinh", "arcsinh"),
        ("arctanh", "arctanh"),
        ("conj", "conjugate"),
        ("conjugate", "conjugate"),
        ("copysign", "copysign"),
        ("cos", "cos"),
        ("cosh", "cosh"),
        ("divide", "divide"),
        ("true_divide", "true_divide"),
        ("equal", "equal"),
        ("exp", "exp"),
        ("exp2", "exp2"),
        ("expm1", "expm1"),
        ("fabs", "fabs"),
        ("fmax", "fmax"),
        ("fmin", "fmin"),
        ("fmod", "fmod"),
        ("floor", "floor"),
        ("ceil", "ceil"),
        ("trunc", "trunc"),
        ("greater", "greater"),
        ("greater_equal", "greater_equal"),
        ("less", "less"),
        ("less_equal", "less_equal"),
        ("maximum", "maximum"),
        ("minimum", "minimum"),
        ("multiply", "multiply"),
        ("negative", "negative"),
        ("not_equal", "not_equal"),
        ("radians", "radians"),
        ("degrees", "degrees"),
        ("deg2rad", "radians"),
        ("rad2deg", "degrees"),
        ("reciprocal", "reciprocal"),
        ("sign", "sign"),
        ("signbit", "signbit"),
        ("sin", "sin"),
        ("sinh", "sinh"),
        ("subtract", "subtract"),
        ('sqrt', 'sqrt'),
        ('square', 'square'),
        ("tan", "tan"),
        ("tanh", "tanh"),
        ('bitwise_and', 'bitwise_and'),
        ('bitwise_or', 'bitwise_or'),
        ('bitwise_xor', 'bitwise_xor'),
        ('bitwise_not', 'invert'),
        ('left_shift', 'left_shift'),
        ('right_shift', 'right_shift'),
        ('invert', 'invert'),
        ('isnan', 'isnan'),
        ('isinf', 'isinf'),
        ('isneginf', 'isneginf'),
        ('isposinf', 'isposinf'),
        ('isfinite', 'isfinite'),
        ('logical_and', 'logical_and'),
        ('logical_xor', 'logical_xor'),
        ('logical_not', 'logical_not'),
        ('logical_or', 'logical_or'),
        ('log', 'log'),
        ('log2', 'log2'),
        ('log10', 'log10'),
        ('log1p', 'log1p'),
        ('power', 'power'),
        ('floor_divide', 'floor_divide'),
        ('logaddexp', 'logaddexp'),
        ('logaddexp2', 'logaddexp2'),
        ('real', 'real'),
        ('imag', 'imag'),
    ]:
        interpleveldefs[exposed] = "interp_ufuncs.get(space).%s" % impl

    appleveldefs = {
        'arange': 'app_numpy.arange',
    }
    def setup_after_space_initialization(self):
        space = self.space
        all_list = sorted(Module.interpleveldefs.keys() + \
                                Module.appleveldefs.keys())
        # found by set(numpypy.__all__) - set(numpy.__all__)
        all_list.remove('set_string_function')
        all_list.remove('typeinfo')
        w_all = space.wrap(all_list)
        space.setitem(self.w_dict, space.new_interned_str('__all__'), w_all)

if 0 and long_double_size == 16:
    Module.interpleveldefs['float128'] = 'interp_boxes.W_Float128Box'
    Module.interpleveldefs['complex256'] = 'interp_boxes.W_Complex256Box'
elif 0 and long_double_size == 12:
    Module.interpleveldefs['float96'] = 'interp_boxes.W_Float96Box'
    Module.interpleveldefs['complex192'] = 'interp_boxes.W_Complex192Box'
