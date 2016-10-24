"""
Interface between loop object and ebtel++ simulation
"""

import os
import logging
import copy

import numpy as np
from scipy.interpolate import splprep,splev
import astropy.units as u

from synthesizAR.util import InputHandler,OutputHandler


class EbtelInterface(object):
    """
    Interface between field/loop model for the EBTEL model

    Parameters
    ----------
    base_config : `dict`
        Config dictionary with default parameters for all loops.
    heating_model
    """

    def __init__(self,base_config,heating_model):
        """
        Create EBTEL interface
        """
        self.logger = logging.getLogger(name=type(self).__name__)
        self.base_config = base_config
        self.heating_model = heating_model
        self.heating_model.base_config = base_config


    def configure_input(self,loop,parent_config_dir,parent_results_dir):
        """
        Configure EBTEL input for a given loop object.

        Parameters
        ----------
        loop
        parent_config_dir : `string`
        parent_results_dir : `string`
        """
        oh = OutputHandler(os.path.join(parent_config_dir,loop.name+'.xml'), copy.deepcopy(self.base_config))
        oh.output_dict['output_filename'] = os.path.join(parent_results_dir,loop.name)
        oh.output_dict['loop_length'] = loop.full_length.value/2.0
        event_properties = self.heating_model.calculate_event_properties(loop)
        events = []
        for i in range(self.heating_model.number_events):
            events.append({'event':{
            'magnitude':event_properties['magnitude'][i],
            'rise_start':event_properties['rise_start'][i],
            'rise_end':event_properties['rise_end'][i],
            'decay_start':event_properties['decay_start'][i],
            'decay_end':event_properties['decay_end'][i]}})
        oh.output_dict['heating']['events'] = events
        oh.print_to_xml()
        oh.output_dict['config_filename'] = oh.output_filename
        loop.hydro_configuration = oh.output_dict


    def load_results(self,loop):
        """
        Load EBTEL output for a given loop object.

        Parameters
        ----------
        loop
        """
        #load in data and interpolate to universal time
        N_s = len(loop.field_aligned_coordinate)
        _tmp = np.loadtxt(loop.hydro_configuration['output_filename'])

        loop.time = _tmp[:,0]*u.s

        temperature = np.outer(_tmp[:,1],np.ones(N_s))*u.K
        density = np.outer(_tmp[:,3],np.ones(N_s))*(u.cm**(-3))

        return temperature,density
