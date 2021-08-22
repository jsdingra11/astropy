# Licensed under a 3-clause BSD style license - see LICENSE.rst

import copy
import sys
import warnings
from abc import ABCMeta, abstractmethod
from inspect import signature
from math import acos, cos, exp, floor, log, pi, sin, sqrt

import numpy as np

import astropy.constants as const
import astropy.units as u
from astropy.io.registry import UnifiedReadWriteMethod
from astropy.utils import isiterable
from astropy.utils.decorators import classproperty
from astropy.utils.exceptions import AstropyDeprecationWarning, AstropyUserWarning
from astropy.utils.metadata import MetaData

from . import scalar_inv_efuncs
from .connect import CosmologyRead, CosmologyWrite
from .utils import _float_or_none, inf_like, vectorize_if_needed

# Originally authored by Andrew Becker (becker@astro.washington.edu),
# and modified by Neil Crighton (neilcrighton@gmail.com) and Roban
# Kramer (robanhk@gmail.com).

# Many of these adapted from Hogg 1999, astro-ph/9905116
# and Linder 2003, PRL 90, 91301

__all__ = ["Cosmology", "FLRW", "LambdaCDM", "FlatLambdaCDM", "wCDM",
           "FlatwCDM", "Flatw0waCDM", "w0waCDM", "wpwaCDM", "w0wzCDM",
           "CosmologyError"]

__doctest_requires__ = {'*': ['scipy']}

# Some conversion constants -- useful to compute them once here and reuse in
# the initialization rather than have every object do them.
H0units_to_invs = (u.km / (u.s * u.Mpc)).to(1.0 / u.s)
sec_to_Gyr = u.s.to(u.Gyr)
# const in critical density in cgs units (g cm^-3)
critdens_const = (3 / (8 * pi * const.G)).cgs.value
arcsec_in_radians = pi / (3600. * 180)
arcmin_in_radians = pi / (60. * 180)
# Radiation parameter over c^2 in cgs (g cm^-3 K^-4)
a_B_c2 = (4 * const.sigma_sb / const.c ** 3).cgs.value
# Boltzmann constant in eV / K
kB_evK = const.k_B.to(u.eV / u.K)


# registry of cosmology classes with {key=name : value=class}
_COSMOLOGY_CLASSES = dict()


class CosmologyError(Exception):
    pass


class Cosmology(metaclass=ABCMeta):
    """Base-class for all Cosmologies.

    Parameters
    ----------
    *args
        Arguments into the cosmology; used by subclasses, not this base class.
    name : str or None (optional, keyword-only)
        The name of the cosmology.
    meta : dict or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.
    **kwargs
        Arguments into the cosmology; used by subclasses, not this base class.

    Notes
    -----
    Class instances are static -- you cannot (and should not) change the values
    of the parameters.  That is, all of the above attributes (except meta) are
    read only.

    For details on how to create performant custom subclasses, see the
    documentation on :ref:`astropy-cosmology-fast-integrals`.
    """

    meta = MetaData()

    # Unified I/O read and write methods
    read = UnifiedReadWriteMethod(CosmologyRead)
    write = UnifiedReadWriteMethod(CosmologyWrite)

    def __init_subclass__(cls):
        super().__init_subclass__()

        _COSMOLOGY_CLASSES[cls.__qualname__] = cls

    def __new__(cls, *args, **kwargs):
        self = super().__new__(cls)

        # bundle and store initialization arguments on the instance
        ba = cls._init_signature.bind_partial(*args, **kwargs)
        ba.apply_defaults()  # and fill in the defaults
        self._init_arguments = ba.arguments

        return self

    def __init__(self, *args, name=None, meta=None, **kwargs):
        self._name = name
        self.meta.update(meta or {})

    @classproperty(lazy=True)
    def _init_signature(cls):
        """Initialization signature (without 'self')."""
        # get signature, dropping "self" by taking arguments [1:]
        sig = signature(cls.__init__)
        sig = sig.replace(parameters=list(sig.parameters.values())[1:])
        return sig

    @property
    def name(self):
        """The name of the Cosmology instance."""
        return self._name

    def clone(self, *, meta=None, **kwargs):
        """Returns a copy of this object with updated parameters, as specified.

        This cannot be used to change the type of the cosmology, so ``clone()``
        cannot be used to change between flat and non-flat cosmologies.

        Parameters
        ----------
        meta : mapping or None (optional, keyword-only)
            Metadata that will update the current metadata.
        **kwargs
            Cosmology parameter (and name) modifications.
            If any parameter is changed and a new name is not given, the name
            will be set to "[old name] (modified)".

        Returns
        -------
        newcosmo : `~astropy.cosmology.Cosmology` subclass instance
            A new instance of this class with updated parameters as specified.
            If no modifications are requested, then a reference to this object
            is returned instead of copy.

        Examples
        --------
        To make a copy of the ``Planck13`` cosmology with a different matter
        density (``Om0``), and a new name:

            >>> from astropy.cosmology import Planck13
            >>> newcosmo = Planck13.clone(name="Modified Planck 2013", Om0=0.35)

        If no name is specified, the new name will note the modification.
            >>> Planck13.clone(Om0=0.35).name
            'Planck13 (modified)'

        """
        # Quick return check, taking advantage of the Cosmology immutability.
        if meta is None and not kwargs:
            return self

        # There are changed parameter or metadata values.
        # The name needs to be changed accordingly, if it wasn't already.
        kwargs.setdefault("name", (self.name + " (modified)"
                                   if self.name is not None else None))

        # mix new meta into existing, preferring the former.
        new_meta = {**self.meta, **(meta or {})}
        # Mix kwargs into initial arguments, preferring the former.
        new_init = {**self._init_arguments, "meta": new_meta, **kwargs}
        # Create BoundArgument to handle args versus kwargs.
        # This also handles all errors from mismatched arguments
        ba = self._init_signature.bind_partial(**new_init)
        # Return new instance, respecting args vs kwargs
        return self.__class__(*ba.args, **ba.kwargs)

    # -----------------------------------------------------

    def __eq__(self, other):
        """Check equality on all immutable fields (i.e. not "meta").

        Parameters
        ----------
        other : `~astropy.cosmology.Cosmology` subclass instance
            The object in which to compare.

        Returns
        -------
        bool
            True if all immutable fields are equal, False otherwise.

        """
        if not isinstance(other, Cosmology):
            return False

        sias, oias = self._init_arguments, other._init_arguments

        # check if the cosmologies have identical signatures.
        # this protects against one cosmology having a superset of input
        # parameters to another cosmology.
        if (sias.keys() ^ oias.keys()) - {'meta'}:
            return False

        # are all the non-excluded immutable arguments equal?
        return all((np.all(oias[k] == v) for k, v in sias.items()
                    if k != "meta"))


