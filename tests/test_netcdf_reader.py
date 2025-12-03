import unittest
from unittest.mock import MagicMock, patch
import numpy as np
import pytest
from readers.netcdf_reader import read_netcdf, _process_variable_data, _process_attribute_value

class TestNetCDFReader(unittest.TestCase):

    def setUp(self):
        self.mock_zip_ref = MagicMock()
        self.mock_file = MagicMock()
        self.mock_zip_ref.open.return_value.__enter__.return_value = self.mock_file

    def test_read_netcdf_invalid_filename(self):
        with self.assertRaises(ValueError):
            read_netcdf(self.mock_zip_ref, "")
        with self.assertRaises(ValueError):
            read_netcdf(self.mock_zip_ref, None)

    def test_read_netcdf_none_zip_ref(self):
        with self.assertRaises(ValueError):
            read_netcdf(None, "test.nc")

    def test_read_netcdf_file_not_found(self):
        self.mock_zip_ref.open.side_effect = KeyError("File not found")
        with self.assertRaises(ValueError):
            read_netcdf(self.mock_zip_ref, "nonexistent.nc")

    @patch('readers.netcdf_reader.Dataset')
    def test_read_netcdf_success(self, mock_dataset_cls):
        # Setup mock dataset
        mock_ds = MagicMock()
        mock_dataset_cls.return_value.__enter__.return_value = mock_ds
        
        # Mock variables
        mock_var = MagicMock()
        mock_var.__getitem__.return_value = np.array([1.0, 2.0, 3.0])
        mock_ds.variables = {'temp': mock_var}
        
        # Mock attributes
        mock_ds.ncattrs.return_value = ['global_attr']
        mock_ds.getncattr.return_value = 'test_value'

        data, metadata = read_netcdf(self.mock_zip_ref, "test.nc")

        self.assertIn('temp', data)
        self.assertIn('global_attr', metadata)
        self.assertEqual(metadata['global_attr'], 'test_value')

    def test_process_variable_data_masked(self):
        masked_data = np.ma.masked_array([1.0, 2.0, 3.0], mask=[0, 1, 0])
        processed = _process_variable_data(masked_data)
        self.assertTrue(np.isnan(processed[1]))
        self.assertEqual(processed[0], 1.0)

    def test_process_variable_data_small_values(self):
        # 1e-40 is smaller than POSTGRES_REAL_MIN (approx 1.17e-38)
        data = np.array([1.0, 1e-40, -1e-40]) 
        processed = _process_variable_data(data)
        self.assertEqual(processed[1], 0.0)
        self.assertEqual(processed[2], 0.0)
        self.assertEqual(processed[0], 1.0)

    def test_process_variable_data_strings(self):
        data = np.array(['hello\nworld'], dtype='U')
        processed = _process_variable_data(data)
        self.assertEqual(processed[0], 'hello world')

    def test_process_attribute_value(self):
        self.assertEqual(_process_attribute_value('hello\nworld'), 'hello world')
        self.assertEqual(_process_attribute_value(123), 123)
