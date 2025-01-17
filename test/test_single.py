from get_package import package
import numpy as np
import unittest


dtype = np.float64
X = np.load("./X_test.npy").astype(dtype)
Y = np.load("./Y_test.npy").astype(dtype)
N = len(Y)
lengthscale = 1.0
variance = 5.0
noise = 1e-2
# lager block size gives more accurate gradient
N_init = 500
lr = 0.5


class Test(unittest.TestCase):
    def _run(self, nGPU, kern, it):
        kernel = kern()
        kernel.set_all_ps([variance, lengthscale])
        bbmm = package.BBMM(kernel, nGPU=nGPU, file=None, verbose=False)
        bbmm.initialize(X, noise)
        bbmm.set_preconditioner(N_init, nGPU=0)
        bbmm.solve_iter(Y)
        self.assertTrue(bbmm.iter == it)
        self.assertTrue(bbmm.converged)
        err = np.max(np.abs(bbmm.get_residual()))
        self.assertTrue(err < 1e-5)

    def test_CPU_RBF(self):
        self._run(0, package.kern.RBF, 7)

    def test_GPU_RBF(self):
        self._run(1, package.kern.RBF, 7)

    def test_CPU_Matern32(self):
        self._run(0, package.kern.Matern32, 11)

    def test_GPU_Matern32(self):
        self._run(1, package.kern.Matern32, 11)

    def test_CPU_Matern52(self):
        self._run(0, package.kern.Matern52, 9)

    def test_GPU_Matern52(self):
        self._run(1, package.kern.Matern52, 9)


if __name__ == '__main__':
    unittest.main()
