import numpy as np
from get_package import package
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
        stationary_kernel = package.kern.RBF()
        stationary_kernel.set_lengthscale(lengthscale)
        stationary_kernel.set_variance(variance)
        kern = package.kern.FullDerivative(stationary_kernel, n, d)
        bbmm = package.BBMM(kern, nGPU=0, verbose=False)
        bbmm.initialize(X, noise)
        bbmm.set_preconditioner(500, nGPU=0)
        bbmm.solve_iter(Y, thres=1e-8)
        self.assertTrue(bbmm.iter <= 5)
        err = np.max(np.abs(bbmm.get_residual()))
        self.assertTrue(err < 1e-5)

    def test_gpu(self):
        stationary_kernel = package.kern.RBF()
        stationary_kernel.set_lengthscale(lengthscale)
        stationary_kernel.set_variance(variance)
        kern = package.kern.FullDerivative(stationary_kernel, n, d)
        bbmm = package.BBMM(kern, nGPU=1, verbose=False)
        bbmm.initialize(X, noise)
        bbmm.set_preconditioner(500, nGPU=0)
        bbmm.solve_iter(Y, thres=1e-8)
        self.assertTrue(bbmm.iter <= 5)
        err = np.max(np.abs(bbmm.get_residual()))
        self.assertTrue(err < 1e-5)


if __name__ == '__main__':
    unittest.main()