class FLRW(Cosmology):
    """ A class describing an isotropic and homogeneous
    (Friedmann-Lemaitre-Robertson-Walker) cosmology.

    This is an abstract base class -- you can't instantiate
    examples of this class, but must work with one of its
    subclasses such as `LambdaCDM` or `wCDM`.

    Parameters
    ----------
    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0.  If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.  Note that this does not include
        massive neutrinos.

    Ode0 : float
        Omega dark energy: density of dark energy in units of the critical
        density at z=0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Notes
    -----
    Class instances are static -- you cannot change the values
    of the parameters.  That is, all of the above attributes (except meta) are
    read only.

    For details on how to create performant custom subclasses, see the
    documentation on :ref:`astropy-cosmology-fast-integrals`.
    """

    def __init__(self, H0, Om0, Ode0, Tcmb0=0.0*u.K, Neff=3.04, m_nu=0.0*u.eV,
                 Ob0=None, *, name=None, meta=None):
        super().__init__(name=name, meta=meta)

        # all densities are in units of the critical density
        self._Om0 = float(Om0)
        if self._Om0 < 0.0:
            raise ValueError("Matter density can not be negative")
        self._Ode0 = float(Ode0)
        if Ob0 is not None:
            self._Ob0 = float(Ob0)
            if self._Ob0 < 0.0:
                raise ValueError("Baryonic density can not be negative")
            if self._Ob0 > self._Om0:
                raise ValueError("Baryonic density can not be larger than "
                                 "total matter density")
            self._Odm0 = self._Om0 - self._Ob0
        else:
            self._Ob0 = None
            self._Odm0 = None

        self._Neff = float(Neff)
        if self._Neff < 0.0:
            raise ValueError("Effective number of neutrinos can "
                             "not be negative")

        # Tcmb may have units
        self._Tcmb0 = u.Quantity(Tcmb0, unit=u.K)
        if not self._Tcmb0.isscalar:
            raise ValueError("Tcmb0 is a non-scalar quantity")

        # Hubble parameter at z=0, km/s/Mpc
        self._H0 = u.Quantity(H0, unit=u.km / u.s / u.Mpc)
        if not self._H0.isscalar:
            raise ValueError("H0 is a non-scalar quantity")

        # 100 km/s/Mpc * h = H0 (so h is dimensionless)
        self._h = self._H0.value / 100.
        # Hubble distance
        self._hubble_distance = (const.c / self._H0).to(u.Mpc)
        # H0 in s^-1; don't use units for speed
        H0_s = self._H0.value * H0units_to_invs
        # Hubble time; again, avoiding units package for speed
        self._hubble_time = u.Quantity(sec_to_Gyr / H0_s, u.Gyr)

        # critical density at z=0 (grams per cubic cm)
        cd0value = critdens_const * H0_s ** 2
        self._critical_density0 = u.Quantity(cd0value, u.g / u.cm ** 3)

        # Load up neutrino masses.  Note: in Py2.x, floor is floating
        self._nneutrinos = int(floor(self._Neff))

        # We are going to share Neff between the neutrinos equally.
        # In detail this is not correct, but it is a standard assumption
        # because properly calculating it is a) complicated b) depends
        # on the details of the massive neutrinos (e.g., their weak
        # interactions, which could be unusual if one is considering sterile
        # neutrinos)
        self._massivenu = False
        if self._nneutrinos > 0 and self._Tcmb0.value > 0:
            self._neff_per_nu = self._Neff / self._nneutrinos

            with u.add_enabled_equivalencies(u.mass_energy()):
                m_nu = u.Quantity(m_nu, u.eV)

            # Now, figure out if we have massive neutrinos to deal with,
            # and, if so, get the right number of masses
            # It is worth the effort to keep track of massless ones separately
            # (since they are quite easy to deal with, and a common use case
            # is to set only one neutrino to have mass)
            if m_nu.isscalar:
                # Assume all neutrinos have the same mass
                if m_nu.value == 0:
                    self._nmasslessnu = self._nneutrinos
                    self._nmassivenu = 0
                else:
                    self._massivenu = True
                    self._nmasslessnu = 0
                    self._nmassivenu = self._nneutrinos
                    self._massivenu_mass = (m_nu.value *
                                            np.ones(self._nneutrinos))
            else:
                # Make sure we have the right number of masses
                # -unless- they are massless, in which case we cheat a little
                if m_nu.value.min() < 0:
                    raise ValueError("Invalid (negative) neutrino mass"
                                     " encountered")
                if m_nu.value.max() == 0:
                    self._nmasslessnu = self._nneutrinos
                    self._nmassivenu = 0
                else:
                    self._massivenu = True
                    if len(m_nu) != self._nneutrinos:
                        errstr = "Unexpected number of neutrino masses"
                        raise ValueError(errstr)
                    # Segregate out the massless ones
                    self._nmasslessnu = len(np.nonzero(m_nu.value == 0)[0])
                    self._nmassivenu = self._nneutrinos - self._nmasslessnu
                    w = np.nonzero(m_nu.value > 0)[0]
                    self._massivenu_mass = m_nu[w]

        # Compute photon density, Tcmb, neutrino parameters
        # Tcmb0=0 removes both photons and neutrinos, is handled
        # as a special case for efficiency
        if self._Tcmb0.value > 0:
            # Compute photon density from Tcmb
            self._Ogamma0 = a_B_c2 * self._Tcmb0.value ** 4 /\
                self._critical_density0.value

            # Compute Neutrino temperature
            # The constant in front is (4/11)^1/3 -- see any
            #  cosmology book for an explanation -- for example,
            #  Weinberg 'Cosmology' p 154 eq (3.1.21)
            self._Tnu0 = 0.7137658555036082 * self._Tcmb0

            # Compute Neutrino Omega and total relativistic component
            # for massive neutrinos.  We also store a list version,
            # since that is more efficient to do integrals with (perhaps
            # surprisingly!  But small python lists are more efficient
            # than small numpy arrays).
            if self._massivenu:
                nu_y = self._massivenu_mass / (kB_evK * self._Tnu0)
                self._nu_y = nu_y.value
                self._nu_y_list = self._nu_y.tolist()
                self._Onu0 = self._Ogamma0 * self.nu_relative_density(0)
            else:
                # This case is particularly simple, so do it directly
                # The 0.2271... is 7/8 (4/11)^(4/3) -- the temperature
                # bit ^4 (blackbody energy density) times 7/8 for
                # FD vs. BE statistics.
                self._Onu0 = 0.22710731766 * self._Neff * self._Ogamma0

        else:
            self._Ogamma0 = 0.0
            self._Tnu0 = u.Quantity(0.0, u.K)
            self._Onu0 = 0.0

        # Compute curvature density
        self._Ok0 = 1.0 - self._Om0 - self._Ode0 - self._Ogamma0 - self._Onu0

        # Subclasses should override this reference if they provide
        #  more efficient scalar versions of inv_efunc.
        self._inv_efunc_scalar = self.inv_efunc
        self._inv_efunc_scalar_args = ()

    def _namelead(self):
        """ Helper function for constructing __repr__"""
        if self.name is None:
            return f"{self.__class__.__name__}("
        else:
            return f"{self.__class__.__name__}(name=\"{self.name}\", "

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, Ode0={3:.3g}, "\
                 "Tcmb0={4:.4g}, Neff={5:.3g}, m_nu={6}, "\
                 "Ob0={7:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0, self._Ode0,
                             self._Tcmb0, self._Neff, self.m_nu,
                             _float_or_none(self._Ob0))

    # Set up a set of properties for H0, Om0, Ode0, Ok0, etc. for user access.
    # Note that we don't let these be set (so, obj.Om0 = value fails)

    @property
    def H0(self):
        """ Return the Hubble constant as an `~astropy.units.Quantity` at z=0"""
        return self._H0

    @property
    def Om0(self):
        """ Omega matter; matter density/critical density at z=0"""
        return self._Om0

    @property
    def Ode0(self):
        """ Omega dark energy; dark energy density/critical density at z=0"""
        return self._Ode0

    @property
    def Ob0(self):
        """ Omega baryon; baryonic matter density/critical density at z=0"""
        return self._Ob0

    @property
    def Odm0(self):
        """ Omega dark matter; dark matter density/critical density at z=0"""
        return self._Odm0

    @property
    def Ok0(self):
        """ Omega curvature; the effective curvature density/critical density
        at z=0"""
        return self._Ok0

    @property
    def Tcmb0(self):
        """ Temperature of the CMB as `~astropy.units.Quantity` at z=0"""
        return self._Tcmb0

    @property
    def Tnu0(self):
        """ Temperature of the neutrino background as `~astropy.units.Quantity` at z=0"""
        return self._Tnu0

    @property
    def Neff(self):
        """ Number of effective neutrino species"""
        return self._Neff

    @property
    def has_massive_nu(self):
        """ Does this cosmology have at least one massive neutrino species?"""
        if self._Tnu0.value == 0:
            return False
        return self._massivenu

    @property
    def m_nu(self):
        """ Mass of neutrino species"""
        if self._Tnu0.value == 0:
            return None
        if not self._massivenu:
            # Only massless
            return u.Quantity(np.zeros(self._nmasslessnu), u.eV)
        if self._nmasslessnu == 0:
            # Only massive
            return u.Quantity(self._massivenu_mass, u.eV)
        # A mix -- the most complicated case
        numass = np.append(np.zeros(self._nmasslessnu),
                           self._massivenu_mass.value)
        return u.Quantity(numass, u.eV)

    @property
    def h(self):
        """ Dimensionless Hubble constant: h = H_0 / 100 [km/sec/Mpc]"""
        return self._h

    @property
    def hubble_time(self):
        """ Hubble time as `~astropy.units.Quantity`"""
        return self._hubble_time

    @property
    def hubble_distance(self):
        """ Hubble distance as `~astropy.units.Quantity`"""
        return self._hubble_distance

    @property
    def critical_density0(self):
        """ Critical density as `~astropy.units.Quantity` at z=0"""
        return self._critical_density0

    @property
    def Ogamma0(self):
        """ Omega gamma; the density/critical density of photons at z=0"""
        return self._Ogamma0

    @property
    def Onu0(self):
        """ Omega nu; the density/critical density of neutrinos at z=0"""
        return self._Onu0

    @abstractmethod
    def w(self, z):
        """ The dark energy equation of state.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        w : ndarray or float
          The dark energy equation of state.
          float if scalar input.

        Notes
        -----
        The dark energy equation of state is defined as
        :math:`w(z) = P(z)/\\rho(z)`, where :math:`P(z)` is the
        pressure at redshift z and :math:`\\rho(z)` is the density
        at redshift z, both in units where c=1.

        This must be overridden by subclasses.
        """
        raise NotImplementedError("w(z) is not implemented")

    def Om(self, z):
        """ Return the density parameter for non-relativistic matter
        at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Om : ndarray or float
          The density of non-relativistic matter relative to the critical
          density at each redshift.
          Returns float if input scalar.

        Notes
        -----
        This does not include neutrinos, even if non-relativistic
        at the redshift of interest; see `Onu`.
        """

        if isiterable(z):
            z = np.asarray(z)
        return self._Om0 * (1. + z) ** 3 * self.inv_efunc(z) ** 2

    def Ob(self, z):
        """ Return the density parameter for baryonic matter at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Ob : ndarray or float
          The density of baryonic matter relative to the critical density at
          each redshift.
          Returns float if input scalar.

        Raises
        ------
        ValueError
          If Ob0 is None.
        """

        if self._Ob0 is None:
            raise ValueError("Baryon density not set for this cosmology")
        if isiterable(z):
            z = np.asarray(z)
        return self._Ob0 * (1. + z) ** 3 * self.inv_efunc(z) ** 2

    def Odm(self, z):
        """ Return the density parameter for dark matter at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Odm : ndarray or float
          The density of non-relativistic dark matter relative to the critical
          density at each redshift.
          Returns float if input scalar.

        Raises
        ------
        ValueError
          If Ob0 is None.

        Notes
        -----
        This does not include neutrinos, even if non-relativistic
        at the redshift of interest.
        """

        if self._Odm0 is None:
            raise ValueError("Baryonic density not set for this cosmology, "
                             "unclear meaning of dark matter density")
        if isiterable(z):
            z = np.asarray(z)
        return self._Odm0 * (1. + z) ** 3 * self.inv_efunc(z) ** 2

    def Ok(self, z):
        """ Return the equivalent density parameter for curvature
        at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Ok : ndarray or float
          The equivalent density parameter for curvature at each redshift.
          Returns float if input scalar.
        """

        if isiterable(z):
            z = np.asarray(z)
            # Common enough case to be worth checking explicitly
            if self._Ok0 == 0:
                return np.zeros(np.asanyarray(z).shape)
        else:
            if self._Ok0 == 0:
                return 0.0

        return self._Ok0 * (1. + z) ** 2 * self.inv_efunc(z) ** 2

    def Ode(self, z):
        """ Return the density parameter for dark energy at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Ode : ndarray or float
          The density of non-relativistic matter relative to the critical
          density at each redshift.
          Returns float if input scalar.
        """

        if isiterable(z):
            z = np.asarray(z)
            # Common case worth checking
            if self._Ode0 == 0:
                return np.zeros(np.asanyarray(z).shape)
        else:
            if self._Ode0 == 0:
                return 0.0

        return self._Ode0 * self.de_density_scale(z) * self.inv_efunc(z) ** 2

    def Ogamma(self, z):
        """ Return the density parameter for photons at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Ogamma : ndarray or float
          The energy density of photons relative to the critical
          density at each redshift.
          Returns float if input scalar.
        """

        if isiterable(z):
            z = np.asarray(z)
        return self._Ogamma0 * (1. + z) ** 4 * self.inv_efunc(z) ** 2

    def Onu(self, z):
        """ Return the density parameter for neutrinos at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Onu : ndarray or float
          The energy density of neutrinos relative to the critical
          density at each redshift.  Note that this includes their
          kinetic energy (if they have mass), so it is not equal to
          the commonly used :math:`\\sum \\frac{m_{\\nu}}{94 eV}`,
          which does not include kinetic energy.
          Returns float if input scalar.
        """

        if isiterable(z):
            z = np.asarray(z)
            if self._Onu0 == 0:
                return np.zeros(np.asanyarray(z).shape)
        else:
            if self._Onu0 == 0:
                return 0.0

        return self.Ogamma(z) * self.nu_relative_density(z)

    def Tcmb(self, z):
        """ Return the CMB temperature at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Tcmb : `~astropy.units.Quantity` ['temperature']
          The temperature of the CMB in K.
        """

        if isiterable(z):
            z = np.asarray(z)
        return self._Tcmb0 * (1. + z)

    def Tnu(self, z):
        """ Return the neutrino temperature at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        Tnu : `~astropy.units.Quantity` ['temperature']
          The temperature of the cosmic neutrino background in K.
        """

        if isiterable(z):
            z = np.asarray(z)
        return self._Tnu0 * (1. + z)

    def nu_relative_density(self, z):
        """ Neutrino density function relative to the energy density in
        photons.

        Parameters
        ----------
        z : array-like
           Redshift

        Returns
        -------
        f : ndarray or float
           The neutrino density scaling factor relative to the density
           in photons at each redshift.
           Only returns float if z is scalar.

        Notes
        -----
        The density in neutrinos is given by

        .. math::

          \\rho_{\\nu} \\left(a\\right) = 0.2271 \\, N_{eff} \\,
          f\\left(m_{\\nu} a / T_{\\nu 0} \\right) \\,
          \\rho_{\\gamma} \\left( a \\right)

        where

        .. math::

          f \\left(y\\right) = \\frac{120}{7 \\pi^4}
          \\int_0^{\\infty} \\, dx \\frac{x^2 \\sqrt{x^2 + y^2}}
          {e^x + 1}

        assuming that all neutrino species have the same mass.
        If they have different masses, a similar term is calculated
        for each one. Note that f has the asymptotic behavior :math:`f(0) = 1`.
        This method returns :math:`0.2271 f` using an
        analytical fitting formula given in Komatsu et al. 2011, ApJS 192, 18.
        """

        # Note that there is also a scalar-z-only cython implementation of
        # this in scalar_inv_efuncs.pyx, so if you find a problem in this
        # you need to update there too.

        # See Komatsu et al. 2011, eq 26 and the surrounding discussion
        # for an explanation of what we are doing here.
        # However, this is modified to handle multiple neutrino masses
        # by computing the above for each mass, then summing
        prefac = 0.22710731766  # 7/8 (4/11)^4/3 -- see any cosmo book

        # The massive and massless contribution must be handled separately
        # But check for common cases first
        if not self._massivenu:
            if np.isscalar(z):
                return prefac * self._Neff
            else:
                return prefac * self._Neff * np.ones(np.asanyarray(z).shape)

        # These are purely fitting constants -- see the Komatsu paper
        p = 1.83
        invp = 0.54644808743  # 1.0 / p
        k = 0.3173

        z = np.asarray(z)
        curr_nu_y = self._nu_y / (1. + np.expand_dims(z, axis=-1))
        rel_mass_per = (1.0 + (k * curr_nu_y) ** p) ** invp
        rel_mass = rel_mass_per.sum(-1) + self._nmasslessnu

        return prefac * self._neff_per_nu * rel_mass

    def _w_integrand(self, ln1pz):
        """ Internal convenience function for w(z) integral."""

        # See Linder 2003, PRL 90, 91301 eq (5)
        # Assumes scalar input, since this should only be called
        # inside an integral

        z = exp(ln1pz) - 1.0
        return 1.0 + self.w(z)

    def de_density_scale(self, z):
        r""" Evaluates the redshift dependence of the dark energy density.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        I : ndarray or float
          The scaling of the energy density of dark energy with redshift.
          Returns float if input scalar.

        Notes
        -----
        The scaling factor, I, is defined by :math:`\rho(z) = \rho_0 I`,
        and is given by

        .. math::

            I = \exp \left( 3 \int_{a}^1 \frac{ da^{\prime} }{ a^{\prime} }
            \left[ 1 + w\left( a^{\prime} \right) \right] \right)

        It will generally helpful for subclasses to overload this method if
        the integral can be done analytically for the particular dark
        energy equation of state that they implement.
        """

        # This allows for an arbitrary w(z) following eq (5) of
        # Linder 2003, PRL 90, 91301.  The code here evaluates
        # the integral numerically.  However, most popular
        # forms of w(z) are designed to make this integral analytic,
        # so it is probably a good idea for subclasses to overload this
        # method if an analytic form is available.
        #
        # The integral we actually use (the one given in Linder)
        # is rewritten in terms of z, so looks slightly different than the
        # one in the documentation string, but it's the same thing.

        from scipy.integrate import quad

        if isiterable(z):
            z = np.asarray(z)
            ival = np.array([quad(self._w_integrand, 0, log(1 + redshift))[0]
                             for redshift in z])
            return np.exp(3 * ival)
        else:
            ival = quad(self._w_integrand, 0, log(1 + z))[0]
            return exp(3 * ival)

    def efunc(self, z):
        """ Function used to calculate H(z), the Hubble parameter.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H(z) = H_0 E`.

        It is not necessary to override this method, but if de_density_scale
        takes a particularly simple form, it may be advantageous to.
        """

        if isiterable(z):
            z = np.asarray(z)

        Om0, Ode0, Ok0 = self._Om0, self._Ode0, self._Ok0
        if self._massivenu:
            Or = self._Ogamma0 * (1 + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z

        return np.sqrt(zp1 ** 2 * ((Or * zp1 + Om0) * zp1 + Ok0) +
                       Ode0 * self.de_density_scale(z))

    def inv_efunc(self, z):
        """Inverse of efunc.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The redshift scaling of the inverse Hubble constant.
          Returns float if input scalar.
        """

        # Avoid the function overhead by repeating code
        if isiterable(z):
            z = np.asarray(z)
        Om0, Ode0, Ok0 = self._Om0, self._Ode0, self._Ok0
        if self._massivenu:
            Or = self._Ogamma0 * (1 + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z

        return (zp1 ** 2 * ((Or * zp1 + Om0) * zp1 + Ok0) +
                Ode0 * self.de_density_scale(z))**(-0.5)

    def _lookback_time_integrand_scalar(self, z):
        """ Integrand of the lookback time.

        Parameters
        ----------
        z : float
          Input redshift.

        Returns
        -------
        I : float
          The integrand for the lookback time

        References
        ----------
        Eqn 30 from Hogg 1999.
        """

        args = self._inv_efunc_scalar_args
        return self._inv_efunc_scalar(z, *args) / (1.0 + z)

    def lookback_time_integrand(self, z):
        """ Integrand of the lookback time.

        Parameters
        ----------
        z : float or array-like
          Input redshift.

        Returns
        -------
        I : float or array
          The integrand for the lookback time

        References
        ----------
        Eqn 30 from Hogg 1999.
        """

        if isiterable(z):
            zp1 = 1.0 + np.asarray(z)
        else:
            zp1 = 1. + z

        return self.inv_efunc(z) / zp1

    def _abs_distance_integrand_scalar(self, z):
        """ Integrand of the absorption distance.

        Parameters
        ----------
        z : float
          Input redshift.

        Returns
        -------
        X : float
          The integrand for the absorption distance

        References
        ----------
        See Hogg 1999 section 11.
        """

        args = self._inv_efunc_scalar_args
        return (1.0 + z) ** 2 * self._inv_efunc_scalar(z, *args)

    def abs_distance_integrand(self, z):
        """ Integrand of the absorption distance.

        Parameters
        ----------
        z : float or array
          Input redshift.

        Returns
        -------
        X : float or array
          The integrand for the absorption distance

        References
        ----------
        See Hogg 1999 section 11.
        """

        if isiterable(z):
            zp1 = 1.0 + np.asarray(z)
        else:
            zp1 = 1. + z
        return zp1 ** 2 * self.inv_efunc(z)

    def H(self, z):
        """ Hubble parameter (km/s/Mpc) at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        H : `~astropy.units.Quantity` ['frequency']
          Hubble parameter at each input redshift.
        """

        return self._H0 * self.efunc(z)

    def scale_factor(self, z):
        """ Scale factor at redshift ``z``.

        The scale factor is defined as :math:`a = 1 / (1 + z)`.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        a : ndarray or float
          Scale factor at each input redshift.
          Returns float if input scalar.
        """

        if isiterable(z):
            z = np.asarray(z)

        return 1. / (1. + z)

    def lookback_time(self, z):
        """ Lookback time in Gyr to redshift ``z``.

        The lookback time is the difference between the age of the
        Universe now and the age at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          Lookback time in Gyr to each input redshift.

        See Also
        --------
        z_at_value : Find the redshift corresponding to a lookback time.
        """
        return self._lookback_time(z)

    def _lookback_time(self, z):
        """ Lookback time in Gyr to redshift ``z``.

        The lookback time is the difference between the age of the
        Universe now and the age at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          Lookback time in Gyr to each input redshift.
        """
        return self._integral_lookback_time(z)

    def _integral_lookback_time(self, z):
        """ Lookback time in Gyr to redshift ``z``.

        The lookback time is the difference between the age of the
        Universe now and the age at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          Lookback time in Gyr to each input redshift.
        """
        from scipy.integrate import quad
        f = lambda red: quad(self._lookback_time_integrand_scalar, 0, red)[0]
        return self._hubble_time * vectorize_if_needed(f, z)

    def lookback_distance(self, z):
        """
        The lookback distance is the light travel time distance to a given
        redshift. It is simply c * lookback_time.  It may be used to calculate
        the proper distance between two redshifts, e.g. for the mean free path
        to ionizing radiation.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Lookback distance in Mpc
        """
        return (self.lookback_time(z) * const.c).to(u.Mpc)

    def age(self, z):
        """ Age of the universe in Gyr at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          The age of the universe in Gyr at each input redshift.

        See Also
        --------
        z_at_value : Find the redshift corresponding to an age.
        """
        return self._age(z)

    def _age(self, z):
        """ Age of the universe in Gyr at redshift ``z``.

        This internal function exists to be re-defined for optimizations.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          The age of the universe in Gyr at each input redshift.
        """
        return self._integral_age(z)

    def _integral_age(self, z):
        """ Age of the universe in Gyr at redshift ``z``.

        Calculated using explicit integration.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          The age of the universe in Gyr at each input redshift.

        See Also
        --------
        z_at_value : Find the redshift corresponding to an age.
        """
        from scipy.integrate import quad
        f = lambda red: quad(self._lookback_time_integrand_scalar,
                             red, np.inf)[0]
        return self._hubble_time * vectorize_if_needed(f, z)

    def critical_density(self, z):
        """ Critical density in grams per cubic cm at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        rho : `~astropy.units.Quantity`
          Critical density in g/cm^3 at each input redshift.
        """

        return self._critical_density0 * (self.efunc(z)) ** 2

    def comoving_distance(self, z):
        """ Comoving line-of-sight distance in Mpc at a given
        redshift.

        The comoving distance along the line-of-sight between two
        objects remains constant with time for objects in the Hubble
        flow.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving distance in Mpc to each input redshift.
        """

        return self._comoving_distance_z1z2(0, z)

    def _comoving_distance_z1z2(self, z1, z2):
        """ Comoving line-of-sight distance in Mpc between objects at
        redshifts z1 and z2.

        The comoving distance along the line-of-sight between two
        objects remains constant with time for objects in the Hubble
        flow.

        Parameters
        ----------
        z1, z2 : (N,) array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving distance in Mpc between each input redshift.
        """
        return self._integral_comoving_distance_z1z2(z1, z2)

    def _integral_comoving_distance_z1z2(self, z1, z2):
        """ Comoving line-of-sight distance in Mpc between objects at
        redshifts z1 and z2.

        The comoving distance along the line-of-sight between two
        objects remains constant with time for objects in the Hubble
        flow.

        Parameters
        ----------
        z1, z2 : (N,) array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving distance in Mpc between each input redshift.
        """

        from scipy.integrate import quad
        f = lambda z1, z2: quad(self._inv_efunc_scalar, z1, z2,
                             args=self._inv_efunc_scalar_args)[0]
        return self._hubble_distance * vectorize_if_needed(f, z1, z2)

    def comoving_transverse_distance(self, z):
        """ Comoving transverse distance in Mpc at a given redshift.

        This value is the transverse comoving distance at redshift ``z``
        corresponding to an angular separation of 1 radian. This is
        the same as the comoving distance if omega_k is zero (as in
        the current concordance lambda CDM model).

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving transverse distance in Mpc at each input redshift.

        Notes
        -----
        This quantity also called the 'proper motion distance' in some
        texts.
        """

        return self._comoving_transverse_distance_z1z2(0, z)

    def _comoving_transverse_distance_z1z2(self, z1, z2):
        """Comoving transverse distance in Mpc between two redshifts.

        This value is the transverse comoving distance at redshift
        ``z2`` as seen from redshift ``z1`` corresponding to an
        angular separation of 1 radian. This is the same as the
        comoving distance if omega_k is zero (as in the current
        concordance lambda CDM model).

        Parameters
        ----------
        z1, z2 : (N,) array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving transverse distance in Mpc between input redshift.

        Notes
        -----
        This quantity is also called the 'proper motion distance' in
        some texts.

        """

        Ok0 = self._Ok0
        dc = self._comoving_distance_z1z2(z1, z2)
        if Ok0 == 0:
            return dc
        sqrtOk0 = sqrt(abs(Ok0))
        dh = self._hubble_distance
        if Ok0 > 0:
            return dh / sqrtOk0 * np.sinh(sqrtOk0 * dc.value / dh.value)
        else:
            return dh / sqrtOk0 * np.sin(sqrtOk0 * dc.value / dh.value)

    def angular_diameter_distance(self, z):
        """ Angular diameter distance in Mpc at a given redshift.

        This gives the proper (sometimes called 'physical') transverse
        distance corresponding to an angle of 1 radian for an object
        at redshift ``z``.

        Weinberg, 1972, pp 421-424; Weedman, 1986, pp 65-67; Peebles,
        1993, pp 325-327.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Angular diameter distance in Mpc at each input redshift.
        """

        if isiterable(z):
            z = np.asarray(z)

        return self.comoving_transverse_distance(z) / (1. + z)

    def luminosity_distance(self, z):
        """ Luminosity distance in Mpc at redshift ``z``.

        This is the distance to use when converting between the
        bolometric flux from an object at redshift ``z`` and its
        bolometric luminosity.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Luminosity distance in Mpc at each input redshift.

        See Also
        --------
        z_at_value : Find the redshift corresponding to a luminosity distance.

        References
        ----------
        Weinberg, 1972, pp 420-424; Weedman, 1986, pp 60-62.
        """

        if isiterable(z):
            z = np.asarray(z)

        return (1. + z) * self.comoving_transverse_distance(z)

    def angular_diameter_distance_z1z2(self, z1, z2):
        """ Angular diameter distance between objects at 2 redshifts.
        Useful for gravitational lensing, for example computing the angular diameter
        distance between a lensed galaxy and the foreground lens.

        Parameters
        ----------
        z1, z2 : (N,) array-like
          Input redshifts. For most practical applications such as gravitational lensing,
          z2 should be larger than z1.  The method will work for z2<z1; however, this will
          return negative distances.

        Returns
        -------
        d : (N,) or scalar `~astropy.units.Quantity`
          The angular diameter distance between each input redshift
          pair.
          Returns scalar if input is scalar, array else-wise.

        """
        z1 = np.asanyarray(z1)
        z2 = np.asanyarray(z2)
        if np.any(np.less(z2, z1)):
            warnings.warn(f"Second redshift(s) z2 ({z2}) is less than first "
                          f"redshift(s) z1 ({z1}).", AstropyUserWarning)
        return self._comoving_transverse_distance_z1z2(z1, z2) / (1. + z2)

    def absorption_distance(self, z):
        """ Absorption distance at redshift ``z``.

        This is used to calculate the number of objects with some
        cross section of absorption and number density intersecting a
        sightline per unit redshift path.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : float or ndarray
          Absorption distance (dimensionless) at each input redshift.

        References
        ----------
        Hogg 1999 Section 11. (astro-ph/9905116)
        Bahcall, John N. and Peebles, P.J.E. 1969, ApJ, 156L, 7B
        """

        from scipy.integrate import quad
        f = lambda red: quad(self._abs_distance_integrand_scalar, 0, red)[0]
        return vectorize_if_needed(f, z)

    def distmod(self, z):
        """ Distance modulus at redshift ``z``.

        The distance modulus is defined as the (apparent magnitude -
        absolute magnitude) for an object at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        distmod : `~astropy.units.Quantity` ['length']
          Distance modulus at each input redshift, in magnitudes

        See Also
        --------
        z_at_value : Find the redshift corresponding to a distance modulus.
        """

        # Remember that the luminosity distance is in Mpc
        # Abs is necessary because in certain obscure closed cosmologies
        #  the distance modulus can be negative -- which is okay because
        #  it enters as the square.
        val = 5. * np.log10(abs(self.luminosity_distance(z).value)) + 25.0
        return u.Quantity(val, u.mag)

    def comoving_volume(self, z):
        """ Comoving volume in cubic Mpc at redshift ``z``.

        This is the volume of the universe encompassed by redshifts less
        than ``z``. For the case of omega_k = 0 it is a sphere of radius
        `comoving_distance` but it is less intuitive
        if omega_k is not 0.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        V : `~astropy.units.Quantity`
          Comoving volume in :math:`Mpc^3` at each input redshift.
        """

        Ok0 = self._Ok0
        if Ok0 == 0:
            return 4. / 3. * pi * self.comoving_distance(z) ** 3

        dh = self._hubble_distance.value  # .value for speed
        dm = self.comoving_transverse_distance(z).value
        term1 = 4. * pi * dh ** 3 / (2. * Ok0) * u.Mpc ** 3
        term2 = dm / dh * np.sqrt(1 + Ok0 * (dm / dh) ** 2)
        term3 = sqrt(abs(Ok0)) * dm / dh

        if Ok0 > 0:
            return term1 * (term2 - 1. / sqrt(abs(Ok0)) * np.arcsinh(term3))
        else:
            return term1 * (term2 - 1. / sqrt(abs(Ok0)) * np.arcsin(term3))

    def differential_comoving_volume(self, z):
        """Differential comoving volume at redshift z.

        Useful for calculating the effective comoving volume.
        For example, allows for integration over a comoving volume
        that has a sensitivity function that changes with redshift.
        The total comoving volume is given by integrating
        differential_comoving_volume to redshift z
        and multiplying by a solid angle.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        dV : `~astropy.units.Quantity`
          Differential comoving volume per redshift per steradian at
          each input redshift."""
        dh = self._hubble_distance
        dm = self.comoving_transverse_distance(z)
        return dh * (dm ** 2.0) / u.Quantity(self.efunc(z), u.steradian)

    def kpc_comoving_per_arcmin(self, z):
        """ Separation in transverse comoving kpc corresponding to an
        arcminute at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          The distance in comoving kpc corresponding to an arcmin at each
          input redshift.
        """
        return (self.comoving_transverse_distance(z).to(u.kpc) *
                arcmin_in_radians / u.arcmin)

    def kpc_proper_per_arcmin(self, z):
        """ Separation in transverse proper kpc corresponding to an
        arcminute at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          The distance in proper kpc corresponding to an arcmin at each
          input redshift.
        """
        return (self.angular_diameter_distance(z).to(u.kpc) *
                arcmin_in_radians / u.arcmin)

    def arcsec_per_kpc_comoving(self, z):
        """ Angular separation in arcsec corresponding to a comoving kpc
        at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        theta : `~astropy.units.Quantity` ['angle']
          The angular separation in arcsec corresponding to a comoving kpc
          at each input redshift.
        """
        return u.arcsec / (self.comoving_transverse_distance(z).to(u.kpc) *
                           arcsec_in_radians)

    def arcsec_per_kpc_proper(self, z):
        """ Angular separation in arcsec corresponding to a proper kpc at
        redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        theta : `~astropy.units.Quantity` ['angle']
          The angular separation in arcsec corresponding to a proper kpc
          at each input redshift.
        """
        return u.arcsec / (self.angular_diameter_distance(z).to(u.kpc) *
                           arcsec_in_radians)


class LambdaCDM(FLRW):
    """FLRW cosmology with a cosmological constant and curvature.

    This has no additional attributes beyond those of FLRW.

    Parameters
    ----------
    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0.  If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    Ode0 : float
        Omega dark energy: density of the cosmological constant in units of
        the critical density at z=0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import LambdaCDM
    >>> cosmo = LambdaCDM(H0=70, Om0=0.3, Ode0=0.7)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, Ode0, Tcmb0=0.0*u.K, Neff=3.04, m_nu=0.0*u.eV,
                 Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=Ode0, Tcmb0=Tcmb0, Neff=Neff,
                         m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.lcdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0)
            if self._Ok0 == 0:
                self._optimize_flat_norad()
            else:
                self._comoving_distance_z1z2 = \
                    self._elliptic_comoving_distance_z1z2
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.lcdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0 + self._Onu0)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.lcdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list)

    def _optimize_flat_norad(self):
        """Set optimizations for flat LCDM cosmologies with no radiation.
        """
        # Call out the Om0=0 (de Sitter) and Om0=1 (Einstein-de Sitter)
        # The dS case is required because the hypergeometric case
        #    for Omega_M=0 would lead to an infinity in its argument.
        # The EdS case is three times faster than the hypergeometric.
        if self._Om0 == 0:
            self._comoving_distance_z1z2 = \
                self._dS_comoving_distance_z1z2
            self._age = self._dS_age
            self._lookback_time = self._dS_lookback_time
        elif self._Om0 == 1:
            self._comoving_distance_z1z2 = \
                self._EdS_comoving_distance_z1z2
            self._age = self._EdS_age
            self._lookback_time = self._EdS_lookback_time
        else:
            self._comoving_distance_z1z2 = \
                self._hypergeometric_comoving_distance_z1z2
            self._age = self._flat_age
            self._lookback_time = self._flat_lookback_time

    def w(self, z):
        """Returns dark energy equation of state at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        w : ndarray or float
          The dark energy equation of state.
          Returns float if input scalar.

        Notes
        ------
        The dark energy equation of state is defined as
        :math:`w(z) = P(z)/\\rho(z)`, where :math:`P(z)` is the
        pressure at redshift z and :math:`\\rho(z)` is the density
        at redshift z, both in units where c=1.  Here this is
        :math:`w(z) = -1`.
        """

        if np.isscalar(z):
            return -1.0
        else:
            return -1.0 * np.ones(np.asanyarray(z).shape)

    def de_density_scale(self, z):
        """ Evaluates the redshift dependence of the dark energy density.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        I : ndarray or float
          The scaling of the energy density of dark energy with redshift.
          Returns float if input scalar.

        Notes
        -----
        The scaling factor, I, is defined by :math:`\\rho(z) = \\rho_0 I`,
        and in this case is given by :math:`I = 1`.
        """

        if np.isscalar(z):
            return 1.
        else:
            return np.ones(np.asanyarray(z).shape)

    def _elliptic_comoving_distance_z1z2(self, z1, z2):
        """ Comoving transverse distance in Mpc between two redshifts.

        This value is the transverse comoving distance at redshift ``z``
        corresponding to an angular separation of 1 radian. This is
        the same as the comoving distance if omega_k is zero.

        For Omega_rad = 0 the comoving distance can be directly calculated
        as an elliptic integral.
        Equation here taken from Kantowski, Kao, and Thomas, arXiv:0002334

        Not valid or appropriate for flat cosmologies (Ok0=0).

        Parameters
        ----------
        z1, z2 : array-like
          Input redshifts.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving distance in Mpc between each input redshift.
        """
        from scipy.special import ellipkinc
        try:
            z1, z2 = np.broadcast_arrays(z1, z2)
        except ValueError as e:
            raise ValueError("z1 and z2 have different shapes") from e

        # The analytic solution is not valid for any of Om0, Ode0, Ok0 == 0.
        # Use the explicit integral solution for these cases.
        if self._Om0 == 0 or self._Ode0 == 0 or self._Ok0 == 0:
            return self._integral_comoving_distance_z1z2(z1, z2)

        b = -(27. / 2) * self._Om0**2 * self._Ode0 / self._Ok0**3
        kappa = b / abs(b)
        if (b < 0) or (2 < b):
            def phi_z(Om0, Ok0, kappa, y1, A, z):
                return np.arccos(((1 + z) * Om0 / abs(Ok0) + kappa * y1 - A) /
                                 ((1 + z) * Om0 / abs(Ok0) + kappa * y1 + A))

            v_k = pow(kappa * (b - 1) + sqrt(b * (b - 2)), 1. / 3)
            y1 = (-1 + kappa * (v_k + 1 / v_k)) / 3
            A = sqrt(y1 * (3 * y1 + 2))
            g = 1 / sqrt(A)
            k2 = (2 * A + kappa * (1 + 3 * y1)) / (4 * A)

            phi_z1 = phi_z(self._Om0, self._Ok0, kappa, y1, A, z1)
            phi_z2 = phi_z(self._Om0, self._Ok0, kappa, y1, A, z2)
        # Get lower-right 0<b<2 solution in Om0, Ode0 plane.
        # Fot the upper-left 0<b<2 solution the Big Bang didn't happen.
        elif (0 < b) and (b < 2) and self._Om0 > self._Ode0:
            def phi_z(Om0, Ok0, y1, y2, z):
                return np.arcsin(np.sqrt((y1 - y2) /
                                         ((1 + z) * Om0 / abs(Ok0) + y1)))

            yb = cos(acos(1 - b) / 3)
            yc = sqrt(3) * sin(acos(1 - b) / 3)
            y1 = (1. / 3) * (-1 + yb + yc)
            y2 = (1. / 3) * (-1 - 2 * yb)
            y3 = (1. / 3) * (-1 + yb - yc)
            g = 2 / sqrt(y1 - y2)
            k2 = (y1 - y3) / (y1 - y2)
            phi_z1 = phi_z(self._Om0, self._Ok0, y1, y2, z1)
            phi_z2 = phi_z(self._Om0, self._Ok0, y1, y2, z2)
        else:
            return self._integral_comoving_distance_z1z2(z1, z2)

        prefactor = self._hubble_distance / sqrt(abs(self._Ok0))
        return prefactor * g * (ellipkinc(phi_z1, k2) - ellipkinc(phi_z2, k2))

    def _dS_comoving_distance_z1z2(self, z1, z2):
        """ Comoving line-of-sight distance in Mpc between objects at redshifts
        z1 and z2 in a flat, Omega_Lambda=1 cosmology (de Sitter).

        The comoving distance along the line-of-sight between two
        objects remains constant with time for objects in the Hubble
        flow.

        The de Sitter case has an analytic solution.

        Parameters
        ----------
        z1, z2 : (N,) array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving distance in Mpc between each input redshift.
        """
        try:
            z1, z2 = np.broadcast_arrays(z1, z2)
        except ValueError as e:
            raise ValueError("z1 and z2 have different shapes") from e

        return self._hubble_distance * (z2 - z1)

    def _EdS_comoving_distance_z1z2(self, z1, z2):
        """ Comoving line-of-sight distance in Mpc between objects at redshifts
        z1 and z2 in a flat, Omega_M=1 cosmology (Einstein - de Sitter).

        The comoving distance along the line-of-sight between two
        objects remains constant with time for objects in the Hubble
        flow.

        For OM=1, Omega_rad=0 the comoving distance has an analytic solution.

        Parameters
        ----------
        z1, z2 : (N,) array-like
          Input redshifts.  Must be 1D or scalar.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving distance in Mpc between each input redshift.
        """
        try:
            z1, z2 = np.broadcast_arrays(z1, z2)
        except ValueError as e:
            raise ValueError("z1 and z2 have different shapes") from e

        prefactor = 2 * self._hubble_distance
        return prefactor * ((1+z1)**(-1./2) - (1+z2)**(-1./2))

    def _hypergeometric_comoving_distance_z1z2(self, z1, z2):
        """ Comoving line-of-sight distance in Mpc between objects at
        redshifts z1 and z2.

        The comoving distance along the line-of-sight between two
        objects remains constant with time for objects in the Hubble
        flow.

        For Omega_radiation = 0 the comoving distance can be directly calculated
        as a hypergeometric function.

        Equation here taken from Baes, Camps, Van De Putte, 2017, MNRAS, 468, 927.

        Parameters
        ----------
        z1, z2 : array-like
          Input redshifts.

        Returns
        -------
        d : `~astropy.units.Quantity` ['length']
          Comoving distance in Mpc between each input redshift.
        """
        try:
            z1, z2 = np.broadcast_arrays(z1, z2)
        except ValueError as e:
            raise ValueError("z1 and z2 have different shapes") from e

        s = ((1 - self._Om0) / self._Om0) ** (1./3)
        # Use np.sqrt here to handle negative s (Om0>1).
        prefactor = self._hubble_distance / np.sqrt(s * self._Om0)
        return prefactor * (self._T_hypergeometric(s / (1 + z1)) -
                            self._T_hypergeometric(s / (1 + z2)))

    def _T_hypergeometric(self, x):
        """ Compute T_hypergeometric(x) using Gauss Hypergeometric function 2F1

        T(x) = 2 \\sqrt(x) _{2}F_{1} \\left(\\frac{1}{6}, \\frac{1}{2}; \\frac{7}{6}; -x^3)

        Note:
        The scipy.special.hyp2f1 code already implements the hypergeometric
        transformation suggested by Baes, Camps, Van De Putte, 2017, MNRAS, 468, 927.
        for use in actual numerical evaulations.

        """
        from scipy.special import hyp2f1
        return 2 * np.sqrt(x) * hyp2f1(1./6, 1./2, 7./6, -x**3)

    def _dS_age(self, z):
        """ Age of the universe in Gyr at redshift ``z``.

        The age of a de Sitter Universe is infinite.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          The age of the universe in Gyr at each input redshift.
        """
        return self._hubble_time * inf_like(z)

    def _EdS_age(self, z):
        """ Age of the universe in Gyr at redshift ``z``.

        For Omega_radiation = 0 (T_CMB = 0; massless neutrinos)
        the age can be directly calculated as an elliptic integral.
        See, e.g., Thomas and Kantowski, arXiv:0003463

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          The age of the universe in Gyr at each input redshift.
        """
        if isiterable(z):
            z = np.asarray(z)

        return (2./3) * self._hubble_time * (1+z)**(-3./2)

    def _flat_age(self, z):
        """ Age of the universe in Gyr at redshift ``z``.

        For Omega_radiation = 0 (T_CMB = 0; massless neutrinos)
        the age can be directly calculated as an elliptic integral.
        See, e.g., Thomas and Kantowski, arXiv:0003463

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          The age of the universe in Gyr at each input redshift.
        """
        if isiterable(z):
            z = np.asarray(z)

        # Use np.sqrt, np.arcsinh instead of math.sqrt, math.asinh
        # to handle properly the complex numbers for 1 - Om0 < 0
        prefactor = (2./3) * self._hubble_time / \
            np.lib.scimath.sqrt(1 - self._Om0)
        arg = np.arcsinh(np.lib.scimath.sqrt((1 / self._Om0 - 1 + 0j) /
                                             (1 + z)**3))
        return (prefactor * arg).real

    def _EdS_lookback_time(self, z):
        """ Lookback time in Gyr to redshift ``z``.

        The lookback time is the difference between the age of the
        Universe now and the age at redshift ``z``.

        For Omega_radiation = 0 (T_CMB = 0; massless neutrinos)
        the age can be directly calculated as an elliptic integral.
        The lookback time is here calculated based on the age(0) - age(z)

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          Lookback time in Gyr to each input redshift.
        """
        return self._EdS_age(0) - self._EdS_age(z)

    def _dS_lookback_time(self, z):
        """ Lookback time in Gyr to redshift ``z``.

        The lookback time is the difference between the age of the
        Universe now and the age at redshift ``z``.

        For Omega_radiation = 0 (T_CMB = 0; massless neutrinos)
        the age can be directly calculated.
        a = exp(H * t)   where t=0 at z=0
        t = (1/H) (ln 1 - ln a) = (1/H) (0 - ln (1/(1+z))) = (1/H) ln(1+z)

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          Lookback time in Gyr to each input redshift.
        """
        if isiterable(z):
            z = np.asarray(z)

        return self._hubble_time * np.log(1+z)

    def _flat_lookback_time(self, z):
        """ Lookback time in Gyr to redshift ``z``.

        The lookback time is the difference between the age of the
        Universe now and the age at redshift ``z``.

        For Omega_radiation = 0 (T_CMB = 0; massless neutrinos)
        the age can be directly calculated.
        The lookback time is here calculated based on the age(0) - age(z)

        Parameters
        ----------
        z : array-like
          Input redshifts.  Must be 1D or scalar

        Returns
        -------
        t : `~astropy.units.Quantity` ['time']
          Lookback time in Gyr to each input redshift.
        """
        return self._flat_age(0) - self._flat_age(z)

    def efunc(self, z):
        """ Function used to calculate H(z), the Hubble parameter.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H(z) = H_0 E`.
        """

        if isiterable(z):
            z = np.asarray(z)

        # We override this because it takes a particularly simple
        # form for a cosmological constant
        Om0, Ode0, Ok0 = self._Om0, self._Ode0, self._Ok0
        if self._massivenu:
            Or = self._Ogamma0 * (1. + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z

        return np.sqrt(zp1 ** 2 * ((Or * zp1 + Om0) * zp1 + Ok0) + Ode0)

    def inv_efunc(self, z):
        r""" Function used to calculate :math:`\frac{1}{H_z}`.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The inverse redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H_z = H_0 /
        E`.
        """

        if isiterable(z):
            z = np.asarray(z)
        Om0, Ode0, Ok0 = self._Om0, self._Ode0, self._Ok0
        if self._massivenu:
            Or = self._Ogamma0 * (1 + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z

        return (zp1 ** 2 * ((Or * zp1 + Om0) * zp1 + Ok0) + Ode0)**(-0.5)


class FlatLambdaCDM(LambdaCDM):
    """FLRW cosmology with a cosmological constant and no curvature.

    This has no additional attributes beyond those of FLRW.

    Parameters
    ----------
    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0.  If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import FlatLambdaCDM
    >>> cosmo = FlatLambdaCDM(H0=70, Om0=0.3)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, Tcmb0=0.0*u.K, Neff=3.04, m_nu=0.0*u.eV,
                 Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=0.0, Tcmb0=Tcmb0, Neff=Neff,
                         m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)
        # Do some twiddling after the fact to get flatness
        self._Ode0 = 1.0 - self._Om0 - self._Ogamma0 - self._Onu0
        self._Ok0 = 0.0

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.flcdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0)
            # Repeat the optimization reassignments here because the init
            # of the LambaCDM above didn't actually create a flat cosmology.
            # That was done through the explicit tweak setting self._Ok0.
            self._optimize_flat_norad()
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.flcdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._Ogamma0 + self._Onu0)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.flcdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list)

    def efunc(self, z):
        """ Function used to calculate H(z), the Hubble parameter.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H(z) = H_0 E`.
        """

        if isiterable(z):
            z = np.asarray(z)

        # We override this because it takes a particularly simple
        # form for a cosmological constant
        Om0, Ode0 = self._Om0, self._Ode0
        if self._massivenu:
            Or = self._Ogamma0 * (1 + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z

        return np.sqrt(zp1 ** 3 * (Or * zp1 + Om0) + Ode0)

    def inv_efunc(self, z):
        r"""Function used to calculate :math:`\frac{1}{H_z}`.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The inverse redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H_z = H_0 / E`.
        """

        if isiterable(z):
            z = np.asarray(z)
        Om0, Ode0 = self._Om0, self._Ode0
        if self._massivenu:
            Or = self._Ogamma0 * (1. + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z
        return (zp1 ** 3 * (Or * zp1 + Om0) + Ode0)**(-0.5)

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, Tcmb0={3:.4g}, "\
                 "Neff={4:.3g}, m_nu={5}, Ob0={6:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0,
                             self._Tcmb0, self._Neff, self.m_nu,
                             _float_or_none(self._Ob0))


class wCDM(FLRW):
    """FLRW cosmology with a constant dark energy equation of state
    and curvature.

    This has one additional attribute beyond those of FLRW.

    Parameters
    ----------

    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0. If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    Ode0 : float
        Omega dark energy: density of dark energy in units of the critical
        density at z=0.

    w0 : float, optional
        Dark energy equation of state at all redshifts. This is
        pressure/density for dark energy in units where c=1. A cosmological
        constant has w0=-1.0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import wCDM
    >>> cosmo = wCDM(H0=70, Om0=0.3, Ode0=0.7, w0=-0.9)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, Ode0, w0=-1.0, Tcmb0=0.0*u.K, Neff=3.04,
                 m_nu=0.0*u.eV, Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=Ode0, Tcmb0=Tcmb0, Neff=Neff,
                         m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)
        self._w0 = float(w0)

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.wcdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._w0)
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.wcdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0 + self._Onu0,
                                           self._w0)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.wcdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list, self._w0)

    @property
    def w0(self):
        """ Dark energy equation of state"""
        return self._w0

    def w(self, z):
        """Returns dark energy equation of state at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        w : ndarray or float
          The dark energy equation of state
          Returns float if input scalar.

        Notes
        ------
        The dark energy equation of state is defined as
        :math:`w(z) = P(z)/\\rho(z)`, where :math:`P(z)` is the
        pressure at redshift z and :math:`\\rho(z)` is the density
        at redshift z, both in units where c=1.  Here this is
        :math:`w(z) = w_0`.
        """

        if np.isscalar(z):
            return self._w0
        else:
            return self._w0 * np.ones(np.asanyarray(z).shape)

    def de_density_scale(self, z):
        """ Evaluates the redshift dependence of the dark energy density.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        I : ndarray or float
          The scaling of the energy density of dark energy with redshift.
          Returns float if input scalar.

        Notes
        -----
        The scaling factor, I, is defined by :math:`\\rho(z) = \\rho_0 I`,
        and in this case is given by
        :math:`I = \\left(1 + z\\right)^{3\\left(1 + w_0\\right)}`
        """

        if isiterable(z):
            z = np.asarray(z)
        return (1. + z) ** (3. * (1. + self._w0))

    def efunc(self, z):
        """ Function used to calculate H(z), the Hubble parameter.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H(z) = H_0 E`.
        """

        if isiterable(z):
            z = np.asarray(z)
        Om0, Ode0, Ok0, w0 = self._Om0, self._Ode0, self._Ok0, self._w0
        if self._massivenu:
            Or = self._Ogamma0 * (1. + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z

        return np.sqrt(zp1 ** 2 * ((Or * zp1 + Om0) * zp1 + Ok0) +
                       Ode0 * zp1 ** (3. * (1. + w0)))

    def inv_efunc(self, z):
        r""" Function used to calculate :math:`\frac{1}{H_z}`.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The inverse redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H_z = H_0 / E`.
        """

        if isiterable(z):
            z = np.asarray(z)
        Om0, Ode0, Ok0, w0 = self._Om0, self._Ode0, self._Ok0, self._w0
        if self._massivenu:
            Or = self._Ogamma0 * (1. + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1.0 + z

        return (zp1 ** 2 * ((Or * zp1 + Om0) * zp1 + Ok0) +
                Ode0 * zp1 ** (3. * (1. + w0)))**(-0.5)

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, Ode0={3:.3g}, w0={4:.3g}, "\
                 "Tcmb0={5:.4g}, Neff={6:.3g}, m_nu={7}, Ob0={8:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0,
                             self._Ode0, self._w0, self._Tcmb0, self._Neff,
                             self.m_nu, _float_or_none(self._Ob0))


class FlatwCDM(wCDM):
    """FLRW cosmology with a constant dark energy equation of state
    and no spatial curvature.

    This has one additional attribute beyond those of FLRW.

    Parameters
    ----------

    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0. If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    w0 : float, optional
        Dark energy equation of state at all redshifts. This is
        pressure/density for dark energy in units where c=1. A cosmological
        constant has w0=-1.0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import FlatwCDM
    >>> cosmo = FlatwCDM(H0=70, Om0=0.3, w0=-0.9)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, w0=-1.0, Tcmb0=0.0*u.K, Neff=3.04, m_nu=0.0*u.eV,
                 Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=0.0, w0=w0, Tcmb0=Tcmb0,
                         Neff=Neff, m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)
        # Do some twiddling after the fact to get flatness
        self._Ode0 = 1.0 - self._Om0 - self._Ogamma0 - self._Onu0
        self._Ok0 = 0.0

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.fwcdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._w0)
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.fwcdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._Ogamma0 + self._Onu0,
                                           self._w0)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.fwcdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list, self._w0)

    def efunc(self, z):
        """ Function used to calculate H(z), the Hubble parameter.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H(z) = H_0 E`.
        """

        if isiterable(z):
            z = np.asarray(z)
        Om0, Ode0, w0 = self._Om0, self._Ode0, self._w0
        if self._massivenu:
            Or = self._Ogamma0 * (1. + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1. + z

        return np.sqrt(zp1 ** 3 * (Or * zp1 + Om0) +
                       Ode0 * zp1 ** (3. * (1 + w0)))

    def inv_efunc(self, z):
        r""" Function used to calculate :math:`\frac{1}{H_z}`.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        E : ndarray or float
          The inverse redshift scaling of the Hubble constant.
          Returns float if input scalar.

        Notes
        -----
        The return value, E, is defined such that :math:`H_z = H_0 / E`.
        """

        if isiterable(z):
            z = np.asarray(z)
        Om0, Ode0, w0 = self._Om0, self._Ode0, self._w0
        if self._massivenu:
            Or = self._Ogamma0 * (1. + self.nu_relative_density(z))
        else:
            Or = self._Ogamma0 + self._Onu0
        zp1 = 1. + z

        return (zp1 ** 3 * (Or * zp1 + Om0) +
                Ode0 * zp1 ** (3. * (1. + w0)))**(-0.5)

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, w0={3:.3g}, Tcmb0={4:.4g}, "\
                 "Neff={5:.3g}, m_nu={6}, Ob0={7:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0, self._w0,
                             self._Tcmb0, self._Neff, self.m_nu,
                             _float_or_none(self._Ob0))


class w0waCDM(FLRW):
    """FLRW cosmology with a CPL dark energy equation of state and curvature.

    The equation for the dark energy equation of state uses the
    CPL form as described in Chevallier & Polarski Int. J. Mod. Phys.
    D10, 213 (2001) and Linder PRL 90, 91301 (2003):
    :math:`w(z) = w_0 + w_a (1-a) = w_0 + w_a z / (1+z)`.

    Parameters
    ----------
    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0. If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    Ode0 : float
        Omega dark energy: density of dark energy in units of the critical
        density at z=0.

    w0 : float, optional
        Dark energy equation of state at z=0 (a=1). This is pressure/density
        for dark energy in units where c=1.

    wa : float, optional
        Negative derivative of the dark energy equation of state with respect
        to the scale factor. A cosmological constant has w0=-1.0 and wa=0.0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import w0waCDM
    >>> cosmo = w0waCDM(H0=70, Om0=0.3, Ode0=0.7, w0=-0.9, wa=0.2)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, Ode0, w0=-1.0, wa=0.0, Tcmb0=0.0*u.K, Neff=3.04,
                 m_nu=0.0*u.eV, Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=Ode0, Tcmb0=Tcmb0, Neff=Neff,
                         m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)
        self._w0 = float(w0)
        self._wa = float(wa)

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.w0wacdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._w0, self._wa)
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.w0wacdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0 + self._Onu0,
                                           self._w0, self._wa)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.w0wacdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list, self._w0,
                                           self._wa)

    @property
    def w0(self):
        """ Dark energy equation of state at z=0"""
        return self._w0

    @property
    def wa(self):
        """ Negative derivative of dark energy equation of state w.r.t. a"""
        return self._wa

    def w(self, z):
        """Returns dark energy equation of state at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        w : ndarray or float
          The dark energy equation of state
          Returns float if input scalar.

        Notes
        ------
        The dark energy equation of state is defined as
        :math:`w(z) = P(z)/\\rho(z)`, where :math:`P(z)` is the
        pressure at redshift z and :math:`\\rho(z)` is the density
        at redshift z, both in units where c=1.  Here this is
        :math:`w(z) = w_0 + w_a (1 - a) = w_0 + w_a \\frac{z}{1+z}`.
        """

        if isiterable(z):
            z = np.asarray(z)

        return self._w0 + self._wa * z / (1.0 + z)

    def de_density_scale(self, z):
        r""" Evaluates the redshift dependence of the dark energy density.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        I : ndarray or float
          The scaling of the energy density of dark energy with redshift.
          Returns float if input scalar.

        Notes
        -----
        The scaling factor, I, is defined by :math:`\\rho(z) = \\rho_0 I`,
        and in this case is given by

        .. math::

          I = \left(1 + z\right)^{3 \left(1 + w_0 + w_a\right)}
          \exp \left(-3 w_a \frac{z}{1+z}\right)

        """
        if isiterable(z):
            z = np.asarray(z)
        zp1 = 1.0 + z
        return zp1 ** (3 * (1 + self._w0 + self._wa)) * \
            np.exp(-3 * self._wa * z / zp1)

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, "\
                 "Ode0={3:.3g}, w0={4:.3g}, wa={5:.3g}, Tcmb0={6:.4g}, "\
                 "Neff={7:.3g}, m_nu={8}, Ob0={9:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0,
                             self._Ode0, self._w0, self._wa,
                             self._Tcmb0, self._Neff, self.m_nu,
                             _float_or_none(self._Ob0))


class Flatw0waCDM(w0waCDM):
    """FLRW cosmology with a CPL dark energy equation of state and no
    curvature.

    The equation for the dark energy equation of state uses the
    CPL form as described in Chevallier & Polarski Int. J. Mod. Phys.
    D10, 213 (2001) and Linder PRL 90, 91301 (2003):
    :math:`w(z) = w_0 + w_a (1-a) = w_0 + w_a z / (1+z)`.

    Parameters
    ----------

    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0. If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    w0 : float, optional
        Dark energy equation of state at z=0 (a=1). This is pressure/density
        for dark energy in units where c=1.

    wa : float, optional
        Negative derivative of the dark energy equation of state with respect
        to the scale factor. A cosmological constant has w0=-1.0 and wa=0.0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import Flatw0waCDM
    >>> cosmo = Flatw0waCDM(H0=70, Om0=0.3, w0=-0.9, wa=0.2)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, w0=-1.0, wa=0.0, Tcmb0=0.0*u.K, Neff=3.04,
                 m_nu=0.0*u.eV, Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=0.0, w0=w0, wa=wa, Tcmb0=Tcmb0,
                         Neff=Neff, m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)
        # Do some twiddling after the fact to get flatness
        self._Ode0 = 1.0 - self._Om0 - self._Ogamma0 - self._Onu0
        self._Ok0 = 0.0

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.fw0wacdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._w0, self._wa)
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.fw0wacdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._Ogamma0 + self._Onu0,
                                           self._w0, self._wa)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.fw0wacdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list, self._w0,
                                           self._wa)

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, "\
                 "w0={3:.3g}, Tcmb0={4:.4g}, Neff={5:.3g}, m_nu={6}, "\
                 "Ob0={7:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0, self._w0,
                             self._Tcmb0, self._Neff, self.m_nu,
                             _float_or_none(self._Ob0))


