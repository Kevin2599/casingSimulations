import numpy as np
import scipy.sparse as sp
import properties
import json
import h5py
from ipywidgets import widgets
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

from SimPEG import Mesh, Utils, Maps
from SimPEG.EM import FDEM, TDEM, mu_0

from pymatsolver import PardisoSolver as Solver

# set a nice colormap
plt.set_cmap(plt.get_cmap('viridis'))


##############################################################################
#                                                                            #
#                           Simulation Parameters                            #
#                                                                            #
##############################################################################


# Parameters to set up the model
class CasingProperties(properties.HasProperties):

    # Conductivities
    sigmaair = properties.Float(
        "conductivity of the air (S/m)",
        default=1e-8
    )

    sigmaback = properties.Float(
        "conductivity of the background (S/m)",
        default=1e-2
    )

    sigmalayer = properties.Float(
        "conductivity of the layer (S/m)",
        default=1e-2
    )

    sigmacasing = properties.Float(
        "conductivity of the casing (S/m)",
        default=5.5e6
    )

    sigmainside = properties.Float(
        "conductivity of the fluid inside the casing (S/m)",
        default=1.
    )

    # Magnetic Permeability
    muModels = properties.Array(
        "permeability of the casing",
        default=[1., 50., 100., 200.],
        dtype = float
    )

    # Casing Geometry
    casing_top = properties.Float(
        "top of the casing (m)",
        default=0.
    )
    casing_l = properties.Float(
        "length of the casing (m)",
        default=1000
    )

    casing_d = properties.Float(
        "diameter of the casing (m)",
        default=10e-2
    ) # 10cm diameter

    casing_t = properties.Float(
        "thickness of the casing (m)",
        default=1e-2
    ) # 1cm thickness

    # Layer Geometry
    layer_z = properties.Array(
        "z-limits of the layer",
        shape=(2,),
        default=np.r_[-1000., -900.]
    )

    freqs = properties.Array(
        "source frequencies",
        default=np.r_[1e-4, 1e-3, 1e-2, 1e-1, 0.5, 1],
        dtype=float
    )

    dsz = properties.Float(
        "down-hole z-location for the source",
        default=-950.
    )

    src_b = properties.Array(
        "B electrode location",
        default=np.r_[1e4, 0., 0.]
    )

    @property
    def src_a(self):
        return np.r_[0., 0., self.dsz]

    # useful quantities to work in
    @property
    def casing_r(self):
        return self.casing_d/2.

    @property
    def casing_a(self):
        return self.casing_r - self.casing_t/2.  # inner radius

    @property
    def casing_b(self):
        return self.casing_r + self.casing_t/2.  # outer radius

    @property
    def casing_z(self):
        return np.r_[-self.casing_l, 0.] + self.casing_top


