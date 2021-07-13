import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import numpy as np
try:
    import cupy as cp
    gpu_available = True
except BaseException:
    gpu_available = False
    xp = np
from .kernel import Kernel
from .cache import Cache


class Stationary(Kernel):
    def __init__(self):
        super().__init__()
        self.default_cache = {'g': 0}

    def set_lengthscale(self, lengthscale):
        self.lengthscale = lengthscale

    def set_variance(self, variance):
        self.variance = variance

    def K_of_r(self, r):
        raise NotImplementedError

    def dK_dr(self, r):
        raise NotImplementedError

    def d2K_drdr(self, r):
        raise NotImplementedError

    def d3K_drdrdr(self, r):
        raise NotImplementedError

    @Cache('g')
    def r(self, X1, X2=None):
        '''
        A numpy-cupy generic code to calculate the distance matrix between X1 and X2.

        Parameters
        ----------
        X1: N*f array.
        X2: None or N*f array. X2=X1 if None is specified.
        '''
        if gpu_available:
            xp = cp.get_array_module(X1)
        if X2 is None:
            X2 = X1
        N1 = len(X1)
        N2 = len(X2)
        # (X1-X2)^2 = X1^2 + X2^2 - 2*X1*X2
        distance = xp.empty((N1, N2))
        X11 = xp.square(X1).sum(1)
        X22 = xp.square(X2).sum(1)
        distance = X11[:, None] + X22[None, :]
        distance -= X1.dot(X2.T) * 2
        # Add a small number to avoid calculating square root of a negative number
        distance += 1e-12
        return xp.sqrt(distance) / self.lengthscale

    @Cache('g')
    def K(self, X1, X2=None):
        r = self.r(X1, X2)
        return self.K_of_r(r)

    @Cache('gd1')
    def Xdiff_dX(self, X1, X2, dX1):
        if gpu_available:
            xp = cp.get_array_module(X1)
        return xp.sum((X1[:, None, :] - X2[None, :, :]) * dX1[:, None, :], axis=-1)

    @Cache('gd2')
    def Xdiff_dX2(self, X1, X2, dX2):
        if gpu_available:
            xp = cp.get_array_module(X1)
        return xp.sum((X2[None, :, :] - X1[:, None, :]) * dX2[None, :, :], axis=-1)

    @Cache('gd1')
    def dr_dX(self, X1, X2, dX1, r):
        if X2 is None:
            X2 = X1
        return self.Xdiff_dX(X1, X2, dX1) / r / self.lengthscale ** 2

    @Cache('gd2')
    def dr_dX2(self, X1, X2, dX2, r):
        if X2 is None:
            X2 = X1
        return self.Xdiff_dX2(X1, X2, dX2) / r / self.lengthscale ** 2

    @Cache('gdd')
    def d2r_dXdX2(self, X1, X2, dX1, dX2, r):
        if X2 is None:
            X2 = X1
        return (-self.dr_dX(X1, X2, dX1, r) * self.dr_dX2(X1, X2, dX2, r) - dX1.dot(dX2.T) / self.lengthscale ** 2) / r
        #return 0

    @Cache('g')
    def dr_dl(self, r):
        return -r / self.lengthscale

    @Cache('g')
    def dK_dl(self, X1, X2=None):
        r = self.r(X1, X2)
        return self.dK_dr(r) * self.dr_dl(r)

    @Cache('g')
    def d2K_drdl(self, r):
        return self.d2K_drdr(r) * self.dr_dl(r) - self.dK_dr(r) / self.lengthscale

    @Cache('g')
    def d3K_drdrdl(self, r):
        return self.d3K_drdrdr(r) * self.dr_dl(r) - self.d2K_drdr(r) / self.lengthscale * 2

    @Cache('g')
    def dK_dv(self, X1, X2=None):
        return self.K(X1, X2) / self.variance

    @Cache('g')
    def d2K_drdv(self, r):
        return self.dK_dr(r) / self.variance

    @Cache('g')
    def d3K_drdrdv(self, r):
        return self.d2K_drdr(r) / self.variance

    # Start fake methods
    def _fake_dK_dX(self, method1, X1, dX1, X2=None):
        r = self.r(X1, X2)
        return method1(r) * self.dr_dX(X1, X2, dX1, r)

    def _fake_dK_dX2(self, method1, X1, dX2, X2=None):
        r = self.r(X1, X2)
        return method1(r) * self.dr_dX2(X1, X2, dX2, r)

    def _fake_d2K_dXdX2(self, method1, method2, X1, dX1, dX2, X2=None):
        r = self.r(X1, X2)
        return method2(r) * self.dr_dX(X1, X2, dX1, r) * self.dr_dX2(X1, X2, dX2, r) + method1(r) * self.d2r_dXdX2(X1, X2, dX1, dX2, r)

    # Start K
    @Cache('gd1')
    def dK_dX(self, X1, dX1, X2=None):
        return self._fake_dK_dX(self.dK_dr, X1, dX1, X2=X2)

    @Cache('dg2')
    def dK_dX2(self, X1, dX2, X2=None):
        return self._fake_dK_dX2(self.dK_dr, X1, dX2, X2=X2)

    @Cache('gdd')
    def d2K_dXdX2(self, X1, dX1, dX2, X2=None):
        return self._fake_d2K_dXdX2(self.dK_dr, self.d2K_drdr, X1, dX1, dX2, X2=X2)

    # Start dK_dl
    @Cache('gd1')
    def d2K_dXdl(self, X1, dX1, X2=None):
        return self._fake_dK_dX(self.d2K_drdl, X1, dX1, X2=X2)

    @Cache('dg2')
    def d2K_dX2dl(self, X1, dX2, X2=None):
        return self._fake_dK_dX2(self.d2K_drdl, X1, dX2, X2=X2)

    @Cache('dgg')
    def d3K_dXdX2dl(self, X1, dX1, dX2, X2=None):
        return self._fake_d2K_dXdX2(self.d2K_drdl, self.d3K_drdrdl, X1, dX1, dX2, X2=X2)

    # Start dK_dv
    @Cache('dg1')
    def d2K_dXdv(self, X1, dX1, X2=None):
        return self._fake_dK_dX(self.d2K_drdv, X1, dX1, X2=X2)

    @Cache('dg2')
    def d2K_dX2dv(self, X1, dX2, X2=None):
        return self._fake_dK_dX2(self.d2K_drdv, X1, dX2, X2=X2)

    @Cache('dgg')
    def d3K_dXdX2dv(self, X1, dX1, dX2, X2=None):
        return self._fake_d2K_dXdX2(self.d2K_drdv, self.d3K_drdrdv, X1, dX1, dX2, X2=X2)

    def K_0(self, dX):
        if gpu_available:
            xp = cp.get_array_module(dX)
        return xp.ones((dX.shape[0],)) * self.variance

    def d2K_dXdX_0(self, dX):
        if gpu_available:
            xp = cp.get_array_module(dX)
        return -xp.sum(dX**2, axis=1) * self.dK_dR0_0() * 2

    def dK_dl_0(self, dX):
        if gpu_available:
            xp = cp.get_array_module(dX)
        return xp.zeros((dX.shape[0],))

    def d3K_dldXdX_0(self, dX):
        return - self.d2K_dXdX_0(dX) * 2 / self.lengthscale

    def clear_cache(self):
        self.cache_data = {}

    def to_dict(self):
        data = {
            'lengthscale': self.lengthscale,
            'variance': self.variance,
            'name': self.name
        }
        return data

    @classmethod
    def from_dict(self, data):
        kernel = self()
        kernel.set_lengthscale(data['lengthscale'])
        kernel.set_variance(data['variance'])
        return kernel


