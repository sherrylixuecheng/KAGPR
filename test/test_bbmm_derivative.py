import numpy as np
import BBMM
import unittest

np.random.seed(0)
lengthscale = 1.0 + np.random.random()
variance = 1.0 + np.random.random()
noise = 1e-4


n = 3
d = 2
N = 1000


def y(X):
    Y = np.sum(np.sin(X[:, 0:d]), axis=1)
    Y_grad = [np.sum(X[:, d * (i + 1):d * (i + 2)] * np.cos(X[:, d * (i + 1):d * (i + 2)]), axis=1) for i in range(n)]
    return np.concatenate([Y] + Y_grad)[:, None]


np.random.seed(0)
X = np.random.random((N, (n + 1) * d))
Y = y(X)


class Test(unittest.TestCase):
    def test_cpu(self):
        stationary_kernel = BBMM.kern.RBF()
        stationary_kernel.set_lengthscale(lengthscale)
        stationary_kernel.set_variance(variance)
        kern = BBMM.kern.FullDerivative(stationary_kernel, n, d)
        bbmm = BBMM.BBMM(kern, nGPU=0, nout=n + 1, verbose=False)
        bbmm.initialize(X, noise)
        bbmm.set_preconditioner(500, nGPU=0)
        w_bbmm = bbmm.solve_iter(Y)

        kern2 = BBMM.kern.FullDerivative(stationary_kernel, n, d)
        gp = BBMM.GP(X, Y, kern2, noise)
        gp.fit()
        err = np.max(np.abs(gp.w - w_bbmm))
        self.assertTrue(err < 1e-3)

    def test_gpu(self):
        stationary_kernel = BBMM.kern.RBF()
        stationary_kernel.set_lengthscale(lengthscale)
        stationary_kernel.set_variance(variance)
        kern = BBMM.kern.FullDerivative(stationary_kernel, n, d)
        bbmm = BBMM.BBMM(kern, nGPU=1, nout=n + 1, verbose=False)
        bbmm.initialize(X, noise)
        bbmm.set_preconditioner(500, nGPU=0)
        w_bbmm = bbmm.solve_iter(Y)

        kern2 = BBMM.kern.FullDerivative(stationary_kernel, n, d)
        gp = BBMM.GP(X, Y, kern2, noise)
        gp.fit()
        err = np.max(np.abs(gp.w - w_bbmm))
        self.assertTrue(err < 1e-3)


if __name__ == '__main__':
    unittest.main()