class wpwaCDM(FLRW):
    """FLRW cosmology with a CPL dark energy equation of state, a pivot
    redshift, and curvature.

    The equation for the dark energy equation of state uses the
    CPL form as described in Chevallier & Polarski Int. J. Mod. Phys.
    D10, 213 (2001) and Linder PRL 90, 91301 (2003), but modified
    to have a pivot redshift as in the findings of the Dark Energy
    Task Force (Albrecht et al. arXiv:0901.0721 (2009)):
    :math:`w(a) = w_p + w_a (a_p - a) = w_p + w_a( 1/(1+zp) - 1/(1+z) )`.

    Parameters
    ----------

    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0. If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    Ode0 : float
        Omega dark energy: density of dark energy in units of the critical
        density at z=0.

    wp : float, optional
        Dark energy equation of state at the pivot redshift zp. This is
        pressure/density for dark energy in units where c=1.

    wa : float, optional
        Negative derivative of the dark energy equation of state with respect
        to the scale factor. A cosmological constant has wp=-1.0 and wa=0.0.

    zp : float, optional
        Pivot redshift -- the redshift where w(z) = wp

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import wpwaCDM
    >>> cosmo = wpwaCDM(H0=70, Om0=0.3, Ode0=0.7, wp=-0.9, wa=0.2, zp=0.4)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, Ode0, wp=-1.0, wa=0.0, zp=0.0, Tcmb0=0.0*u.K,
                 Neff=3.04, m_nu=0.0*u.eV, Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=Ode0, Tcmb0=Tcmb0, Neff=Neff,
                         m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)
        self._wp = float(wp)
        self._wa = float(wa)
        self._zp = float(zp)

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        apiv = 1.0 / (1.0 + self._zp)
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.wpwacdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._wp, apiv, self._wa)
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.wpwacdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0 + self._Onu0,
                                           self._wp, apiv, self._wa)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.wpwacdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list, self._wp,
                                           apiv, self._wa)

    @property
    def wp(self):
        """ Dark energy equation of state at the pivot redshift zp"""
        return self._wp

    @property
    def wa(self):
        """ Negative derivative of dark energy equation of state w.r.t. a"""
        return self._wa

    @property
    def zp(self):
        """ The pivot redshift, where w(z) = wp"""
        return self._zp

    def w(self, z):
        """Returns dark energy equation of state at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        w : ndarray or float
          The dark energy equation of state
          Returns float if input scalar.

        Notes
        ------
        The dark energy equation of state is defined as
        :math:`w(z) = P(z)/\\rho(z)`, where :math:`P(z)` is the
        pressure at redshift z and :math:`\\rho(z)` is the density
        at redshift z, both in units where c=1.  Here this is
        :math:`w(z) = w_p + w_a (a_p - a)` where :math:`a = 1/1+z`
        and :math:`a_p = 1 / 1 + z_p`.
        """

        if isiterable(z):
            z = np.asarray(z)

        apiv = 1.0 / (1.0 + self._zp)
        return self._wp + self._wa * (apiv - 1.0 / (1. + z))

    def de_density_scale(self, z):
        r""" Evaluates the redshift dependence of the dark energy density.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        I : ndarray or float
          The scaling of the energy density of dark energy with redshift.
          Returns float if input scalar.

        Notes
        -----
        The scaling factor, I, is defined by :math:`\\rho(z) = \\rho_0 I`,
        and in this case is given by

        .. math::

          a_p = \frac{1}{1 + z_p}

          I = \left(1 + z\right)^{3 \left(1 + w_p + a_p w_a\right)}
          \exp \left(-3 w_a \frac{z}{1+z}\right)
        """

        if isiterable(z):
            z = np.asarray(z)
        zp1 = 1. + z
        apiv = 1. / (1. + self._zp)
        return zp1 ** (3. * (1. + self._wp + apiv * self._wa)) * \
            np.exp(-3. * self._wa * z / zp1)

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, Ode0={3:.3g}, wp={4:.3g}, "\
                 "wa={5:.3g}, zp={6:.3g}, Tcmb0={7:.4g}, Neff={8:.3g}, "\
                 "m_nu={9}, Ob0={10:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0,
                             self._Ode0, self._wp, self._wa, self._zp,
                             self._Tcmb0, self._Neff, self.m_nu,
                             _float_or_none(self._Ob0))


class w0wzCDM(FLRW):
    """FLRW cosmology with a variable dark energy equation of state
    and curvature.

    The equation for the dark energy equation of state uses the
    simple form: :math:`w(z) = w_0 + w_z z`.

    This form is not recommended for z > 1.

    Parameters
    ----------

    H0 : float or scalar quantity-like ['frequency']
        Hubble constant at z = 0. If a float, must be in [km/sec/Mpc]

    Om0 : float
        Omega matter: density of non-relativistic matter in units of the
        critical density at z=0.

    Ode0 : float
        Omega dark energy: density of dark energy in units of the critical
        density at z=0.

    w0 : float, optional
        Dark energy equation of state at z=0. This is pressure/density for
        dark energy in units where c=1.

    wz : float, optional
        Derivative of the dark energy equation of state with respect to z.
        A cosmological constant has w0=-1.0 and wz=0.0.

    Tcmb0 : float or scalar quantity-like ['temperature'], optional
        Temperature of the CMB z=0. If a float, must be in [K].
        Default: 0 [K]. Setting this to zero will turn off both photons
        and neutrinos (even massive ones).

    Neff : float, optional
        Effective number of Neutrino species. Default 3.04.

    m_nu : quantity-like ['energy', 'mass'] or array-like, optional
        Mass of each neutrino species in [eV] (mass-energy equivalency enabled).
        If this is a scalar Quantity, then all neutrino species are assumed to
        have that mass. Otherwise, the mass of each species. The actual number
        of neutrino species (and hence the number of elements of m_nu if it is
        not scalar) must be the floor of Neff. Typically this means you should
        provide three neutrino masses unless you are considering something like
        a sterile neutrino.

    Ob0 : float or None, optional
        Omega baryons: density of baryonic matter in units of the critical
        density at z=0.  If this is set to None (the default), any
        computation that requires its value will raise an exception.

    name : str or None (optional, keyword-only)
        Name for this cosmological object.

    meta : mapping or None (optional, keyword-only)
        Metadata for the cosmology, e.g., a reference.

    Examples
    --------
    >>> from astropy.cosmology import w0wzCDM
    >>> cosmo = w0wzCDM(H0=70, Om0=0.3, Ode0=0.7, w0=-0.9, wz=0.2)

    The comoving distance in Mpc at redshift z:

    >>> z = 0.5
    >>> dc = cosmo.comoving_distance(z)
    """

    def __init__(self, H0, Om0, Ode0, w0=-1.0, wz=0.0, Tcmb0=0.0*u.K, Neff=3.04,
                 m_nu=0.0*u.eV, Ob0=None, *, name=None, meta=None):
        super().__init__(H0=H0, Om0=Om0, Ode0=Ode0, Tcmb0=Tcmb0, Neff=Neff,
                         m_nu=m_nu, Ob0=Ob0, name=name, meta=meta)
        self._w0 = float(w0)
        self._wz = float(wz)

        # Please see :ref:`astropy-cosmology-fast-integrals` for discussion
        # about what is being done here.
        if self._Tcmb0.value == 0:
            self._inv_efunc_scalar = scalar_inv_efuncs.w0wzcdm_inv_efunc_norel
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._w0, self._wz)
        elif not self._massivenu:
            self._inv_efunc_scalar = scalar_inv_efuncs.w0wzcdm_inv_efunc_nomnu
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0 + self._Onu0,
                                           self._w0, self._wz)
        else:
            self._inv_efunc_scalar = scalar_inv_efuncs.w0wzcdm_inv_efunc
            self._inv_efunc_scalar_args = (self._Om0, self._Ode0, self._Ok0,
                                           self._Ogamma0, self._neff_per_nu,
                                           self._nmasslessnu,
                                           self._nu_y_list, self._w0,
                                           self._wz)

    @property
    def w0(self):
        """ Dark energy equation of state at z=0"""
        return self._w0

    @property
    def wz(self):
        """ Derivative of the dark energy equation of state w.r.t. z"""
        return self._wz

    def w(self, z):
        """Returns dark energy equation of state at redshift ``z``.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        w : ndarray or float
          The dark energy equation of state
          Returns float if input scalar.

        Notes
        ------
        The dark energy equation of state is defined as
        :math:`w(z) = P(z)/\\rho(z)`, where :math:`P(z)` is the
        pressure at redshift z and :math:`\\rho(z)` is the density
        at redshift z, both in units where c=1.  Here this is given by
        :math:`w(z) = w_0 + w_z z`.
        """

        if isiterable(z):
            z = np.asarray(z)

        return self._w0 + self._wz * z

    def de_density_scale(self, z):
        r""" Evaluates the redshift dependence of the dark energy density.

        Parameters
        ----------
        z : array-like
          Input redshifts.

        Returns
        -------
        I : ndarray or float
          The scaling of the energy density of dark energy with redshift.
          Returns float if input scalar.

        Notes
        -----
        The scaling factor, I, is defined by :math:`\\rho(z) = \\rho_0 I`,
        and in this case is given by

        .. math::

          I = \left(1 + z\right)^{3 \left(1 + w_0 - w_z\right)}
          \exp \left(-3 w_z z\right)
        """

        if isiterable(z):
            z = np.asarray(z)
        zp1 = 1. + z
        return zp1 ** (3. * (1. + self._w0 - self._wz)) *\
            np.exp(-3. * self._wz * z)

    def __repr__(self):
        retstr = "{0}H0={1:.3g}, Om0={2:.3g}, "\
                 "Ode0={3:.3g}, w0={4:.3g}, wz={5:.3g} Tcmb0={6:.4g}, "\
                 "Neff={7:.3g}, m_nu={8}, Ob0={9:s})"
        return retstr.format(self._namelead(), self._H0, self._Om0,
                             self._Ode0, self._w0, self._wz, self._Tcmb0,
                             self._Neff, self.m_nu, _float_or_none(self._Ob0))
