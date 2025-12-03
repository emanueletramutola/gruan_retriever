import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import pandas as pd
from utils.station_manager import StationManager, get_station_manager

class TestStationManager(unittest.TestCase):

    def setUp(self):
        # Reset singleton instance before each test
        StationManager._instance = None
        StationManager._cache = None

    def test_singleton(self):
        s1 = StationManager()
        s2 = StationManager()
        self.assertIs(s1, s2)
        self.assertIs(s1, get_station_manager())

    @patch('utils.station_manager.requests.get')
    def test_scrape_gruan_sites_success(self, mock_get):
        # Mock HTML response
        html_content = """
        <html>
        <body>
        <table>
            <tr><th>Header</th></tr>
            <tr>
                <td>LIN</td>
                <td>Lindenberg, Germany</td>
                <td>52.21°</td>
                <td>14.12°</td>
                <td>98m</td>
                <td>10393</td>
            </tr>
        </table>
        </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.content = html_content.encode('utf-8')
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        manager = StationManager()
        df = manager.scrape_gruan_sites()

        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]['idstation'], 'LIN')
        self.assertEqual(df.iloc[0]['name'], 'Lindenberg')
        self.assertEqual(df.iloc[0]['latitude'], 52.21)
        self.assertEqual(df.iloc[0]['longitude'], 14.12)
        self.assertEqual(df.iloc[0]['elevation'], 98.0)
        self.assertEqual(df.iloc[0]['wmoid'], 10393)

    @patch('utils.station_manager.requests.get')
    def test_scrape_gruan_sites_empty_table(self, mock_get):
        html_content = "<html><body><table></table></body></html>"
        mock_response = MagicMock()
        mock_response.content = html_content.encode('utf-8')
        mock_get.return_value = mock_response

        manager = StationManager()
        df = manager.scrape_gruan_sites()
        self.assertTrue(df.empty)

    @patch('utils.station_manager.get_connection')
    @patch('utils.station_manager.StationManager.scrape_gruan_sites')
    def test_update_station_database(self, mock_scrape, mock_get_conn):
        # Mock scraped data
        mock_scrape.return_value = pd.DataFrame({
            'idstation': ['LIN'],
            'name': ['Lindenberg'],
            'latitude': [52.21],
            'longitude': [14.12],
            'elevation': [98.0],
            'wmoid': [10393]
        })

        # Mock database connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        # Mock existing station check (return None -> insert)
        mock_cursor.fetchone.return_value = None

        manager = StationManager()
        manager.update_station_database()

        # Verify insert was called
        self.assertTrue(mock_cursor.execute.called)
        # Check if insert query was executed
        calls = mock_cursor.execute.call_args_list
        insert_called = any("INSERT INTO station" in str(call) for call in calls)
        self.assertTrue(insert_called)

    @patch('utils.station_manager.get_connection')
    def test_load_stations(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_conn.return_value.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock database return
        mock_cursor.fetchall.return_value = [
            (1, 'LIN', 'GRUAN'),
            (2, 'SOD', 'GRUAN')
        ]

        manager = StationManager()
        manager.load_stations()

        self.assertEqual(manager.get_station_id('LIN'), 1)
        self.assertEqual(manager.get_station_id('SOD'), 2)
        self.assertIsNone(manager.get_station_id('UNKNOWN'))
