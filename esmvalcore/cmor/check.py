"""Module for checking iris cubes against their CMOR definitions."""
import logging

import cf_units
import iris.coord_categorisation
import iris.coords
import iris.exceptions
import iris.util
import numpy as np

from .table import CMOR_TABLES


class CMORCheckError(Exception):
    """Exception raised when a cube does not pass the CMORCheck."""


class CMORCheck():
    """Class used to check the CMOR-compliance of the data.

    It can also fix some minor errors and does some minor data
    homogeneization:

    Parameters
    ----------
    cube: iris.cube.Cube:
        Iris cube to check.
    var_info: variables_info.VariableInfo
        Variable info to check.
    frequency: str
        Expected frequency for the data.
    fail_on_error: bool
        If true, CMORCheck stops on the first error. If false, it collects
        all possible errors before stopping.
    automatic_fixes: bool
        If True, CMORCheck will try to apply automatic fixes for any
        detected error, if possible.

    Attributes
    ----------
    frequency: str
        Expected frequency for the data.
    automatic_fixes: bool
        If True, CMORCheck will try to apply automatic fixes for any
        detected error, if possible.

    """

    _attr_msg = '{}: {} should be {}, not {}'
    _does_msg = '{}: does not {}'
    _is_msg = '{}: is not {}'
    _vals_msg = '{}: has values {} {}'
    _contain_msg = '{}: does not contain {} {}'

    def __init__(self,
                 cube,
                 var_info,
                 frequency=None,
                 fail_on_error=False,
                 automatic_fixes=False):

        self._cube = cube
        self._failerr = fail_on_error
        self._errors = list()
        self._warnings = list()
        self._cmor_var = var_info
        if frequency is None:
            frequency = self._cmor_var.frequency
        self.frequency = frequency
        self.automatic_fixes = automatic_fixes

    def check_metadata(self, logger=None):
        """
        Check the cube metadata.

        Perform all the tests that do not require to have the data in memory.

        It will also report some warnings in case of minor errors and
        homogenize some data:

            - Equivalent calendars will all default to the same name.
            - Auxiliary coordinates year, month_number, day_of_month and
              day_of_year will be added for the time axis.

        Raises
        ------
        CMORCheckError
            If errors are found. If fail_on_error attribute is set to True,
            raises as soon as an error is detected. If set to False, it perform
            all checks and then raises.

        """
        if logger is None:
            logger = logging.getLogger(__name__)

        self._check_var_metadata()
        self._check_fill_value()
        self._check_dim_names()
        self._check_coords()
        self._check_time_coord()
        self._check_rank()

        self.report_warnings(logger)
        self.report_errors()

        self._add_auxiliar_time_coordinates()
        return self._cube

    def report_errors(self):
        """Report detected errors.

        Raises
        ------
        CMORCheckError
            If any errors were reported before calling this method.

        """
        if self.has_errors():
            msg = 'There were errors in variable {}:\n{}\nin cube:\n{}'
            msg = msg.format(self._cube.var_name, '\n '.join(self._errors),
                             self._cube)
            raise CMORCheckError(msg)

    def report_warnings(self, logger):
        """Report detected warnings to the given logger.

        Parameters
        ----------
        logger

        """
        if self.has_warnings():
            msg = 'There were warnings in variable {}:\n{}\n'.format(
                self._cube.var_name, '\n '.join(self._warnings))
            logger.warning(msg)

    def check_data(self, logger=None):
        """Check the cube data.

        Performs all the tests that require to have the data in memory.
        Assumes that metadata is correct, so you must call check_metadata prior
        to this.

        It will also report some warnings in case of minor errors.

        Raises
        ------
        CMORCheckError
            If errors are found. If fail_on_error attribute is set to True,
            raises as soon as an error is detected. If set to False, it perform
            all checks and then raises.

        """
        if logger is None:
            logger = logging.getLogger(__name__)

        if self._cmor_var.units:
            units = self._get_effective_units()
            if str(self._cube.units) != units:
                self._cube.convert_units(units)

        self._check_coords_data()

        self.report_warnings(logger)
        self.report_errors()
        return self._cube

    def _check_fill_value(self):
        """Check fill value."""
        # Iris removes _FillValue/missing_value information if data has none
        #  of these values. If there are values == _FillValue then it will
        #  be encoded in the numpy.ma object created.
        #
        #  => Very difficult to check!

    def _check_var_metadata(self):
        """Check metadata of variable."""
        # Check standard_name
        if self._cmor_var.standard_name:
            if self._cube.standard_name != self._cmor_var.standard_name:
                self.report_error(
                    self._attr_msg, self._cube.var_name, 'standard_name',
                    self._cmor_var.standard_name, self._cube.standard_name)

        # Check units
        if (self.automatic_fixes and self._cube.attributes.get(
                'invalid_units', '').lower() == 'psu'):
            self._cube.units = '1.0'
            del self._cube.attributes['invalid_units']

        if self._cmor_var.units:
            units = self._get_effective_units()

            if not self._cube.units.is_convertible(units):
                self.report_error(f'Variable {self._cube.var_name} units '
                                  f'{self._cube.units} can not be '
                                  f'converted to {self._cmor_var.units}')

        # Check other variable attributes that match entries in cube.attributes
        attrs = ('positive', )
        for attr in attrs:
            attr_value = getattr(self._cmor_var, attr)
            if attr_value:
                if attr not in self._cube.attributes:
                    self.report_warning('{}: attribute {} not present',
                                        self._cube.var_name, attr)
                elif self._cube.attributes[attr] != attr_value:
                    self.report_error(self._attr_msg, self._cube.var_name,
                                      attr, attr_value,
                                      self._cube.attributes[attr])

    def _get_effective_units(self):
        """Get effective units."""
        if self._cmor_var.units.lower() == 'psu':
            units = '1.0'
        else:
            units = self._cmor_var.units
        return units

    def _check_rank(self):
        """Check rank, excluding scalar dimensions."""
        rank = 0
        dimensions = []
        for coordinate in self._cmor_var.coordinates.values():
            if coordinate.generic_level:
                rank += 1
            elif not coordinate.value:
                try:
                    for dim in self._cube.coord_dims(coordinate.standard_name):
                        dimensions.append(dim)
                except iris.exceptions.CoordinateNotFoundError:
                    # Error reported at other stages
                    pass
        rank += len(set(dimensions))

        # Check number of dimension coords matches rank
        if self._cube.ndim != rank:
            self.report_error(self._does_msg, self._cube.var_name,
                              'match coordinate rank')

    def _check_dim_names(self):
        """Check dimension names."""
        for (_, coordinate) in self._cmor_var.coordinates.items():
            if coordinate.generic_level:
                continue
            else:
                try:
                    cube_coord = self._cube.coord(var_name=coordinate.out_name)
                    if cube_coord.standard_name != coordinate.standard_name:
                        self.report_error(
                            self._attr_msg,
                            coordinate.out_name,
                            'standard_name',
                            coordinate.standard_name,
                            cube_coord.standard_name,
                        )
                except iris.exceptions.CoordinateNotFoundError:
                    try:
                        coord = self._cube.coord(coordinate.standard_name)
                        self.report_error(
                            'Coordinate {0} has var name {1} instead of {2}',
                            coordinate.name,
                            coord.var_name,
                            coordinate.out_name,
                        )
                    except iris.exceptions.CoordinateNotFoundError:
                        self.report_error(self._does_msg, coordinate.name,
                                          'exist')

    def _check_coords(self):
        """Check coordinates."""
        for coordinate in self._cmor_var.coordinates.values():
            # Cannot check generic_level coords as no CMOR information
            if coordinate.generic_level:
                continue
            var_name = coordinate.out_name

            # Get coordinate var_name as it exists!
            try:
                coord = self._cube.coord(var_name=var_name, dim_coords=True)
            except iris.exceptions.CoordinateNotFoundError:
                continue

            self._check_coord(coordinate, coord, var_name)

    def _check_coords_data(self):
        """Check coordinate data."""
        for coordinate in self._cmor_var.coordinates.values():
            # Cannot check generic_level coords as no CMOR information
            if coordinate.generic_level:
                continue
            var_name = coordinate.out_name

            # Get coordinate var_name as it exists!
            try:
                coord = self._cube.coord(var_name=var_name, dim_coords=True)
            except iris.exceptions.CoordinateNotFoundError:
                continue

            self._check_coord_monotonicity_and_direction(
                coordinate, coord, var_name)

    def _check_coord(self, cmor, coord, var_name):
        """Check single coordinate."""
        if coord.var_name == 'time':
            return
        if cmor.units:
            if str(coord.units) != cmor.units:
                fixed = False
                if self.automatic_fixes:
                    try:
                        new_unit = cf_units.Unit(cmor.units,
                                                 coord.units.calendar)
                        coord.convert_units(new_unit)
                        fixed = True
                    except ValueError:
                        pass
                if not fixed:
                    self.report_error(self._attr_msg, var_name, 'units',
                                      cmor.units, coord.units)
        self._check_coord_values(cmor, coord, var_name)
        if not self.automatic_fixes:
            self._check_coord_monotonicity_and_direction(cmor, coord, var_name)

    def _check_coord_monotonicity_and_direction(self, cmor, coord, var_name):
        """Check monotonicity and direction of coordinate."""
        if not coord.is_monotonic():
            self.report_error(self._is_msg, var_name, 'monotonic')
        if len(coord.points) == 1:
            return
        if cmor.stored_direction:
            if cmor.stored_direction == 'increasing':
                if coord.points[0] > coord.points[1]:
                    if not self.automatic_fixes or coord.ndim > 1:
                        self.report_error(self._is_msg, var_name, 'increasing')
                    else:
                        self._reverse_coord(coord)
            elif cmor.stored_direction == 'decreasing':
                if coord.points[0] < coord.points[1]:
                    if not self.automatic_fixes or coord.ndim > 1:
                        self.report_error(self._is_msg, var_name, 'decreasing')
                    else:
                        self._reverse_coord(coord)

    def _reverse_coord(self, coord):
        """Reverse coordinate."""
        if coord.ndim == 1:
            self._cube = iris.util.reverse(self._cube,
                                           self._cube.coord_dims(coord))

    def _check_coord_values(self, coord_info, coord, var_name):
        """Check coordinate values."""
        # Check requested coordinate values exist in coord.points
        self._check_requested_values(coord, coord_info, var_name)

        l_fix_coord_value = False

        # Check coordinate value ranges
        if coord_info.valid_min:
            valid_min = float(coord_info.valid_min)
            if np.any(coord.points < valid_min):
                if coord_info.standard_name == 'longitude' and \
                        self.automatic_fixes:
                    l_fix_coord_value = True
                else:
                    self.report_error(self._vals_msg, var_name,
                                      '< {} ='.format('valid_min'), valid_min)

        if coord_info.valid_max:
            valid_max = float(coord_info.valid_max)
            if np.any(coord.points > valid_max):
                if coord_info.standard_name == 'longitude' and \
                        self.automatic_fixes:
                    l_fix_coord_value = True
                else:
                    self.report_error(self._vals_msg, var_name,
                                      '> {} ='.format('valid_max'), valid_max)

        if l_fix_coord_value:
            lon_extent = iris.coords.CoordExtent(coord, 0.0, 360., True, False)
            self._cube = self._cube.intersection(lon_extent)

    def _check_requested_values(self, coord, coord_info, var_name):
        """Check requested values."""
        if coord_info.requested:
            cmor_points = [float(val) for val in coord_info.requested]
            coord_points = list(coord.points)
            for point in cmor_points:
                if point not in coord_points:
                    self.report_warning(self._contain_msg, var_name,
                                        str(point), str(coord.units))

    def _check_time_coord(self):
        """Check time coordinate."""
        try:
            coord = self._cube.coord('time', dim_coords=True)  # , axis='T')
            var_name = coord.var_name
        except iris.exceptions.CoordinateNotFoundError:
            return

        if not coord.units.is_time_reference():
            self.report_error(self._does_msg, var_name,
                              'have time reference units')
        else:
            coord.convert_units(
                cf_units.Unit(
                    'days since 1950-1-1 00:00:00',
                    calendar=coord.units.calendar))
            simplified_cal = self._simplify_calendar(coord.units.calendar)
            coord.units = cf_units.Unit(coord.units.origin, simplified_cal)

        tol = 0.001
        intervals = {'dec': (3600, 3660), 'day': (1, 1)}
        if self.frequency == 'mon':
            for i in range(len(coord.points) - 1):
                first = coord.cell(i).point
                second = coord.cell(i + 1).point
                second_month = first.month + 1
                second_year = first.year
                if second_month == 13:
                    second_month = 1
                    second_year += 1
                if second_month != second.month or \
                   second_year != second.year:
                    msg = '{}: Frequency {} does not match input data'
                    self.report_error(msg, var_name, self.frequency)
                    break
        elif self.frequency == 'yr':
            for i in range(len(coord.points) - 1):
                first = coord.cell(i).point
                second = coord.cell(i + 1).point
                second_month = first.month + 1
                if first.year + 1 != second.year:
                    msg = '{}: Frequency {} does not match input data'
                    self.report_error(msg, var_name, self.frequency)
                    break
        else:
            if self.frequency in intervals:
                interval = intervals[self.frequency]
                target_interval = (interval[0] - tol, interval[1] + tol)
            elif self.frequency.endswith('hr'):
                frequency = self.frequency[:-2]
                if frequency == 'sub':
                    frequency = 1.0 / 24
                    target_interval = (-tol, frequency + tol)
                else:
                    frequency = float(frequency) / 24
                    target_interval = (frequency - tol, frequency + tol)
            else:
                msg = '{}: Frequency {} not supported by checker'
                self.report_error(msg, var_name, self.frequency)
                return
            for i in range(len(coord.points) - 1):
                interval = coord.points[i + 1] - coord.points[i]
                if (interval < target_interval[0]
                        or interval > target_interval[1]):
                    msg = '{}: Frequency {} does not match input data'
                    self.report_error(msg, var_name, self.frequency)
                    break

    @staticmethod
    def _simplify_calendar(calendar):
        calendar_aliases = {
            'all_leap': '366_day',
            'noleap': '365_day',
            'standard': 'gregorian',
        }
        return calendar_aliases.get(calendar, calendar)

    def has_errors(self):
        """Check if there are reported errors.

        Returns
        -------
        bool:
            True if there are pending errors, False otherwise.

        """
        return len(self._errors) > 0

    def has_warnings(self):
        """Check if there are reported warnings.

        Returns
        -------
        bool:
            True if there are pending warnings, False otherwise.

        """
        return len(self._warnings) > 0

    def report_error(self, message, *args):
        """Report an error.

        If fail_on_error is set to True, raises automatically.
        If fail_on_error is set to False, stores it for later reports.

        Parameters
        ----------
        message: str: unicode
            Message for the error.
        *args:
            arguments to format the message string.

        """
        msg = message.format(*args)
        if self._failerr:
            raise CMORCheckError(msg + '\nin cube:\n{}'.format(self._cube))
        self._errors.append(msg)

    def report_warning(self, message, *args):
        """Report a warning.

        If fail_on_error is set to True, logs it automatically.
        If fail_on_error is set to False, stores it for later reports.

        Parameters
        ----------
        message: str: unicode
            Message for the warning.
        *args:
            arguments to format the message string.

        """
        msg = message.format(*args)
        if self._failerr:
            print('WARNING: {0}'.format(msg))
        else:
            self._warnings.append(msg)

    def _add_auxiliar_time_coordinates(self):
        coords = [coord.name() for coord in self._cube.aux_coords]
        if 'day_of_month' not in coords:
            iris.coord_categorisation.add_day_of_month(self._cube, 'time')
        if 'day_of_year' not in coords:
            iris.coord_categorisation.add_day_of_year(self._cube, 'time')
        if 'month_number' not in coords:
            iris.coord_categorisation.add_month_number(self._cube, 'time')
        if 'year' not in coords:
            iris.coord_categorisation.add_year(self._cube, 'time')


