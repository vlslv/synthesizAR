"""
Active region object definition. This object holds all the important information about our synthesized active region.
"""
import os
import sys
import logging

import numpy as np
import matplotlib.pyplot as plt
import seaborn.apionly as sns
import sunpy.map
import astropy.units as u
import yt
import solarbextrapolation.map3dclasses
import solarbextrapolation.extrapolators

from .util import convert_angle_to_length,find_seed_points


class Skeleton(object):
    """
    Construct magnetic field skeleton from HMI fits file

    Parameters
    ----------

    Examples
    --------
    """
    def __init__(self,hmi_fits_file,**kwargs):
        """
        Constructor

        Notes
        -----
        Right now, this class just accepts an HMI fits file. Could be adjusted to do the actual query as well.
        """
        self.logger = logging.getLogger(name=type(self).__name__)
        tmp_map = sunpy.map.Map(hmi_fits_file)
        self._process_map(tmp_map,**kwargs)


    def _process_map(self,tmp_map,crop=None,resample=None):
        """
        Rotate, crop and resample map if needed. Can do any other needed processing here too.

        Parameters
        ----------
        map : `~sunpy.map.Map`
            Original HMI map
        crop : `tuple` `[xrange,yrange]`, optional
            The x- and y-ranges of the cropped map, both should be of type `~astropy.units.Quantity` and have the same units as `map.xrange` and `map.yrange`
        resample : `~astropy.units.Quantity`, `[new_xdim,new_ydim]`, optional
            The new x- and y-dimensions of the resampled map, should have the same units as `map.dimensions.x` and `map.dimensions.y`
        """

        tmp_map = tmp_map.rotate()
        if crop is not None:
            tmp_map = tmp_map.submap(crop[0],crop[1])
        if resample is not None:
            tmp_map = tmp_map.resample(resample,method='linear')

        self.hmi_map = tmp_map


    def _convert_angle_to_length(self,angle_or_length,working_units=u.meter):
        """
        Recast the `synthesizAR.util.convert_angle_to_length` to automatically use the supplied HMI map.
        """

        result = convert_angle_to_length(self.hmi_map,angle_or_length,working_units=working_units)
        return result


    def _transform_to_yt(self,map_3d,boundary_clipping=(2,2,2)):
        """
        Reshape data structure to something yt can work with.

        Parameters
        ----------
        map_3d : `solarbextrapolation.map3dclasses.Map3D`
            Result from the field extrapolation routine
        boundary_clipping : `tuple`, optional
            The extrapolated volume has a layer of ghost cells in each dimension. This tuple of (nx,ny,nz) tells how many cells we need to contract the volume and map in each direction.
        """

        #reshape the magnetic field data
        _tmp = map_3d.data[boundary_clipping[0]:-boundary_clipping[0], boundary_clipping[1]:-boundary_clipping[1], boundary_clipping[2]:-boundary_clipping[2],:]
        #some annoying and cryptic translation between yt and SunPy
        data = dict(
                    Bx=(np.swapaxes(_tmp[:,:,:,1],0,1),"T"),
                    By=(np.swapaxes(_tmp[:,:,:,0],0,1),"T"),
                    Bz=(np.swapaxes(_tmp[:,:,:,2],0,1),"T"))

        #trim the boundary hmi map appropriately
        self.clipped_hmi_map = self.hmi_map.submap(
                                self.hmi_map.xrange+self.hmi_map.scale.x*u.Quantity([boundary_clipping[0]*u.pixel,-boundary_clipping[0]*u.pixel]),
                                self.hmi_map.yrange+self.hmi_map.scale.y*u.Quantity([boundary_clipping[1]*u.pixel,-boundary_clipping[1]*u.pixel]))

        #create the bounding box
        bbox = np.array([self._convert_angle_to_length(self.clipped_hmi_map.xrange).value,
                         self._convert_angle_to_length(self.clipped_hmi_map.yrange).value,
                         self._convert_angle_to_length(map_3d.zrange+map_3d.scale.z*u.Quantity([boundary_clipping[2]*u.pixel,-boundary_clipping[2]*u.pixel])).value])

        #assemble the dataset
        self.extrapolated_3d_field = yt.load_uniform_grid(data, data['Bx'][0].shape, bbox=bbox, length_unit='cm', geometry=('cartesian',('x','y','z')))


    def _filter_streamlines(self,streamline,close_threshold=0.05,loop_length_range=[2.e+9,5.e+10]):
        """
        Check extracted loop to make sure it fits given criteria. Return True if it passes.

        Parameters
        ----------
        streamline : yt streamline object
        close_threshold : `float`
            percentage of domain width allowed between loop endpoints
        loop_length_range : `tuple`
            minimum and maximum allowed loop lengths (in centimeters)
        """

        streamline = streamline[np.all(streamline != 0.0, axis=1)]
        loop_length = np.sum(np.linalg.norm(np.diff(streamline,axis=0),axis=1))

        if np.fabs(streamline[0,2] - streamline[-1,2]) > close_threshold*self.extrapolated_3d_field.domain_width[2]:
            return False
        elif loop_length > loop_length_range[1] or loop_length < loop_length_range[0]:
            return False
        else:
            return True


    def extrapolate_field(self,zshape,zrange,use_numba_for_extrapolation=True):
        """
        Extrapolate the 3D field and transform it into a yt data object.
        """

        #extrapolate field
        self.logger.debug('Extrapolating field.')
        extrapolator = solarbextrapolation.extrapolators.PotentialExtrapolator(self.hmi_map, zshape=zshape, zrange=zrange)
        map_3d = extrapolator.extrapolate(enable_numba=use_numba_for_extrapolation)

        #hand it to yt
        self.logger.debug('Transforming to yt data object')
        self._transform_to_yt(map_3d)


    def extract_streamlines(self,number_fieldlines,max_tries=100):
        """
        Trace the fieldlines through extrapolated 3D volume
        """
        #trace field and return list of field lines
        self.logger.debug('Tracing fieldlines')
        self.streamlines = []
        seed_points = []
        i_tries = 0
        while len(self.streamlines) < number_fieldlines and i_tries < max_tries:
            remaining_fieldlines = number_fieldlines - len(self.streamlines)
            self.logger.debug('Remaining number of streamlines is {}'.format(remaining_fieldlines))
            #calculate seed points
            seed_points = find_seed_points(self.extrapolated_3d_field, self.clipped_hmi_map, remaining_fieldlines, preexisting_seeds=seed_points, mask_threshold=0.1, safety=2.)
            #trace fieldlines
            streamlines = yt.visualization.api.Streamlines(self.extrapolated_3d_field, seed_points*self.extrapolated_3d_field.domain_width/self.extrapolated_3d_field.domain_width.value, xfield='Bx', yfield='By', zfield='Bz', get_magnitude=True, direction=-1)
            streamlines.integrate_through_volume()
            streamlines.clean_streamlines()
            #filter
            keep_streamline = list(map(self._filter_streamlines,streamlines.streamlines))
            if True not in keep_streamline:
                i_tries += 1
                self.logger.debug('No acceptable streamlines found. # of tries left = {}'.format(max_tries-i_tries))
                continue
            else:
                i_tries = 0
            #save strealines
            self.streamlines += [(stream[np.all(stream != 0.0, axis=1)],mag) for stream,mag,keep in zip(streamlines.streamlines,streamlines.magnitudes,keep_streamline) if keep is True]

        if i_tries == max_tries:
            self.logger.warning('Maxed out number of tries. Only found {} acceptable streamlines'.format(len(self.streamlines)))


    def peek(self,figsize=(10,10),color=sns.color_palette('deep')[0],alpha=0.75,print_to_file=None,**kwargs):
        """
        Show extracted fieldlines overlaid on HMI image.
        """

        fig = plt.figure(figsize=figsize)
        ax = fig.gca(projection=self.hmi_map)
        self.hmi_map.plot()
        ax.set_autoscale_on(False)
        for stream,_ in self.streamlines:
            ax.plot(self._convert_angle_to_length(stream[:,0]*u.cm,working_units=u.arcsec).to(u.deg),
                    self._convert_angle_to_length(stream[:,1]*u.cm,working_units=u.arcsec).to(u.deg),
                    alpha=alpha,color=color,transform=ax.get_transform('world'))

        if print_to_file is not None:
            plt.savefig(print_to_file,**kwargs)
        plt.show()
