'''
An implementation of Black-Box Matrix Multiplication (BBMM) to MOB-ML

Reference of Black-Box Matrix Multiplication (BBMM):
Wang, Ke Alexander, et al. "Exact Gaussian processes on a million data points." arXiv preprint arXiv:1903.08114 (2019).
Reference of block conjugate gradient algorithm:
O'Leary, Dianne P. "The block conjugate gradient algorithm and related methods." Linear algebra and its applications 29 (1980): 293-322.


Basic principle:
The key of gaussian process is to solve woodbury vector from linear system (K+\sigma^2 I) w = y.
The idea is to iteratively solve this by block conjugate gradient with an appropriate preconditioner.
A symmetric preconditioner is used to ensure the preconditioned kernel to be positive-definite.

Complexity analysis:
Let N be the number of total training points, k be the preconditioner size (~2000), s be the block size of block conjugate gradient (~50), C be the cost of constructing each kernel element (~200)
One-time cost of preparating preconditioner:
    1. Partial construction of N*k kernel: N*k*C
    2. QR of a N*k matrix. Time: N*k*k
    3. Inverse and full eigendecomposition of k*k matrix: k*k*k
In each iteration:
    1. Preconditioner transformation: N*k*s
    2. Kernel construction: N*N*C (Ideally the main cost)
    3. Matrix-Matrix Multiplication of N*N kernel and N*s iteractive vector: N*N*s
    4. Block conjugate gradient: N*s*s

Impact of parameters to convergence rate:
According to conjugate gradient, the needed number of iteraction is proportional to square root of the condition number.
The two parameters that influence the condition number are preconditioner size k (the larger the faster) and gaussian noise (the smaller the faster).
Since Using too large noise may increase the prediction error, it is recommended to determine the noise in a prior test by choosing the largest one without increasing the error too much.
Aside from the condition number, increasing block size used in block conjugate gradient increases the searching space in each iteration and also result in faster convergence.

Structure:
    _matrix_batch_CPU(method, vec), _matrix_batch_GPU(method, vec)
    mv_K(vec), mv_Knoise(vec), mv_dK_dps(i, vec)
    mv_Knoise_numpy(vec), mv_dK_dps_numpy(i, vec)
    _matrix_multiple(self, method, *vs), mv_Knoise_multiple(self, *vs), mv_Knoise_multiple(self, *vs)
'''
import typing as tp
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import numpy as np
try:
    import cupy as cp
    gpu_available = True
except BaseException:
    gpu_available = False
from .krylov import Krylov
from .preconditioner import Preconditioner_Nystroem
from .. import kern
from ..kern import Kernel
import numpy.linalg as LA
import scipy.linalg as SLA
import time
import sys
up_line = '\033[F'
clear_line = '\033[2K\033[1G'


def get_GRAM_usage():
    '''
    Return memory usage of each GPU by order
    '''
    import os
    return os.popen(r"nvidia-smi | grep MiB | grep -v % | sed 's/ /\n/g' | grep MiB").read().replace('\n', ' ')


def get_tridiagonal_matrix(d, e):
    assert len(d) == len(e) + 1
    n = len(e)
    M = np.zeros((n + 1, n + 1))
    M[np.arange(n + 1), np.arange(n + 1)] = d.copy()
    M[np.arange(0, n), np.arange(1, n + 1)] = e.copy()
    M[np.arange(1, n + 1), np.arange(0, n)] = e.copy()
    return M


def get_tridiagonal_matrix_log(d, e):
    M = get_tridiagonal_matrix(d, e)
    return SLA.logm(M)