class RBF(Stationary):
    '''
    A RBF BBMM kernel.

    Parameters
    ----------
    X: N*f array.
    lengthscale: scalar.
    variance: scalar.
    noise: scalar.
    batch: Batch size of block kernel construction in order to save memory.
    nGPU: Number of used GPUs.
    file: A file to write the print information. Default to be standand console output.
    '''

    def __init__(self):
        super().__init__()
        self.name = 'kern.RBF'

    @Cache('g')
    def K_of_r(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        return xp.exp(-r**2 / 2) * self.variance

    @Cache('g')
    def dK_dr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        return -xp.exp(-r**2 / 2) * r * self.variance

    @Cache('g')
    def d2K_drdr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        return xp.exp(-r**2 / 2) * (r**2 - 1) * self.variance

    @Cache('g')
    def d3K_drdrdr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        return xp.exp(-r**2 / 2) * (3 - r**2) * r * self.variance

    def dK_dR0_0(self):
        return -0.5 / self.lengthscale ** 2 * self.variance


class Matern32(Stationary):
    '''
    A Matern32 BBMM kernel.

    Parameters
    ----------
    X: N*f array.
    lengthscale: scalar.
    variance: scalar.
    noise: scalar.
    batch: Batch size of block kernel construction in order to save memory.
    nGPU: Number of used GPUs.
    file: A file to write the print information. Default to be standand console output.
    '''

    def __init__(self):
        super().__init__()
        self.name = 'kern.Matern32'

    @Cache('g')
    def K_of_r(self, r):
        xp = cp.get_array_module(r)
        s3 = xp.sqrt(3.)
        return (1. + s3 * r) * xp.exp(-s3 * r) * self.variance

    @Cache('g')
    def dK_dr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        s3 = xp.sqrt(3.)
        return - 3 * r * xp.exp(-s3 * r) * self.variance

    @Cache('g')
    def d2K_drdr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        s3 = xp.sqrt(3.)
        return (s3 * r - 1) * 3 * xp.exp(-s3 * r) * self.variance

    @Cache('g')
    def d3K_drdrdr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        s3 = xp.sqrt(3.)
        return (s3 * 2 - r * 3) * 3 * xp.exp(-s3 * r) * self.variance

    def dK_dR0_0(self):
        return -1.5 / self.lengthscale ** 2 * self.variance


class Matern52(Stationary):
    '''
    A Matern52 BBMM kernel.

    Parameters
    ----------
    X: N*f array.
    lengthscale: scalar.
    variance: scalar.
    noise: scalar.
    batch: Batch size of block kernel construction in order to save memory.
    nGPU: Number of used GPUs.
    file: A file to write the print information. Default to be standand console output.
    '''

    def __init__(self):
        super().__init__()
        self.name = 'kern.Matern52'

    @Cache('g')
    def K_of_r(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        s5 = xp.sqrt(5)
        return (1 + s5 * r + 5. / 3 * r**2) * xp.exp(-s5 * r) * self.variance

    @Cache('g')
    def dK_dr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        s5 = xp.sqrt(5)
        return (- 5.0 / 3 * r - 5. * s5 / 3 * r**2) * xp.exp(-s5 * r) * self.variance

    @Cache('g')
    def d2K_drdr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        s5 = xp.sqrt(5)
        return (-1 - s5 * r + 5. * r**2) * 5 / 3 * xp.exp(-xp.sqrt(5.) * r) * self.variance

    @Cache('g')
    def d3K_drdrdr(self, r):
        if gpu_available:
            xp = cp.get_array_module(r)
        s5 = xp.sqrt(5)
        return (3 * r - s5 * r**2) * 25 / 3 * xp.exp(-xp.sqrt(5.) * r) * self.variance

    def dK_dR0_0(self):
        return -5.0 / 6 / self.lengthscale ** 2 * self.variance