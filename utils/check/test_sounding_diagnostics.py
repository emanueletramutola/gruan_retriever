"""
Tests for processors.sounding_diagnostics
"""
import unittest
from unittest.mock import patch
import numpy as np


def _make_synthetic_sounding(n=100):
    """
    Build a simple synthetic sounding dictionary (raw, without MetPy units)
    that mimics what comes out of read_netcdf.

    Profile: sea-level to ~20 km using the standard atmosphere approximation.
    """
    z = np.linspace(0, 20000, n)           # altitude in metres
    # ISA temperature lapse: -6.5 K/km in troposphere, constant in stratosphere
    T = np.where(z < 11000, 15 - 6.5e-3 * z, -56.5)  # °C
    # Hydrostatic pressure approximation
    press = 1013.25 * np.exp(-z / 8500)    # hPa
    rh = np.where(z < 5000, 80 - z * 1e-3, 20)  # %
    wzon = np.full(n, 5.0)                 # m/s eastward
    wmeri = np.full(n, 2.0)               # m/s northward
    alt_amsl = z                           # same as z for simplicity
    # Simulate a few NaN entries that the function should handle gracefully
    press[0] = np.nan
    T[0] = np.nan

    return {
        'press': press,
        'temp': T,
        'rh': rh,
        'wzon': wzon,
        'wmeri': wmeri,
        'alt_amsl': alt_amsl,
    }


class TestEnrichDataWithDiagnostics(unittest.TestCase):

    def setUp(self):
        from processors.sounding_diagnostics import enrich_data_with_diagnostics
        self.enrich = enrich_data_with_diagnostics

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _run(self, data=None, metadata=None):
        if data is None:
            data = _make_synthetic_sounding()
        if metadata is None:
            metadata = {}
        return self.enrich(data, metadata)

    # ------------------------------------------------------------------
    # Basic profile outputs
    # ------------------------------------------------------------------

    def test_profile_keys_added_to_data(self):
        data, _ = self._run()
        for key in ('diag_dewpoint', 'diag_theta', 'diag_theta_e',
                    'diag_parcel_profile'):
            self.assertIn(key, data, f"Profile key '{key}' missing from data")

    def test_profile_arrays_are_numpy(self):
        data, _ = self._run()
        for key in ('diag_dewpoint', 'diag_theta', 'diag_theta_e',
                    'diag_parcel_profile'):
            self.assertIsInstance(data[key], np.ndarray,
                                  f"data['{key}'] should be a numpy array")

    # ------------------------------------------------------------------
    # Basic scalar outputs
    # ------------------------------------------------------------------

    def test_scalar_keys_added_to_metadata(self):
        _, metadata = self._run()
        expected_scalars = [
            'diag_cape', 'diag_cin',
            'diag_lifted_index', 'diag_k_index', 'diag_total_totals',
            'diag_pwat',
            'diag_cloud_base_type',
        ]
        for key in expected_scalars:
            self.assertIn(key, metadata,
                          f"Scalar key '{key}' missing from metadata")

    def test_cape_is_non_negative(self):
        _, metadata = self._run()
        cape = metadata.get('diag_cape')
        if cape is not None:
            self.assertGreaterEqual(cape, 0.0)

    def test_cloud_base_type_is_string(self):
        _, metadata = self._run()
        cbt = metadata.get('diag_cloud_base_type')
        self.assertIsInstance(cbt, str)

    # ------------------------------------------------------------------
    # Wind diagnostics
    # ------------------------------------------------------------------

    def test_wind_scalars_present_when_wind_available(self):
        data, metadata = self._run()
        for key in ('diag_shear_0_6km_u', 'diag_shear_0_6km_v',
                    'diag_srh_0_3km_total'):
            self.assertIn(key, metadata,
                          f"Wind scalar '{key}' missing from metadata")

    def test_wind_scalars_absent_when_wind_missing(self):
        data = _make_synthetic_sounding()
        del data['wzon']
        del data['wmeri']
        _, metadata = self._run(data=data)
        self.assertNotIn('diag_shear_0_6km_u', metadata)

    # ------------------------------------------------------------------
    # Robustness
    # ------------------------------------------------------------------

    def test_missing_required_variable_returns_unchanged(self):
        """If press/temp/rh is missing the original dicts are returned intact."""
        data = {'alt': np.linspace(0, 20000, 50)}
        metadata = {'foo': 'bar'}
        out_data, out_meta = self._run(data=data, metadata=metadata)
        self.assertEqual(out_meta, {'foo': 'bar'})
        self.assertNotIn('diag_cape', out_meta)

    def test_all_nan_returns_unchanged(self):
        """If all values are NaN the function should not raise."""
        data = {
            'press': np.full(50, np.nan),
            'temp': np.full(50, np.nan),
            'rh': np.full(50, np.nan),
            'alt_amsl': np.full(50, np.nan),
        }
        metadata = {}
        # Should not raise
        out_data, out_meta = self._run(data=data, metadata=metadata)
        # No profile keys should have been added
        self.assertNotIn('diag_dewpoint', out_data)

    def test_original_data_not_modified_on_failure(self):
        """Partial data should leave original data dict intact."""
        data = {'press': np.array([1000.0])}  # only one variable
        metadata = {'station': 'TEST'}
        out_data, out_meta = self._run(data=data, metadata=metadata)
        self.assertIn('station', out_meta)
        self.assertEqual(out_meta['station'], 'TEST')

    def test_metadata_has_no_metpy_units_objects(self):
        """All scalar values in metadata must be plain Python scalars (or None)."""
        _, metadata = self._run()
        for key, val in metadata.items():
            if key.startswith('diag_') and val is not None:
                self.assertNotIn('pint', type(val).__module__,
                                 f"metadata['{key}'] still has pint units: {type(val)}")


if __name__ == '__main__':
    unittest.main()
