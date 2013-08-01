
#include "Python.h"
#include "numpy/arrayobject.h"
#include <string.h>   /* memset */

PyObject* 
PyArray_ZEROS(int nd, npy_intp* dims, int type_num, int fortran) 
{
    PyObject *arr = PyArray_EMPTY(nd, dims, type_num, fortran);
    memset(PyArray_DATA(arr), 0, PyArray_NBYTES(arr));
    return arr;
}

