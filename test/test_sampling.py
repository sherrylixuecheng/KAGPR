from get_package import package
import numpy as np
import GPy
import unittest

X = np.load("./X_test.npy")
Y = np.load("./Y_test.npy")
N = len(Y)
lengthscale = 1.0
variance = 10.0
noise = 1e-3
batch = min(4096, N)
thres = 1e-6
N_init = 500
bs = 100
BBMM_kernels = [package.kern.RBF(), package.kern.Matern32(), package.kern.Matern52()]
GPy_kernels = [GPy.kern.RBF, GPy.kern.Matern32, GPy.kern.Matern52]


class Test(unittest.TestCase):
    def _run(self, i):
        bbmm_kernel = BBMM_kernels[i]
        bbmm_kernel.set_all_ps([variance, lengthscale])
        bbmm = package.BBMM(bbmm_kernel, nGPU=1, file=None, verbose=False)
        bbmm.initialize(X, noise, batch=batch)
        bbmm.set_preconditioner(N_init, nGPU=1, debug=True)
        woodbury_vec_iter = bbmm.solve_iter(Y, thres=thres, block_size=bs, compute_gradient=True, random_seed=0, compute_loglikelihood=False, lanczos_n_iter=20, debug=False, max_iter=1000)

        gpy_kernel = GPy_kernels[i](input_dim=X.shape[1], lengthscale=lengthscale, variance=variance)
        gpy_model = GPy.models.GPRegression(X, Y, kernel=gpy_kernel, noise_var=noise)
        # 1e-6
        err_r = np.max(np.abs(gpy_kernel._scaled_dist(X) - bbmm.kernel.r(X)))
        self.assertTrue(err_r < 1e-5)
        # 1e-10
        err_K = np.max(np.abs(gpy_kernel.K_of_r(gpy_kernel._scaled_dist(X)) - bbmm.kernel.K_of_r(bbmm.kernel.r(X))))
        self.assertTrue(err_K < 1e-10)

        # 1e-6
        err_pred = np.max(np.abs(bbmm.get_residual()))
        self.assertTrue(err_pred < 1e-5)

        try:
            random_vectors = bbmm.random_vectors.get()
        except BaseException:
            random_vectors = bbmm.random_vectors
        sampled_tr_I = np.sum(bbmm.Knoise_inv.dot(random_vectors) * random_vectors, axis=0)
        tr_I = np.mean(sampled_tr_I)
        # 1e-8
        err_tr_noise = np.abs((bbmm.tr_I - tr_I) / tr_I)
        self.assertTrue(err_tr_noise < 1e-8)
        sampled_tr_dK_dl = np.sum(bbmm.Knoise_inv.dot(bbmm.dK_dl_full_np).dot(random_vectors) * random_vectors, axis=0)
        tr_dK_dl = np.mean(sampled_tr_dK_dl)
        # 1e-5
        err_tr_l = np.abs((bbmm.tr_dK_dps[1] - tr_dK_dl) / tr_dK_dl)
        self.assertTrue(err_tr_l < 1e-7)

        # 1e-4
        err_grad_variance = np.abs((bbmm.gradients[0] - gpy_model.gradient[0]) / gpy_model.gradient[0])
        self.assertTrue(err_grad_variance < 0.05)
        # 1e-2
        err_grad_lengthscale = np.abs((bbmm.gradients[1] - gpy_model.gradient[1]) / gpy_model.gradient[1])
        self.assertTrue(err_grad_lengthscale < 0.05)
        # 1e-4
        err_grad_noise = np.abs((bbmm.gradients[2] - gpy_model.gradient[2]) / gpy_model.gradient[2])
        self.assertTrue(err_grad_noise < 0.05)

    def test_RBF(self):
        self._run(0)

    def test_Matern32(self):
        self._run(1)

    def test_Matern52(self):
        self._run(2)


if __name__ == '__main__':
    unittest.main()
