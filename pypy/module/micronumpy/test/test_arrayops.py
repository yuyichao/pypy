
from pypy.module.micronumpy.test.test_base import BaseNumpyAppTest

class AppTestNumSupport(BaseNumpyAppTest):
    def test_where(self):
        from numpypy import where, ones, zeros, array
        a = [1, 2, 3, 0, -3]
        a = where(array(a) > 0, ones(5), zeros(5))
        assert (a == [1, 1, 1, 0, 0]).all()

    def test_where_differing_dtypes(self):
        from numpypy import array, ones, zeros, where
        a = [1, 2, 3, 0, -3]
        a = where(array(a) > 0, ones(5, dtype=int), zeros(5, dtype=float))
        assert (a == [1, 1, 1, 0, 0]).all()

    def test_where_broadcast(self):
        from numpypy import array, where
        a = where(array([[1, 2, 3], [4, 5, 6]]) > 3, [1, 1, 1], 2)
        assert (a == [[2, 2, 2], [1, 1, 1]]).all()
        a = where(True, [1, 1, 1], 2)
        assert (a == [1, 1, 1]).all()

    def test_where_errors(self):
        from numpypy import where, array
        raises(ValueError, "where([1, 2, 3], [3, 4, 5])")
        raises(ValueError, "where([1, 2, 3], [3, 4, 5], [6, 7])")
        assert where(True, 1, 2) == array(1)
        assert where(False, 1, 2) == array(2)
        assert (where(True, [1, 2, 3], 2) == [1, 2, 3]).all()
        assert (where(False, 1, [1, 2, 3]) == [1, 2, 3]).all()
        assert (where([1, 2, 3], True, False) == [True, True, True]).all()

    #def test_where_1_arg(self):
    #    xxx

    def test_where_invalidates(self):
        from numpypy import where, ones, zeros, array
        a = array([1, 2, 3, 0, -3])
        b = where(a > 0, ones(5), zeros(5))
        a[0] = 0
        assert (b == [1, 1, 1, 0, 0]).all()


    def test_dot(self):
        from numpypy import array, dot, arange
        a = array(range(5))
        assert dot(a, a) == 30.0

        a = array(range(5))
        assert a.dot(range(5)) == 30
        assert dot(range(5), range(5)) == 30
        assert (dot(5, [1, 2, 3]) == [5, 10, 15]).all()

        a = arange(12).reshape(3, 4)
        b = arange(12).reshape(4, 3)
        c = a.dot(b)
        assert (c == [[ 42, 48, 54], [114, 136, 158], [186, 224, 262]]).all()

        a = arange(24).reshape(2, 3, 4)
        raises(ValueError, "a.dot(a)")
        b = a[0, :, :].T
        #Superfluous shape test makes the intention of the test clearer
        assert a.shape == (2, 3, 4)
        assert b.shape == (4, 3)
        c = dot(a, b)
        assert (c == [[[14, 38, 62], [38, 126, 214], [62, 214, 366]],
                   [[86, 302, 518], [110, 390, 670], [134, 478, 822]]]).all()
        c = dot(a, b[:, 2])
        assert (c == [[62, 214, 366], [518, 670, 822]]).all()
        a = arange(3*2*6).reshape((3,2,6))
        b = arange(3*2*6)[::-1].reshape((2,6,3))
        assert dot(a, b)[2,0,1,2] == 1140
        assert (dot([[1,2],[3,4]],[5,6]) == [17, 39]).all()

    def test_dot_constant(self):
        from numpypy import array, dot
        a = array(range(5))
        b = a.dot(2.5)
        for i in xrange(5):
            assert b[i] == 2.5 * a[i]
        c = dot(4, 3.0)
        assert c == 12.0
        c = array(3.0).dot(array(4))
        assert c == 12.0

    def test_choose_basic(self):
        from numpypy import array, choose
        a, b, c = array([1, 2, 3]), array([4, 5, 6]), array([7, 8, 9])
        r = array([2, 1, 0]).choose([a, b, c])
        assert (r == [7, 5, 3]).all()
        r = choose(array([2, 1, 0]), [a, b, c])
        assert (r == [7, 5, 3]).all()

    def test_choose_broadcast(self):
        from numpypy import array
        a, b, c = array([1, 2, 3]), [4, 5, 6], 13
        r = array([2, 1, 0]).choose([a, b, c])
        assert (r == [13, 5, 3]).all()

    def test_choose_out(self):
        from numpypy import array
        a, b, c = array([1, 2, 3]), [4, 5, 6], 13
        r = array([2, 1, 0]).choose([a, b, c], out=None)
        assert (r == [13, 5, 3]).all()
        assert (a == [1, 2, 3]).all()
        r = array([2, 1, 0]).choose([a, b, c], out=a)
        assert (r == [13, 5, 3]).all()
        assert (a == [13, 5, 3]).all()

    def test_choose_modes(self):
        from numpypy import array
        a, b, c = array([1, 2, 3]), [4, 5, 6], 13
        raises(ValueError, "array([3, 1, 0]).choose([a, b, c])")
        raises(ValueError, "array([3, 1, 0]).choose([a, b, c], mode='raises')")
        raises(ValueError, "array([3, 1, 0]).choose([])")
        raises(ValueError, "array([-1, -2, -3]).choose([a, b, c])")
        r = array([4, 1, 0]).choose([a, b, c], mode='clip')
        assert (r == [13, 5, 3]).all()
        r = array([4, 1, 0]).choose([a, b, c], mode='wrap')
        assert (r == [4, 5, 3]).all()

    def test_choose_dtype(self):
        from numpypy import array
        a, b, c = array([1.2, 2, 3]), [4, 5, 6], 13
        r = array([2, 1, 0]).choose([a, b, c])
        assert r.dtype == float

    def test_choose_dtype_out(self):
        from numpypy import array
        a, b, c = array([1, 2, 3]), [4, 5, 6], 13
        x = array([0, 0, 0], dtype='i2')
        r = array([2, 1, 0]).choose([a, b, c], out=x)
        assert r.dtype == 'i2'
