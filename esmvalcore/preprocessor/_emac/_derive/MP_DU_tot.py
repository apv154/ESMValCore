"""Derivation of sum over all dust tracers"""

from . import var_name_constraint

def derive(cubes):
        MP_DU_tot_cube = cubes.extract_strict(var_name_constraint('MP_DU_as_ave'))+cubes.extract_strict(var_name_constraint('MP_DU_ai_ave'))+
                         cubes.extract_strict(var_name_constraint('MP_DU_ci_ave'))+ cubes.extract_strict(var_name_constraint('MP_DU_cs_ave'))
        return MP_DU_tot_cube
