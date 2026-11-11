INPUT_VR={'latitude': 0, 'longitude': 1, 'elevation': 2, 'slope_rad_input': 3, 'aspect_rad_input': 4, 'curvature_scaled_input': 5, 'lapse_rate': 6, 'vapor_coefficient': 7, 'gamma_s': 8, 'gamma_c': 9, 'hour_local': 10, 'day_of_year': 11, 'rh700': 12, 'barnes_filter_m2': 13, 'curvature_radius_m': 14, 'zW': 15, 'zE': 16, 'zS': 17, 'zN': 18, 'zSW': 19, 'zNE': 20, 'zNW': 21, 'zSE': 22, 'curvature_min': 23, 'curvature_max': 24, 'precipitation_chi_km': 25, 'use_dem_curvature': 26, 'source0_enabled': 27, 'source0_latitude': 28, 'source0_longitude': 29, 'source0_elevation': 30, 'source0_temperature': 31, 'source0_relative_humidity': 32, 'source0_wind_speed': 33, 'source0_wind_direction': 34, 'source0_precipitation': 35, 'source0_surface_pressure_hpa': 36, 'source1_enabled': 37, 'source1_latitude': 38, 'source1_longitude': 39, 'source1_elevation': 40, 'source1_temperature': 41, 'source1_relative_humidity': 42, 'source1_wind_speed': 43, 'source1_wind_direction': 44, 'source1_precipitation': 45, 'source1_surface_pressure_hpa': 46, 'source2_enabled': 47, 'source2_latitude': 48, 'source2_longitude': 49, 'source2_elevation': 50, 'source2_temperature': 51, 'source2_relative_humidity': 52, 'source2_wind_speed': 53, 'source2_wind_direction': 54, 'source2_precipitation': 55, 'source2_surface_pressure_hpa': 56, 'source3_enabled': 57, 'source3_latitude': 58, 'source3_longitude': 59, 'source3_elevation': 60, 'source3_temperature': 61, 'source3_relative_humidity': 62, 'source3_wind_speed': 63, 'source3_wind_direction': 64, 'source3_precipitation': 65, 'source3_surface_pressure_hpa': 66, 'source4_enabled': 67, 'source4_latitude': 68, 'source4_longitude': 69, 'source4_elevation': 70, 'source4_temperature': 71, 'source4_relative_humidity': 72, 'source4_wind_speed': 73, 'source4_wind_direction': 74, 'source4_precipitation': 75, 'source4_surface_pressure_hpa': 76, 'terrain_dzdx': 77, 'terrain_dzdy': 78, 'derive_slope_aspect_from_gradients': 79, 'barnes_second_pass_enabled': 80, 'barnes_second_pass_radius_factor': 81, 'calibration_enabled': 82, 'temperature_bias_c': 83, 'relative_humidity_bias_pct': 84, 'wind_speed_scale': 85, 'wind_direction_bias_deg': 86, 'shortwave_scale': 87, 'pressure_bias_pa': 88, 'shortwave_reference_wm2': 89, 'shortwave_reference_blend': 90}
OUTPUT_NAMES=['barnes_weight', 'temperature_reference', 'temperature_local', 'saturation_vapor_pressure', 'actual_vapor_pressure', 'dewpoint_local', 'dewpoint_lapse_rate', 'relative_humidity_local', 'wind_u', 'wind_v', 'wind_speed_background', 'wind_direction_background_rad', 'slope_rad', 'aspect_rad', 'curvature_scaled', 'slope_in_wind_direction', 'wind_weight', 'wind_speed_terrain', 'wind_direction_diversion', 'wind_direction_terrain_rad', 'wind_u_terrain', 'wind_v_terrain', 'cloud_fraction', 'solar_zenith', 'solar_hour_angle', 'solar_declination', 'solar_incidence', 'solar_azimuth', 'direct_transmissivity', 'diffuse_transmissivity', 'shortwave_radiation', 'atmospheric_emissivity', 'longwave_radiation', 'surface_pressure_pa', 'precipitation_local', 'barnes_distance_m', 'saturation_vapor_pressure_source', 'dewpoint_source', 'dewpoint_reference', 'saturation_vapor_pressure_local', 'actual_vapor_pressure_local', 'curvature_raw', 'slope_in_wind_direction_raw', 'rh700_used', 'coefficient_Xs', 'coefficient_Ys', 'coefficient_Zs', 'surface_pressure_forecast_pa', 'source_elevation_barnes', 'precipitation_reference', 'enabled_source_count', 'formula_valid', 'formula_error_code', 'fmi_time_s', 'terrain_dzdx_used', 'terrain_dzdy_used', 'barnes_second_pass_applied', 'raw_temperature_local', 'raw_relative_humidity_local', 'raw_wind_speed_terrain', 'raw_wind_direction_terrain_rad', 'raw_shortwave_radiation', 'raw_surface_pressure_pa']
OUTPUT_START=100

# MicroMet-3DW Extension. Legacy valueReferences remain unchanged.
INPUT_VR.update({
    'wind_3dw_enabled':170, 'altitude_agl_m':171, 'fd_dx_m':172, 'fd_dy_m':173, 'fd_dz_m':174,
    'temporal_t0_u10':175, 'temporal_t0_v10':176, 'temporal_t1_u10':177, 'temporal_t1_v10':178,
    'temporal_interval_s':179, 'temporal_alpha':180, 'profile_level_count':181,
    'wind_profile_power':207,
})
for _i in range(5):
    _b=182+5*_i
    INPUT_VR.update({
        f'profile{_i}_agl_m':_b, f'profile{_i}_u_t0':_b+1, f'profile{_i}_v_t0':_b+2,
        f'profile{_i}_u_t1':_b+3, f'profile{_i}_v_t1':_b+4,
    })
WIND3DW_OUTPUT_NAMES=[
    'wind_u_3dw','wind_v_3dw','diagnostic_vertical_wind','wind_speed_3d',
    'du_dx','du_dy','du_dz','dv_dx','dv_dy','dv_dz','dw_dx','dw_dy','dw_dz',
    'du_dt','dv_dt','dw_dt','Du_Dt','Dv_Dt','Dw_Dt','continuity_residual',
    'vertical_interp_lower_level','vertical_interp_upper_level','temporal_interp_t0_s','temporal_interp_t1_s',
    'wind_3dw_valid','wind_3dw_profile_mode'
]
WIND3DW_OUTPUT_START=220
OUTPUT_VR={name:OUTPUT_START+i for i,name in enumerate(OUTPUT_NAMES)}
OUTPUT_VR.update({name:WIND3DW_OUTPUT_START+i for i,name in enumerate(WIND3DW_OUTPUT_NAMES)})
OUTPUT_VR['air_density']=246
OUTPUT_NAMES = OUTPUT_NAMES + WIND3DW_OUTPUT_NAMES + ['air_density']