# Build the mesh
class CasingMesh(properties.HasProperties):

    # X-direction of the mesh
    csx1 = properties.Float(
        "finest cells in the x-direction", default=2.5e-3
    )
    csx2 = properties.Float(
        "second uniform cell region in x-direction", default=25.
    )
    pfx1 = properties.Float(
        "padding factor to pad from csx1 to csx2", default=1.3
    )
    pfx2 = properties.Float(
        "padding factor to pad to infinity", default=1.5
    )
    dx2 = properties.Float(
        "domain extent for uniform cell region", default=1000.
    )
    npadx2 = properties.Integer(
        "number of padding cells required to get to infinity!", default=23
    )

    # Z-direction of mesh
    csz = properties.Float(
        "cell size in the z-direction", default=0.05
    )
    nza = properties.Integer(
        "number of fine cells above the air-earth interface", default=10
    )
    pfz = properties.Float(
        "padding factor in the z-direction", default=1.5
    )
    npadzu = properties.Integer(
        "number of padding cells going up (positive z)", default=38
    )
    npadzd = properties.Integer(
        "number of padding cells going down (negative z)", default=38
    )

    # Instantiate the class with casing parameters
    def __init__(self, cp):
        self.cp = cp

    @property
    def ncx1(self):
        """number of cells with size csx1"""
        return np.ceil(self.cp.casing_b/self.csx1+2)

    @property
    def npadx1(self):
        """number of padding cells to get from csx1 to csx2"""
        return np.floor(np.log(self.csx2/self.csx1) / np.log(self.pfx1))

    @property
    def hx(self):
        """
        cell spacings in the x-direction
        """
        if getattr(self, '_hx', None) is None:

            # finest uniform region
            hx1a = Utils.meshTensor([(self.csx1, self.ncx1)])

            # pad to second uniform region
            hx1b = Utils.meshTensor([(self.csx1, self.npadx1, self.pfx1)])

            # scale padding so it matches cell size properly
            dx1 = sum(hx1a)+sum(hx1b)
            dx1 = np.floor(dx1/self.csx2)
            hx1b *= (dx1*self.csx2 - sum(hx1a))/sum(hx1b)

            # second uniform chunk of mesh
            ncx2 = np.ceil((self.dx2 - dx1)/self.csx2)
            hx2a = Utils.meshTensor([(self.csx2, ncx2)])

            # pad to infinity
            hx2b = Utils.meshTensor([(self.csx2, self.npadx2, self.pfx2)])

            self._hx = np.hstack([hx1a, hx1b, hx2a, hx2b])

        return self._hx

    @property
    def ncz(self):
        """
        number of core z-cells
        """
        if getattr(self, '_ncz', None) is None:
            # number of core z-cells (add 10 below the end of the casing)
            self._ncz = (
                np.int(np.ceil(-self.cp.casing_z[0]/self.csz))+10
            )
        return self._ncz

    @property
    def hz(self):
        if getattr(self, '_hz', None) is None:

            self._hz = Utils.meshTensor([
                (self.csz, self.npadzd, -self.pfz),
                (self.csz, self.ncz),
                (self.csz, self.npadzu, self.pfz)
            ])
        return self._hz

    @property
    def mesh(self):
        if getattr(self, '_mesh', None) is None:
            self._mesh = Mesh.CylMesh(
                [self.hx, 1., self.hz],
                [0., 0., -np.sum(self.hz[:self.npadzu+self.ncz-self.nza])]
            )
        return self._mesh


##############################################################################
#                                                                            #
#                             Utilities                                      #
#                                                                            #
##############################################################################


# Calculate Casing Currents from fields object
def CasingCurrents(cp, fields, mesh, sigma_m, indActive, casingMap):
    IxCasing = {}
    IzCasing = {}

    casing_ind = sigma_m.copy()
    casing_ind[[0, 1, 3]] = 0. # zero outside casing
    casing_ind[2] = 1. # 1 inside casing

    actMap_Zeros = Maps.InjectActiveCells(mesh, indActive, 0.)

    indCasing = actMap_Zeros * casingMap * casing_ind

    casing_faces = mesh.aveF2CC.T * indCasing
    casing_faces[casing_faces < 0.25] = 0

    for mur in cp.muModels:
        j = fields[mur][:, 'j']
        jA = Utils.sdiag(mesh.area) * j

        jACasing = Utils.sdiag(casing_faces) * jA

        ixCasing = []
        izCasing = []

        for freqind in range(len(cp.freqs)):
            jxCasing = jACasing[:mesh.nFx, freqind].reshape(
                mesh.vnFx[0], mesh.vnFx[2], order='F'
            )
            jzCasing = jACasing[mesh.nFx:, freqind].reshape(
                mesh.vnFz[0], mesh.vnFz[2], order='F'
            )

            ixCasing.append(jxCasing.sum(0))
            izCasing.append(jzCasing.sum(0))

        IxCasing[mur] = ixCasing
        IzCasing[mur] = izCasing
    return IxCasing, IzCasing