class BBMM(object):
    def __init__(self, kernel: Kernel, nGPU: int=0, file: tp.IO=sys.stdout, verbose: bool=True) -> None:
        '''
        A general BBMM stationary kernel.
        RBF, Matern32 and Matern52 are implemented as example.
        You can defined your own kernel by inheriting this class.

        Parameters
        ----------
        X: N*f array.
        noise: scalar.
        batch: Batch size of block kernel construction in order to save memory.
        nGPU: Number of used GPUs.
        file: A file to write the print information. Default to be standand console output.
        '''
        self.file = file
        self.GPU = (nGPU > 0)
        self.nGPU = nGPU
        self.verbose = verbose
        if self.GPU:
            import cupy as cp
            self.cnp = cp
        else:
            self.cnp = np
        self.kernel = kernel
        self.kernel.set_cache_state(False)

    def initialize(self, X: np.ndarray, noise: float, batch: int=4096) -> None:
        # batch=None: no batch, else, batch=min(N, batch)
        # initialize bbmm

        self.X_CPU = X.copy()
        if not self.GPU:
            self.X: tp.Any = self.X_CPU
        else:
            self.X = []
            for i in range(self.nGPU):
                with cp.cuda.Device(i):
                    self.X.append(cp.asarray(X))
            cp.cuda.Stream.null.synchronize()
        self.dtype = X.dtype
        self.N = len(X)
        self.batch = batch
        self.N_out = self.N * self.kernel.nout
        if self.batch is not None:
            self.batch = min(self.batch // self.kernel.nout, self.N)
        self.noise = noise
        assert len(self.kernel.likelihood_split(self.N)) == 1

        if self.batch is None:
            assert not self.GPU
            self.K_full = self.kernel.K(self.X, self.X)
            self.dK_dps_full = [self.kernel.dK_dps[i](self.X, self.X) for i in range(len(self.kernel.ps))]
        else:
            self.division = np.split(np.arange(self.N), range(self.batch, self.N, self.batch))
            self.division_out = []
            for i in range(len(self.division)):
                self.division_out.append(np.concatenate([self.division[i] + j * self.N for j in range(self.kernel.nout)]))
            self.n_division = len(self.division)

        self.iter = 0
        self.total_time_Kx = 0.0
        self.total_time_pred = 0.0
        self.total_time_CG = 0.0
        self.GRAM_printed = False

    def _matrix_batch_CPU(self, method, vec):
        '''
        Calculate the matrix matrix multiplication of the symmetric kernal and a given matrix by block on CPU.

        Parameters
        ----------
        vec: N*s numpy array.
        '''
        result = np.zeros_like(vec)
        for i in range(self.n_division):
            for j in range(self.n_division):
                x = self.division[i]
                y = self.division[j]
                x_out = self.division_out[i]
                y_out = self.division_out[j]
                # Only calculate the lower triangular block
                if i <= j:
                    t1 = time.time()
                    K_block = method(self.X[x], self.X[y])
                    result[x_out] += K_block.dot(vec[y_out])
                    if i < j:
                        result[y_out] += K_block.T.dot(vec[x_out])
                    t2 = time.time()
                    self.total_time_Kx += t2 - t1
        return result

    def _matrix_batch_GPU(self, method, vec):
        '''
        Calculate the matrix matrix multiplication of the symmetric kernal and a given matrix by block on GPU.

        Parameters
        ----------
        vec: N*s cupy array.
        '''
        assert vec.device.id == 0
        cp.cuda.Stream.null.synchronize()
        t1 = time.time()
        batches = [None for i in range(self.nGPU)]
        vecs = [None for i in range(self.nGPU)]
        for i in range(self.nGPU):
            with cp.cuda.Device(i):
                batches[i] = cp.zeros(vec.shape)
                vecs[i] = cp.asarray(vec)
        k = 0
        for i in range(self.n_division):
            for j in range(self.n_division):
                x = self.division[i]
                y = self.division[j]
                x_out = self.division_out[i]
                y_out = self.division_out[j]
                if i <= j:
                    # Only calculate the lower triangular block
                    # distribute calculation to multiple devices
                    device_index = k % self.nGPU
                    with cp.cuda.Device(device_index):
                        K_block = method(self.X[device_index][x], self.X[device_index][y])
                        batches[device_index][x_out] += K_block.dot(vecs[device_index][y_out])
                        if i < j:
                            batches[device_index][y_out] += K_block.T.dot(vecs[device_index][x_out])
                    k += 1
        with cp.cuda.Device(0):
            for i in range(self.nGPU):
                batches[i] = cp.asarray(batches[i])
            result = cp.sum(cp.array(batches), axis=0)
        cp.cuda.Stream.null.synchronize()
        t2 = time.time()
        self.total_time_Kx += t2 - t1
        return result

    def mv_K(self, vec):
        '''
        Calculate the matrix matrix multiplication of the symmetric kernal and a given matrix by block on GPU.

        Parameters
        ----------
        vec: N*s numpy or cupy array.
        '''
        self.iter += 1
        if self.batch is None:
            return self.K_full.dot(vec)
        else:
            if self.GPU:
                return self._matrix_batch_GPU(self.kernel.K, vec)
            else:
                return self._matrix_batch_CPU(self.kernel.K, vec)

    def mv_Knoise(self, vec):
        '''
        Calculate the matrix matrix multiplication of the symmetric kernal and a given matrix by block on GPU.

        Parameters
        ----------
        vec: N*s numpy or cupy array.
        '''
        return self.mv_K(vec) + vec * self.noise

    def mv_dK_dps(self, i, vec):
        '''
        Calculate the matrix matrix multiplication of the symmetric kernal and a given matrix by block on GPU.

        Parameters
        ----------
        vec: N*s numpy or cupy array.
        '''
        self.iter += 1
        if self.batch is None:
            return self.dK_dps_full[i].dot(vec)
        else:
            if self.GPU:
                return self._matrix_batch_GPU(self.kernel.dK_dps[i], vec)
            else:
                return self._matrix_batch_CPU(self.kernel.dk_dps[i], vec)

    def mv_Knoise_numpy(self, vec):
        '''
        A numpy wrapper of GPU kernel matrix matrix multiplication

        Parameters
        ----------
        vec: N*s numpy array.
        '''
        if self.GPU:
            with cp.cuda.Device(0):
                return cp.asnumpy(self.mv_Knoise(cp.asarray(vec)))
        else:
            return self.mv_Knoise(vec)

    def mv_dK_dps_numpy(self, i, vec):
        '''
        A numpy wrapper of GPU kernel matrix matrix multiplication

        Parameters
        ----------
        vec: N*s numpy array.
        '''
        if self.GPU:
            with cp.cuda.Device(0):
                return cp.asnumpy(self.mv_dK_dps(i, cp.asarray(vec)))
        else:
            return self.mv_dK_dps(i, vec)

    def _matrix_multiple(self, method, *vs):
        if gpu_available:
            xp = cp.get_array_module(vs[0])
        else:
            xp = np
        lengths = np.array([v.shape[1] for v in vs])
        cumsum = np.cumsum(lengths)[:-1].tolist()
        result = method(xp.concatenate(vs, axis=1))
        return xp.split(result, cumsum, axis=1)

    def mv_Knoise_multiple(self, *vs):
        return self._matrix_multiple(self.mv_Knoise, *vs)

    def mv_Knoise_numpy_multiple(self, *vs):
        return self._matrix_multiple(self.mv_Knoise_numpy, *vs)

    def save(self, path: str) -> None:
        if self.GPU:
            data = {
                'kernel': self.kernel.to_dict(),
                'X': cp.asnumpy(self.X[0]),
                'Y': cp.asnumpy(self.Y),
                'w': cp.asnumpy(self.w),
                'noise': self.noise,
            }
        else:
            data = {
                'kernel': self.kernel.to_dict(),
                'X': self.X,
                'Y': self.Y,
                'w': self.w,
                'noise': self.noise,
            }
        np.savez(path, **data)

    @classmethod
    def from_dict(self, data: tp.Dict[str, tp.Any], GPU: bool) -> BBMM:
        kernel_dict = data['kernel'][()]
        kernel = kern.get_kern_obj(kernel_dict)
        if GPU:
            nGPU = 1
        else:
            nGPU = 0
        result = self(kernel, nGPU=nGPU)
        result.initialize(data['X'], data['noise'][()])
        if GPU:
            result.w = cp.asarray(data['w']).copy()
        else:
            result.w = data['w']
        return result

    @classmethod
    def load(self, path: str, GPU: bool) -> BBMM:
        data = dict(np.load(path, allow_pickle=True))
        return self.from_dict(data, GPU)

    def predict(self, X2: np.ndarray, training: bool=False) -> np.ndarray:
        if self.GPU:
            result = self.kernel.K(cp.asarray(X2), self.X[0]).dot(self.w)
            if training:
                result += self.w * self.noise
            result = cp.asnumpy(result)
        else:
            result = self.kernel.K(X2, self.X).dot(self.w)
            if training:
                result += self.w * self.noise
        return result

    def set_preconditioner(self, N_init: int, indices: np.ndarray=None, debug: bool=False, nGPU: int=0, random_seed: int=0) -> None:
        '''
        Construct a Nystroem preconditioner by M = K21 (K11 + \sigma^2 I)^{-1} K12 + \sigma^2 I

        Parameters
        ----------
        N_init: int. Preconditioner size.
        indices: vector of size k. An array of indices used as Nystroem kernel approximation.
        nGPU: int. Number of GPUs for preconditioner.
        '''
        if self.verbose:
            print("Start constructing Nystroem preconditioner:", file=self.file, flush=True)
        t1 = time.time()
        if self.verbose:
            print("Total size:", self.N, file=self.file, flush=True)
            print("Preconditioner size:", N_init, file=self.file, flush=True)
        if nGPU == 0:
            if self.verbose:
                print("Preconditioner on CPU", file=self.file, flush=True)
            self.main_GPU = False
        else:
            if self.verbose:
                print("Preconditioner on GPU", file=self.file, flush=True)
            self.main_GPU = True
        np.random.seed(random_seed)
        if indices is None:
            init_indices = np.random.permutation(self.N)[0:N_init]
        else:
            init_indices = indices.copy()
        init_indices = np.sort(init_indices)
        K11 = self.kernel.K(self.X_CPU[init_indices])
        if self.verbose:
            print("Constructing K21", file=self.file, flush=True)
        K21 = self.kernel.K(self.X_CPU, self.X_CPU[init_indices])
        I = np.eye(len(K11), dtype=self.dtype)
        if self.verbose:
            print("Running QR", file=self.file, flush=True)
        # K21 = Q R
        Q, R = LA.qr(K21)
        # M = Q R (K11 + \sigma^2 I)^{-1} R^T Q^t
        K_core = R.dot(LA.inv(K11 + I * self.noise)).dot(R.T)
        K_core = (K_core + K_core.T) / 2
        if not debug:
            del K21, K11, R
        if self.verbose:
            print("Calculating eigenvectors", file=self.file, flush=True)
        eigvals, eigvecs = LA.eigh(K_core)
        U = Q.dot(eigvecs)
        # M = U \Lambda U^T
        self.pred_nystroem = Preconditioner_Nystroem(eigvals + self.noise, U, self.noise, nGPU)
        t2 = time.time()
        if self.verbose:
            print("Preconditioner done. Time:", t2 - t1, file=self.file, flush=True)
        if debug:
            self.init_indices = init_indices
            self.K11 = K11
            self.I = I
            self.K21 = K21
            self.K_core = K_core
            self.K_core_direct = R.dot(LA.inv(K11 + I * self.noise)).dot(R.T)
            self.eigvals = eigvals
            self.eigvecs = eigvecs
            self.U = U
            self.Q = Q
            self.R = R
            if self.GPU:
                self.K_full_np = cp.asnumpy(self.kernel.K(self.X[0], self.X[0]))
                self.dK_dl_full_np = cp.asnumpy(self.kernel.dK_dps[1](self.X[0], self.X[0]))
            else:
                self.K_full_np = self.kernel.K(self.X, self.X)
                self.dK_dl_full_np = self.kernel.dK_dps[1](self.X, self.X)
            self.K_guess = K21.dot(LA.inv(self.K11 + I * self.noise)).dot(K21.T)
            self.err_K_guess_qr = self.Q.dot(self.K_core).dot(self.Q.T) - self.K_guess
            if self.verbose:
                print("Error of K_guess after QR:", np.max(np.abs(self.err_K_guess_qr)) / self.kernel.ps[0].value)
            self.invhalf_eigvals = 1 / np.sqrt(self.eigvals + self.noise) - 1 / np.sqrt(self.noise)
            # Some numerical issues here
            self.K_pred_invhalf = self.U.dot(np.diag(self.invhalf_eigvals)).dot(self.U.T) + np.eye(self.N_out) / np.sqrt(self.noise)
            self.err_eigdecomposition = self.K_pred_invhalf.dot(self.K_guess + np.eye(self.N_out) * self.noise).dot(self.K_pred_invhalf) - np.eye(self.N_out)
            if self.verbose:
                print("Error of Eigenvalue Decomposition:", np.max(np.abs(self.err_eigdecomposition)) / self.kernel.ps[0].value)
            self.K_pred = self.K_pred_invhalf.dot(self.K_full_np + np.eye(self.N_out) * self.noise).dot(self.K_pred_invhalf)
            self.eigvals_K_pred = LA.eigvalsh(self.K_pred)
            if self.verbose:
                print("Condition Number:", np.max(self.eigvals_K_pred) / np.min(self.eigvals_K_pred))
            self.Knoise = self.K_full_np + np.eye(self.N_out) * self.noise
            self.Knoise_inv = LA.inv(self.Knoise)
            #self.logKnoise = SLA.logm(self.Knoise)

    def mv_preconditioned_Knoise(self, v, l=None):
        '''
        Perform M^{-1/2} A M^{-1/2} v

        Parameters
        ----------
        v: N*s numpy array.
        '''
        result = v

        t1 = time.time()
        result = self.pred_nystroem.mv_invhalf(result)
        t2 = time.time()
        self.total_time_pred += t2 - t1

        if l is not None:
            if self.main_GPU:
                result, result_l = self.mv_Knoise_multiple(result, l)
            else:
                result, result_l = self.mv_Knoise_numpy_multiple(result, l)
        else:
            if self.main_GPU:
                result = self.mv_Knoise(result)
            else:
                result = self.mv_Knoise_numpy(result)

        t3 = time.time()
        result = self.pred_nystroem.mv_invhalf(result)
        t4 = time.time()
        self.total_time_pred += t4 - t3

        if l is not None:
            return result, result_l
        else:
            return result

    def callback(self, i, residual, t_cg):
        '''
        A callback function used for block conjugate gradient

        Parameters
        ----------
        i: int. Number of finished iterations.
        residual: scalar.
        t_cg: Time spent on block_conjugate gradient.
        '''
        self.total_time_CG += t_cg
        if self.verbose:
            if self.GPU and (not self.GRAM_printed):
                print("GPU Memory usage:", get_GRAM_usage(), file=self.file, flush=True)
                self.GRAM_printed = True
            print("Iter", i, "residual: %12.8f" % (residual,), flush=True, file=self.file)

    def solve_iter(self, Y: np.ndarray, x0: np.ndarray=None, block_size: int=50, thres: float=1e-6, compute_gradient: bool=False, random_seed: int=0, compute_loglikelihood: bool=None, lanczos_n_iter: int=20, debug: bool=False, max_iter: int=None):
        '''
        Solve the preconditioned kernel linear system iteratively by block conjugate gradient
        Equation: M^{-1/2} A M^{-1/2} M^{1/2} v = M^{-1/2} y

        Parameters
        ----------
        Y: N*s numpy array.
        block_size: int.
        thres: scalar. Convergence threshold.
        '''
        self.Y = Y.copy()
        if x0 is not None:
            return self.solve_iter(Y - self.mv_Knoise_numpy(x0), block_size=block_size, thres=thres, compute_gradient=compute_gradient, random_seed=random_seed, compute_loglikelihood=compute_loglikelihood, lanczos_n_iter=lanczos_n_iter, debug=debug) + x0
        if self.main_GPU:
            xp = cp
            cp.cuda.Device(0).use()
            self.Y = cp.asarray(self.Y)
        else:
            xp = np
        # M^{-1/2} b
        np.random.seed(random_seed)
        self.random_vectors = np.random.normal(size=(len(Y), block_size))
        if compute_loglikelihood:
            self.lanczos_vectors = self.random_vectors.copy()
            if self.main_GPU:
                self.lanczos_vectors = cp.asarray(self.lanczos_vectors)
            self.lanczos_vectors /= xp.linalg.norm(self.lanczos_vectors, axis=0)
        else:
            self.lanczos_vectors = None
        area_Y = slice(0, 1)
        area_I = slice(1, block_size + 1)
        nps = len(self.kernel.ps)
        area_dL_dps = [slice(block_size * (1 + i) + 1, block_size * (2 + i) + 1) for i in range(nps)]
        #area_dL_dl = slice(block_size + 1, block_size * 2 + 1)
        if compute_gradient:
            self.block_Y = np.zeros((len(Y), block_size * (1 + nps) + 1))
            self.block_Y[:, area_Y] = Y.copy()
            self.block_Y[:, area_I] = self.random_vectors.copy()
            for i in range(nps):
                self.block_Y[:, area_dL_dps[i]] = self.mv_dK_dps_numpy(i, self.random_vectors)
            #self.mv_dK_dl_random_vectors = self.mv_dK_dps_numpy(1, self.random_vectors)
            #self.block_Y[:, area_dL_dl] = self.mv_dK_dl_random_vectors.copy()
        else:
            self.block_Y = np.zeros((len(Y), block_size + 1))
            self.block_Y[:, area_Y] = Y.copy()
            self.block_Y[:, area_I] = self.random_vectors.copy()
        if self.main_GPU:
            self.block_Y = xp.array(self.block_Y)
        self.Y_transform = self.pred_nystroem.mv_invhalf(self.block_Y)
        t1 = time.time()
        if self.verbose:
            print("Start iteractive solver with block size", block_size, "and threshold", thres, file=self.file, flush=True)
            #print("\n\n\n\n", file=self.file, flush=True, end='')
        self.krylov = Krylov(self.mv_preconditioned_Knoise, self.Y_transform, thres=thres, callback=self.callback, lanczos_vectors=self.lanczos_vectors, lanczos_n_iter=lanczos_n_iter, debug=debug, max_iter=max_iter)

        if compute_loglikelihood:
            self.solution, self.d, self.e = self.krylov.run()
        else:
            self.solution = self.krylov.run()
        self.converged = self.krylov.bcg_converged
        t2 = time.time()
        if self.verbose:
            print("Iterative solving done. Time:", t2 - t1, file=self.file, flush=True)
            print("Total time spent on CG:", self.total_time_CG, file=self.file, flush=True)
            print("Total time spent on kernel MMM:", self.total_time_Kx, file=self.file, flush=True)
            print("Total time spent on preconditioner transformation:", self.total_time_pred, file=self.file, flush=True)

        if compute_gradient:
            real_solution = self.pred_nystroem.mv_invhalf(self.solution)
            real_solution = cp.asnumpy(real_solution)
            woodbury_vec_iter = real_solution[:, area_Y].copy()
            woodbury_vec_I = real_solution[:, area_I].copy()
            #woodbury_vec_dK_dl = real_solution[:, area_dL_dl]
            woodbury_vec_dK_dps = [real_solution[:, area_dL_dps[i]] for i in range(nps)]
            self.tr_I = np.sum(woodbury_vec_I * self.random_vectors) / block_size
            self.tr_dK_dps = [np.sum(woodbury_vec_dK_dps[i] * self.random_vectors) / block_size for i in range(nps)]
            dK_dps_dot_woodbury_vec = [self.mv_dK_dps_numpy(i, woodbury_vec_iter) for i in range(nps)]
            dL_dps = [woodbury_vec_iter.T.dot(dK_dps_dot_woodbury_vec[i])[0, 0] / 2 - self.tr_dK_dps[i] / 2 for i in range(nps)]
            dL_dnoise = woodbury_vec_iter.T.dot(woodbury_vec_iter)[0, 0] / 2 - self.tr_I / 2
            self.gradients = dL_dps + [dL_dnoise]
            #old = True
            #if old:
            #    Knoise_dot_woodbury_vec = self.mv_Knoise_numpy(woodbury_vec_iter)
            #    K_dot_woodbury_vec = Knoise_dot_woodbury_vec - woodbury_vec_iter * self.noise
            #    dL_dl = dL_dps[1]
            #    dL_dv = (woodbury_vec_iter.T.dot(K_dot_woodbury_vec)[0, 0] / 2 - (len(Y) - self.tr_I * self.noise) / 2) / self.kernel.ps[0]
            #    self.gradients = LL_gradient(dL_dv, dL_dl, dL_dnoise)
        else:
            real_solution = self.pred_nystroem.mv_invhalf(self.solution)
            real_solution = cp.asnumpy(real_solution)
            woodbury_vec_iter = real_solution[:, area_Y].copy()
        if compute_loglikelihood:
            self.logdets_samples = xp.array([get_tridiagonal_matrix_log(d, e)[0, 0] for d, e in zip(cp.asnumpy(self.d.T), cp.asnumpy(self.e.T))])
            self.logdet = xp.mean(self.logdets_samples) * len(Y)
            self.log_likelihood = -self.logdet / 2 - woodbury_vec_iter.T.dot(Y)[0, 0] / 2 - np.log(np.pi * 2) * len(Y) / 2
        if self.GPU:
            self.w = cp.asarray(woodbury_vec_iter).copy()
        else:
            self.w = woodbury_vec_iter
        return woodbury_vec_iter