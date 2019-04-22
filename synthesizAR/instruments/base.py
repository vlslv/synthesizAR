"""
Base class for instrument objects.
"""

import warnings

import numpy as np
from scipy.interpolate import interp1d
import astropy.units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
import h5py
from sunpy.util.metadata import MetaDict
from sunpy.sun import constants
from sunpy.coordinates.frames import Helioprojective, HeliographicStonyhurst
try:
    import distributed
except ImportError:
    warnings.warn('Dask distributed scheduler required for parallel execution')

from synthesizAR.util import SpatialPair, get_keys


class InstrumentBase(object):
    """
    Base class for instruments. Need to at least implement a detect() method that is used by the
    `Observer` class to get the detector counts.

    Parameters
    ----------
    observing_time : `~astropy.units.Quantity`
        Tuple of start and end observing times
    observer_coordinate : `~astropy.coordinates.SkyCoord`
        Coordinate of the observing instrument
    """
    fits_template = MetaDict()

    @u.quantity_input
    def __init__(self, observing_time: u.s, observer_coordinate, start_time=None):
        self.observing_time = np.arange(observing_time[0].to(u.s).value,
                                        observing_time[1].to(u.s).value,
                                        self.cadence.value)*u.s
        self.observer_coordinate = observer_coordinate
        self.start_time = Time.now() if start_time is None else Time(start_time)

    def detect(self, *args, **kwargs):
        """
        Converts emissivity for a particular transition to counts per detector channel. When writing
        a new instrument class, this method should be overridden.
        """
        raise NotImplementedError('No detect method implemented.')

    def build_detector_file(self, file_template, dset_shape, chunks, *args, **kwargs):
        """
        Allocate space for counts data.
        """
        dset_names = ['density', 'electron_temperature', 'ion_temperature', 'velocity_x',
                      'velocity_y', 'velocity_z']
        dset_names += kwargs.get('additional_fields', [])
        self.counts_file = file_template.format(self.name)

        with h5py.File(self.counts_file, 'a') as hf:
            if 'time' not in hf:
                dset = hf.create_dataset('time', data=self.observing_time.value)
                dset.attrs['unit'] = self.observing_time.unit.to_string()
            for dn in dset_names:
                if dn not in hf:
                    hf.create_dataset(dn, dset_shape, chunks=chunks)

    @property
    def total_coordinates(self):
        """
        Helioprojective coordinates for all loops for the instrument observer
        """
        if not hasattr(self, 'counts_file'):
            raise AttributeError(f'''No counts file found for {self.name}. Build it first
                                     using Observer.build_detector_files''')
        with h5py.File(self.counts_file, 'r') as hf:
            dset = hf['coordinates']
            total_coordinates = u.Quantity(dset, get_keys(dset.attrs, ('unit', 'units')))

        coords = SkyCoord(x=total_coordinates[:, 0], y=total_coordinates[:, 1],
                          z=total_coordinates[:, 2], frame=HeliographicStonyhurst,
                          representation='cartesian')
        return coords.transform_to(Helioprojective(observer=self.observer_coordinate))

    def los_velocity(self, v_x, v_y, v_z):
        """
        Compute the LOS velocity for the instrument observer
        """
        # NOTE: transform from HEEQ to HCC with respect to the instrument observer
        obs = self.observer_coordinate.transform_to(HeliographicStonyhurst)
        Phi_0, B_0 = obs.lon.to(u.radian), obs.lat.to(u.radian)
        v_los = v_z*np.sin(B_0) + v_x*np.cos(B_0)*np.cos(Phi_0) + v_y*np.cos(B_0)*np.sin(Phi_0)
        # NOTE: Negative sign to be consistent with convention v_los > 0 away from observer
        return -v_los

    def interpolate(self, y, loop, interp_s):
        """
        Interpolate in time and space and.
        """
        if type(y) is str:
            y = getattr(loop, y)
        f_s = interp1d(loop.field_aligned_coordinate.value, y.value, axis=1, kind='linear')
        y_s = f_s(interp_s)
        if loop.time.shape == (1,):
            # If static case, no need to interpolate in time
            # But require that the observing and loop times are the same
            assert np.all(loop.time == self.observing_time)
            interpolated_y = y_s
        else:
            f_t = interp1d(loop.time.value, y_s, axis=0, kind='linear', fill_value='extrapolate')
            interpolated_y = f_t(self.observing_time.value)
        return interpolated_y * y.unit

    def write_to_hdf5(self, value, start_index, dset_name):
        """
        Write quantity for a single loop to a block in an HDF5 dataset
        """
        lock = distributed.Lock(f'hdf5_{self.name}')
        with lock:
            with h5py.File(self.counts_file, 'a') as hf:
                self.commit(value, hf[dset_name], start_index)

    @staticmethod
    def commit(y, dset, start_index):
        if 'unit' not in dset.attrs:
            dset.attrs['unit'] = y.unit.to_string()
        dset[:, start_index:(start_index + y.shape[1])] = y.value

    @staticmethod
    def generic_2d_histogram(counts_filename, dset_name, i_time, bins, bin_range):
        """
        Turn flattened quantity into 2D weighted histogram
        """
        with h5py.File(counts_filename, 'r') as hf:
            weights = np.array(hf[dset_name][i_time, :])
            units = u.Unit(get_keys(hf[dset_name].attrs, ('unit', 'units')))
            coordinates = np.array(hf['coordinates'][:, :2])
        hc, _ = np.histogramdd(coordinates, bins=bins[:2], range=bin_range[:2])
        h, _ = np.histogramdd(coordinates, bins=bins[:2], range=bin_range[:2], weights=weights)
        h /= np.where(hc == 0, 1, hc)
        return h.T*units

    def make_fits_header(self, field, channel):
        """
        Build up FITS header with relevant instrument information.
        """
        min_x, max_x, min_y, max_y = self._get_fov()
        bins, _ = self.make_detector_array(field)
        fits_header = MetaDict()
        fits_header['crval1'] = (min_x + (max_x - min_x)/2).value
        fits_header['crval2'] = (min_y + (max_y - min_y)/2).value
        fits_header['cunit1'] = self.total_coordinates.Tx.unit.to_string()
        fits_header['cunit2'] = self.total_coordinates.Ty.unit.to_string()
        fits_header['hglt_obs'] = self.observer_coordinate.lat.to(u.deg).value
        fits_header['hgln_obs'] = self.observer_coordinate.lon.to(u.deg).value
        fits_header['ctype1'] = 'HPLN-TAN'
        fits_header['ctype2'] = 'HPLT-TAN'
        fits_header['dsun_obs'] = self.observer_coordinate.radius.to(u.m).value
        fits_header['rsun_obs'] = ((constants.radius
                                    / (self.observer_coordinate.radius - constants.radius))
                                   .decompose() * u.radian).to(u.arcsec).value
        fits_header['cdelt1'] = self.resolution.x.value
        fits_header['cdelt2'] = self.resolution.y.value
        fits_header['crpix1'] = (bins.x.value + 1.0)/2.0
        fits_header['crpix2'] = (bins.y.value + 1.0)/2.0
        if 'instrument_label' in channel:
            fits_header['instrume'] = channel['instrument_label']
        if 'wavelength' in channel:
            fits_header['wavelnth'] = channel['wavelength'].value
        # Anything that needs to be overridden in a subclass can be put in the fits template
        fits_header.update(self.fits_template)

        return fits_header

    def _get_fov(self):
        """
        Find the field of view given the loop coordinates in HPC.
        """
        loop_coords = self.total_coordinates
        if 'gaussian_width' in self.channels[0]:
            width_max = u.Quantity([c['gaussian_width']['x'] for c in self.channels]).max()
            pad_x = self.resolution.x * width_max * 10
            width_max = u.Quantity([c['gaussian_width']['y'] for c in self.channels]).max()
            pad_y = self.resolution.y * width_max * 10
        else:
            pad_x = self.resolution.x * 10 * u.pixel
            pad_y = self.resolution.y * 10 * u.pixel
        min_x = loop_coords.Tx.min() - pad_x
        max_x = loop_coords.Tx.max() + pad_x
        min_y = loop_coords.Ty.min() - pad_y
        max_y = loop_coords.Ty.max() + pad_y

        return min_x, max_x, min_y, max_y

    def make_detector_array(self, field):
        """
        Construct bins based on desired observing area.
        """
        # Get field of view
        min_x, max_x, min_y, max_y = self._get_fov()
        min_z = self.total_coordinates.distance.min()
        max_z = self.total_coordinates.distance.max()
        delta_x = max_x - min_x
        delta_y = max_y - min_y
        bins_x = np.ceil(delta_x / self.resolution.x)
        bins_y = np.ceil(delta_y / self.resolution.y)
        bins_z = max(bins_x, bins_y)

        # NOTE: the z-quantities are used to determine the integration step along the LOS
        bins = SpatialPair(x=bins_x, y=bins_y, z=bins_z)
        bin_range = SpatialPair(x=u.Quantity([min_x, max_x]),
                                y=u.Quantity([min_y, max_y]),
                                z=u.Quantity([min_z, max_z]))

        return bins, bin_range