# Source Grounded on Casing
class DownHoleCasingSrc(object):

    def __init__(self, mesh, src_a, src_b, casing_a, freqs):
        self.mesh = mesh
        self.src_a = src_a
        self.src_b = src_b
        self.casing_a = casing_a
        self.freqs = freqs

    @property
    def dgv_ind(self):
        # vertically directed wire in borehole
        # go through the center of the well
        mesh = self.mesh
        src_a = self.src_a
        src_b = self.src_b

        dgv_indx = (mesh.gridFz[:, 0] < mesh.hx.min())
        dgv_indz = ((mesh.gridFz[:, 2] >= src_a[2])
                    & (mesh.gridFz[:, 2] <= src_b[2] + 2*mesh.hz.min()))
        dgv_ind = dgv_indx & dgv_indz
        return dgv_ind

    @property
    def dgh_ind2(self):
        mesh = self.mesh
        src_a = self.src_a
        src_b = self.src_b

        # couple to the casing downhole - top part
        dgh_indx = mesh.gridFx[:, 0] <= self.casing_a  # + mesh.hx.min()*2

        # couple to the casing downhole - bottom part
        dgh_indz2 = ((mesh.gridFx[:, 2] <= src_a[2]) &
                     (mesh.gridFx[:, 2] > src_a[2] - mesh.hz.min()))
        return dgh_indx & dgh_indz2

    @property
    def sgh_ind(self):
        mesh = self.mesh
        src_a = self.src_a
        src_b = self.src_b

        # horizontally directed wire
        sgh_indx = (mesh.gridFx[:, 0] <= src_b[0])
        sgh_indz = (
            (mesh.gridFx[:, 2] > mesh.hz.min()) &
            (mesh.gridFx[:, 2] < 2*mesh.hz.min())
        )
        return sgh_indx & sgh_indz

    @property
    def sgv_ind(self):
        mesh = self.mesh
        src_a = self.src_a
        src_b = self.src_b

        # return electrode
        sgv_indx = (
            (mesh.gridFz[:, 0] > src_b[0]*0.9) &
            (mesh.gridFz[:, 0] < src_b[0]*1.1)
        )
        sgv_indz = (
            (mesh.gridFz[:, 2] >= -mesh.hz.min()) &
            (mesh.gridFz[:, 2] < 2*mesh.hz.min())
        )
        return sgv_indx & sgv_indz

    @property
    def dg_p(self):
        if getattr(self, '_dg_p', None) is None:
            # downhole source
            dg_x = np.zeros(self.mesh.vnF[0], dtype=complex)
            dg_y = np.zeros(self.mesh.vnF[1], dtype=complex)
            dg_z = np.zeros(self.mesh.vnF[2], dtype=complex)

            dg_z[self.dgv_ind] = -1.  # part of wire through borehole
            dg_x[self.dgh_ind2] = 1.  # downhole hz part of wire
            dg_x[self.sgh_ind] = -1.  # horizontal part of wire along surface
            dg_z[self.sgv_ind] = 1.  # vertical part of return electrode

            # assemble the source (downhole grounded primary)
            dg = np.hstack([dg_x, dg_y, dg_z])
            dg_p = [
                FDEM.Src.RawVec_e([], _, dg/self.mesh.area) for _ in self.freqs
            ]
            self._dg_p = dg_p
        return self._dg_p

    def plotSrc(self, ax=None):
        if ax is None:
            fig, ax = plt.subplots(1, 1, figsize=(6, 4))

        mesh = self.mesh

        ax.plot(
            mesh.gridFz[self.dgv_ind, 0], mesh.gridFz[self.dgv_ind, 2], 'rv'
        )
        ax.plot(
            mesh.gridFx[self.dgh_ind2, 0], mesh.gridFx[self.dgh_ind2, 2], 'r>'
        )
        ax.plot(
            mesh.gridFz[self.sgv_ind, 0], mesh.gridFz[self.sgv_ind, 2], 'r^'
        )
        ax.plot(
            mesh.gridFx[self.sgh_ind, 0], mesh.gridFx[self.sgh_ind, 2], 'r<'
        )

        return ax


##############################################################################
#                                                                            #
#                             Plotting Code                                  #
#                                                                            #
##############################################################################


