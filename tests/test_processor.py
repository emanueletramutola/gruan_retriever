import unittest
from unittest.mock import MagicMock, patch, call
import pandas as pd
from processors.processor import GRUANProcessor

class TestGRUANProcessor(unittest.TestCase):

    @patch('processors.processor.DatabaseOperations')
    @patch('processors.processor.DataFrameConverter')
    def setUp(self, mock_converter_cls, mock_db_ops_cls):
        self.mock_db_ops = mock_db_ops_cls.return_value
        self.mock_converter = mock_converter_cls.return_value
        
        # Mock cached columns
        self.mock_db_ops.get_columns_excluding_serial_pk_cached.return_value = ['col1', 'col2']
        
        self.processor = GRUANProcessor('RS92')

    @patch('processors.processor.zipfile.ZipFile')
    def test_process_single_jar_file_no_files(self, mock_zip_cls):
        mock_zip = MagicMock()
        mock_zip_cls.return_value.__enter__.return_value = mock_zip
        mock_zip.namelist.return_value = ['META-INF/', 'other.txt']
        
        jar_file = MagicMock()
        jar_file.name = "test.jar"
        
        result = self.processor.process_single_jar_file(jar_file)
        self.assertFalse(result)

    @patch('processors.processor.zipfile.ZipFile')
    def test_process_single_jar_file_with_revisions(self, mock_zip_cls):
        mock_zip = MagicMock()
        mock_zip_cls.return_value.__enter__.return_value = mock_zip
        # Simulate files with revisions
        mock_zip.namelist.return_value = [
            'rev001/data.nc',
            'rev002/data.nc', # Should select this one
            'META-INF/'
        ]
        
        jar_file = MagicMock()
        jar_file.name = "test.jar"
        
        # Mock process_single_nc_file to return success
        with patch.object(self.processor, 'process_single_nc_file') as mock_process:
            mock_process.return_value = (True, None)
            
            result = self.processor.process_single_jar_file(jar_file)
            
            self.assertTrue(result)
            # Should only process the file from rev002
            mock_process.assert_called_once()
            args, _ = mock_process.call_args
            self.assertEqual(args[1], 'rev002/data.nc')

    @patch('processors.processor.zipfile.ZipFile')
    def test_process_single_jar_file_root_files(self, mock_zip_cls):
        mock_zip = MagicMock()
        mock_zip_cls.return_value.__enter__.return_value = mock_zip
        mock_zip.namelist.return_value = ['data.nc']
        
        jar_file = MagicMock()
        jar_file.name = "test.jar"
        
        with patch.object(self.processor, 'process_single_nc_file') as mock_process:
            mock_process.return_value = (True, None)
            
            result = self.processor.process_single_jar_file(jar_file)
            
            self.assertTrue(result)
            mock_process.assert_called_once()
            args, _ = mock_process.call_args
            self.assertEqual(args[1], 'data.nc')

    @patch('processors.processor.read_netcdf')
    def test_process_single_nc_file_success(self, mock_read_netcdf):
        # Mock read_netcdf return
        mock_read_netcdf.return_value = ({'data': []}, {'meta': 'data'})
        
        # Mock converter return
        mock_header_df = pd.DataFrame({'id': [1]})
        mock_data_df = pd.DataFrame({'value': [10.0]})
        self.mock_converter.convert_to_dataframe.return_value = (
            mock_header_df, mock_data_df, None
        )
        
        # Mock save_data_copy_method
        with patch.object(self.processor, 'save_data_copy_method') as mock_save:
            mock_save.return_value = True
            
            result, error = self.processor.process_single_nc_file(
                MagicMock(), "data.nc", "test.jar"
            )
            
            self.assertTrue(result)
            self.assertIsNone(error)
            mock_save.assert_called_once()

    @patch('processors.processor.read_netcdf')
    def test_process_single_nc_file_skip(self, mock_read_netcdf):
        mock_read_netcdf.return_value = ({}, {})
        
        # Mock converter returning skip reason
        self.mock_converter.convert_to_dataframe.return_value = (
            pd.DataFrame(), pd.DataFrame(), "Station not found"
        )
        
        result, error = self.processor.process_single_nc_file(
            MagicMock(), "data.nc", "test.jar"
        )
        
        self.assertFalse(result)
        self.assertIn("Station not found", error)

    @patch('processors.processor.get_connection')
    def test_save_data_copy_method(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        df_header = pd.DataFrame({'col': [1]})
        df_data = pd.DataFrame({'col': [1]})
        
        result = self.processor.save_data_copy_method(df_header, df_data, "test.nc")
        
        self.assertTrue(result)
        self.assertEqual(self.mock_db_ops.bulk_insert_copy.call_count, 2)
        self.mock_db_ops.update_import_status.assert_called_once()