def _get_cmor_checker(table,
                      mip,
                      short_name,
                      frequency,
                      fail_on_error=True,
                      automatic_fixes=False):
    """Get a CMOR checker/fixer."""
    if table not in CMOR_TABLES:
        raise NotImplementedError(
            "No CMOR checker implemented for table {}."
            "\nThe following options are available: {}".format(
                table, ', '.join(CMOR_TABLES)))

    cmor_table = CMOR_TABLES[table]
    var_info = cmor_table.get_variable(mip, short_name)
    if var_info is None:
        var_info = CMOR_TABLES['custom'].get_variable(mip, short_name)

    def _checker(cube):
        return CMORCheck(
            cube,
            var_info,
            frequency=frequency,
            fail_on_error=fail_on_error,
            automatic_fixes=automatic_fixes)

    return _checker


def cmor_check_metadata(cube, cmor_table, mip, short_name, frequency):
    """Check if metadata conforms to variable's CMOR definiton.

    None of the checks at this step will force the cube to load the data.

    Parameters
    ----------
    cube: iris.cube.Cube
        Data cube to check.
    cmor_table: basestring
        CMOR definitions to use.
    mip:
        Variable's mip.
    short_name: basestring
        Variable's short name.
    frequency: basestring
        Data frequency.

    """
    checker = _get_cmor_checker(cmor_table, mip, short_name, frequency)
    checker(cube).check_metadata()
    return cube


def cmor_check_data(cube, cmor_table, mip, short_name, frequency):
    """Check if data conforms to variable's CMOR definiton.

    The checks performed at this step require the data in memory.

    Parameters
    ----------
    cube: iris.cube.Cube
        Data cube to check.
    cmor_table: basestring
        CMOR definitions to use.
    mip:
        Variable's mip.
    short_name: basestring
        Variable's short name
    frequency: basestring
        Data frequency

    """
    checker = _get_cmor_checker(cmor_table, mip, short_name, frequency)
    checker(cube).check_data()
    return cube


def cmor_check(cube, cmor_table, mip, short_name, frequency):
    """Check if cube conforms to variable's CMOR definiton.

    Equivalent to calling cmor_check_metadata and cmor_check_data
    consecutively.

    Parameters
    ----------
    cube: iris.cube.Cube
        Data cube to check.
    cmor_table: basestring
        CMOR definitions to use.
    mip:
        Variable's mip.
    short_name: basestring
        Variable's short name.
    frequency: basestring
        Data frequency.

    """
    cmor_check_metadata(cube, cmor_table, mip, short_name, frequency)
    cmor_check_data(cube, cmor_table, mip, short_name, frequency)
    return cube