# Plot the physical Property Models
def plotModels(mesh, sigma, mu, xlim=[0., 1.], zlim=[-1200., 100.], ax=None):
    if ax is None:
        fig, ax = plt.subplots(1, 2, figsize=(10, 4))

    plt.colorbar(mesh.plotImage(np.log10(sigma), ax=ax[0])[0], ax=ax[0])
    plt.colorbar(mesh.plotImage(mu/mu_0, ax=ax[1])[0], ax=ax[1])

    ax[0].set_xlim(xlim)
    ax[1].set_xlim(xlim)

    ax[0].set_ylim(zlim)
    ax[1].set_ylim(zlim)

    ax[0].set_title('$\log_{10}\sigma$')
    ax[1].set_title('$\mu_r$')

    plt.tight_layout()

    return ax


def plotCurrentDensity(
    mesh,
    fields_j, saveFig=False,
    figsize=(4, 5), fontsize=12, csx=5., csz=5.,
    xmax=1000., zmin=0., zmax=-1200., real_or_imag='real'
):
    csx, ncx = csx, np.ceil(xmax/csx)
    csz, ncz = csz, np.ceil(-zmax/csz)

    xlim=[0., xmax]
    ylim=[zmax, zmin]
    # define the tensor mesh
    meshcart = Mesh.TensorMesh(
        [[(csx, ncx)], [(csx, 1)], [(csz, ncz)]], [0, -csx/2., zmax]
    )

    projF = mesh.getInterpolationMatCartMesh(meshcart, 'F')

    jcart = projF*fields_j
    jcart = getattr(jcart, real_or_imag)

    fig, ax = plt.subplots(1, 1, figsize=figsize)
    if saveFig is True:
        # this looks obnoxious inline, but nice in the saved png
        f = meshcart.plotSlice(
            jcart, normal='Y', vType='F', view='vec',
            pcolorOpts={
                'norm': LogNorm(), 'cmap': plt.get_cmap('viridis')
            },
            streamOpts={'arrowsize': 8, 'color': 'k'},
            ax=ax
        )
    else:
        f = meshcart.plotSlice(
            jcart, normal='Y', vType='F', view='vec',
            pcolorOpts={
                'norm': LogNorm(), 'cmap': plt.get_cmap('viridis')
            },
            ax=ax
        )
    plt.colorbar(
        f[0], label='{} current density (A/m$^2$)'.format(
            real_or_imag
        )
    )

    ax.set_ylim(ylim)
    ax.set_xlim(xlim)
    ax.set_title('Current Density')
    ax.set_xlabel('radius (m)', fontsize=fontsize)
    ax.set_ylabel('z (m)', fontsize=fontsize)

    if saveFig is True:
        fig.savefig('primaryCurrents', dpi=300, bbox_inches='tight')

    return ax


def plot_currents_over_freq(
    IxCasing, IzCasing, cp, mesh,
    mur=1, subtract=None, real_or_imag='real', ax=None, xlim=[-1100., 0.]
):
    print("mu = {} mu_0".format(mur))

    ixCasing = IxCasing[mur]
    izCasing = IzCasing[mur]

    if ax is None:
        fig, ax = plt.subplots(2, 1, figsize=(10, 8))

    for a in ax:
        a.grid(
            which='both', linestyle='-', linewidth=0.4, color=[0.8, 0.8, 0.8],
            alpha=0.5
        )
        a.semilogy(
            [cp.src_a[2], cp.src_a[2]], [1e-14, 1], color=[0.3, 0.3, 0.3]
        )
        a.set_xlim(xlim)
        a.invert_xaxis()

    col = ['b', 'g', 'r', 'c', 'm', 'y']

    leg = []
    for i, f in enumerate(cp.freqs):
        Ix, Iz = ixCasing[i].copy(), izCasing[i].copy()

        if subtract is not None:
            Ix += -IxCasing[subtract][i].copy()
            Iz += -IzCasing[subtract][i].copy()

        Iz_plt = getattr(Iz, real_or_imag)
        leg.append(
            ax[0].semilogy(
                mesh.vectorNz, Iz_plt, '-{}'.format(col[i]),
                label="{} Hz".format(f)
            )
        )
        ax[0].semilogy(mesh.vectorNz, -Iz_plt, '--{}'.format(col[i]))

        Ix_plt = getattr(Ix, real_or_imag)
        ax[1].semilogy(mesh.vectorCCz, Ix_plt, '-{}'.format(col[i]))
        ax[1].semilogy(mesh.vectorCCz, -Ix_plt, '--{}'.format(col[i]))

    if real_or_imag == 'real' and subtract is None:
        if subtract is None:
            ax[0].set_ylim([1e-3, 1.])
            ax[1].set_ylim([3e-5, 2e-4])
        else:
            ax[0].set_ylim([1e-3, 1.])
            ax[1].set_ylim(1e-5, 2e-4)

    elif real_or_imag == 'imag' and subtract is None:
        ax[0].set_ylim([1e-9, 1e-2])
        ax[1].set_ylim([1e-12, 1e-5])

    ax[0].legend(bbox_to_anchor=[1.15, 1])
    plt.show()

    return ax


