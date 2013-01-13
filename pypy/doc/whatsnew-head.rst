======================
What's new in PyPy 2.0
======================

.. this is a revision shortly after release-2.0-beta1
.. startrev: 0e6161a009c6

.. branch: callback-jit
Callbacks from C are now better JITted

.. branch: remove-globals-in-jit

.. branch: length-hint
Implement __lenght_hint__ according to PEP 424

.. branch: numpypy-longdouble
Long double support for numpypy
.. branch: numpypy-real-as-view
Convert real, imag from ufuncs to views. This involves the beginning of 
view() functionality

.. branch: signatures
Improved RPython typing

.. branch: rpython-bytearray
Rudimentary support for bytearray in RPython

.. branches we don't care about
.. branch: autoreds
.. branch: reflex-support
.. branch: kill-faking
.. branch: improved_ebnfparse_error
.. branch: task-decorator

.. branch: release-2.0-beta1

.. branch: remove-PYPY_NOT_MAIN_FILE

.. branch: missing-jit-operations

.. branch: fix-lookinside-iff-oopspec
Fixed the interaction between two internal tools for controlling the JIT.