# plot current density over mu
def plot_currents_over_mu(
    IxCasing, IzCasing, cp, mesh,
    freqind=0, real_or_imag='real',
    subtract=None, ax=None
):
    print("{} Hz".format(cp.freqs[freqind]))

    if ax is None:
        fig, ax = plt.subplots(2, 1, figsize=(10, 8))

    for a in ax:
        a.grid(
            which='both', linestyle='-', linewidth=0.4, color=[0.8, 0.8, 0.8],
            alpha=0.5
        )
        a.semilogy([cp.src_a[2], cp.src_a[2]], [1e-14, 1], color=[0.3, 0.3, 0.3])
        a.set_xlim([-1100., 0.])
    #     a.set_ylim([1e-3, 1.])
        a.invert_xaxis()

    col = ['b', 'g', 'r', 'c', 'm', 'y']

    leg = []
    for i, mur in enumerate(cp.muModels):

        ixCasing = IxCasing[mur]
        izCasing = IzCasing[mur]

        Ix, Iz = ixCasing[freqind].copy(), izCasing[freqind].copy()

        if subtract is not None:
            Ix = Ix - IxCasing[subtract][freqind]
            Iz = Iz - IzCasing[subtract][freqind]

        Iz_plt = getattr(Iz, real_or_imag)
        leg.append(
            ax[0].semilogy(
                mesh.vectorNz, Iz_plt, '-{}'.format(col[i]),
                label="{} $\mu_0$".format(mur)
            )
        )
        ax[0].semilogy(mesh.vectorNz, -Iz_plt, '--{}'.format(col[i]))

        Ix_plt = getattr(Ix, real_or_imag)
        ax[1].semilogy(mesh.vectorCCz, Ix_plt, '-{}'.format(col[i]))
        ax[1].semilogy(mesh.vectorCCz, -Ix_plt, '--{}'.format(col[i]))

    if real_or_imag == 'real' and subtract is None:
        ax[0].set_ylim([1e-3, 1.])
        ax[1].set_ylim([3e-5, 2e-4])

    elif real_or_imag == 'imag':
        ax[0].set_ylim([1e-9, 1e-2])
        ax[1].set_ylim([1e-12, 1e-5])

    ax[0].legend(bbox_to_anchor=[1.15, 1])
    plt.show()
    return ax


# plot over mu
def plot_j_over_mu_z(
    cp, fields, mesh, survey, freqind=0, r=1., zlim=[-1100., 0.],
    real_or_imag='real', subtract=None, ax=None
):
    print("{} Hz".format(cp.freqs[freqind]))

    x_plt = np.r_[r]
    z_plt = np.linspace(zlim[0], zlim[1], int(zlim[1]-zlim[0]))

    XYZ = Utils.ndgrid(x_plt, np.r_[0], z_plt)

    Pfx = mesh.getInterpolationMat(XYZ, 'Fx')
    Pfz = mesh.getInterpolationMat(XYZ, 'Fz')

    Pc = mesh.getInterpolationMat(XYZ, 'CC')
    Zero = sp.csr_matrix(Pc.shape)
    Pcx,Pcz = sp.hstack([Pc,Zero]),sp.hstack([Zero,Pc])

    if ax is None:
        fig, ax = plt.subplots(2,1, figsize=(10,8))

    for a in ax:
        a.grid(which='both', linestyle='-', linewidth=0.4, color=[0.8, 0.8, 0.8], alpha=0.5)
        a.semilogy([cp.src_a[2], cp.src_a[2]], [1e-14, 1], color=[0.3, 0.3, 0.3])
        a.set_xlim([-1100., 0.])
        a.invert_xaxis()

    col = ['b', 'g', 'r', 'c', 'm', 'y']

    leg = []
    for i, mur in enumerate(cp.muModels):

        j = Utils.mkvc(fields[mur][survey.srcList[freqind],'j'].copy())

        if subtract is not None:
            j = j - Utils.mkvc(fields[subtract][survey.srcList[freqind],'j'].copy())

        if real_or_imag == 'real':
            j = j.real
        else:
            j = j.imag

        jx, jz = Pfx *j, Pfz *j

        leg.append(ax[0].semilogy(z_plt, jz, '-{}'.format(col[i]), label="{} $\mu_0$".format(mur)))
        ax[0].semilogy(z_plt,-jz,'--{}'.format(col[i]))

        ax[1].semilogy(z_plt, jx, '-{}'.format(col[i]))
        ax[1].semilogy(z_plt,-jx,'--{}'.format(col[i]))

    if real_or_imag == 'real':
        ax[0].set_ylim([1e-10, 1e-4])
        ax[1].set_ylim([1e-8, 1e-3])

    elif real_or_imag == 'imag':
        ax[0].set_ylim([1e-11, 1e-6])
        ax[1].set_ylim([1e-9, 1e-5])

    ax[0].legend(bbox_to_anchor=[1.15,1])
    return ax

# plot over mu

def plot_j_over_mu_x(cp, fields, mesh, survey, freqind=0, z=-950., real_or_imag='real', subtract=None, ax=None, xlim = [0., 2000.]):
    print("{} Hz".format(cp.freqs[freqind]))

    x_plt = np.linspace(xlim[0], xlim[1], xlim[1])
    z_plt = np.r_[z]

    XYZ = Utils.ndgrid(x_plt, np.r_[0], z_plt)

    Pfx = mesh.getInterpolationMat(XYZ, 'Fx')
    Pfz = mesh.getInterpolationMat(XYZ, 'Fz')

    Pc = mesh.getInterpolationMat(XYZ, 'CC')
    Zero = sp.csr_matrix(Pc.shape)
    Pcx,Pcz = sp.hstack([Pc,Zero]),sp.hstack([Zero,Pc])

    if ax is None:
        fig, ax = plt.subplots(2,1, figsize=(10,8))

    for a in ax:
        a.grid(which='both', linestyle='-', linewidth=0.4, color=[0.8, 0.8, 0.8], alpha=0.5)
#         a.semilogy([src_a[2], src_a[2]], [1e-14, 1], color=[0.3, 0.3, 0.3])
        a.set_xlim(xlim)
#         a.invert_xaxis()

    col = ['b', 'g', 'r', 'c', 'm', 'y']

    leg = []
    for i, mur in enumerate(cp.muModels):

        j = Utils.mkvc(fields[mur][survey.srcList[freqind],'j'].copy())

        if subtract is not None:
            j = j - Utils.mkvc(fields[subtract][survey.srcList[freqind],'j'].copy())

        if real_or_imag == 'real':
            j = j.real
        else:
            j = j.imag

        jx, jz = Pfx *j, Pfz *j

        if np.any(jz > 0):
            leg.append(ax[0].semilogy(x_plt, jz, '-{}'.format(col[i]), label="{} $\mu_0$".format(mur)))
        if np.any(jz < 0):
            ax[0].semilogy(x_plt,-jz,'--{}'.format(col[i]))

        if np.any(jx > 0):
            ax[1].semilogy(x_plt, jx, '-{}'.format(col[i]))
        if np.any(jx < 0):
            ax[1].semilogy(x_plt,-jx,'--{}'.format(col[i]))

    if real_or_imag == 'real':
        ax[0].set_ylim([1e-11, 2e-6])
        ax[1].set_ylim([1e-11, 2e-5])

    elif real_or_imag == 'imag':
        ax[0].set_ylim([1e-11, 1e-6])
        ax[1].set_ylim([1e-9, 1e-5])

    ax[0].legend(bbox_to_anchor=[1.15,1])
    return ax
