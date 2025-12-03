import logging
import re

import pandas as pd
import requests
from bs4 import BeautifulSoup

from database.connection import get_connection

logger = logging.getLogger(__name__)


class StationManager:
    """
    Comprehensive station management system that handles both database updates
    and in-memory caching for efficient station lookups.
    """

    _instance = None
    _cache = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(StationManager, cls).__new__(cls)
            cls._cache = {}
        return cls._instance

    def scrape_gruan_sites(self):
        """
        Extract GRUAN station data from the official website.

        Returns:
            pandas.DataFrame: DataFrame with station information
        """
        url = "https://www.gruan.org/network/sites"

        # Make HTTP request
        response = requests.get(url)
        response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.content, 'html.parser')

        # Find the table
        table = soup.find('table')

        if not table:
            raise ValueError("Table not found in the page")

        # Extract data
        data = {
            'idstation': [],
            'name': [],
            'latitude': [],
            'longitude': [],
            'elevation': [],
            'wmoid': []
        }

        # Find all table rows (excluding header)
        rows = table.find_all('tr')[1:]  # Skip header

        for row in rows:
            cells = row.find_all('td')

            if len(cells) >= 6:  # Make sure there are enough columns
                # idstation (Code)
                idstation = cells[0].get_text(strip=True)
                if not idstation:  # Skip if idstation is empty
                    continue

                # Name - take only the part before the comma
                name_full = cells[1].get_text(strip=True)
                name = name_full.split(',')[
                    0] if ',' in name_full else name_full

                # Latitude - remove the ° character
                latitude_text = (cells[2].get_text(strip=True)
                                 .replace('°', ''))
                try:
                    latitude = float(latitude_text) if latitude_text else None
                except ValueError:
                    latitude = None

                # Longitude - remove the ° character
                longitude_text = (cells[3].get_text(strip=True)
                                  .replace('°', ''))
                try:
                    longitude = float(
                        longitude_text) if longitude_text else None
                except ValueError:
                    longitude = None

                # elevation (Altitude) - remove 'm' and extract the largest
                # number
                elevation_text = (cells[4].get_text(strip=True)
                                  .replace('m', ''))
                elevation = None
                if elevation_text:
                    # Extract all numbers from the string
                    numbers = re.findall(r'\d+\.?\d*', elevation_text)
                    if numbers:
                        try:
                            # Convert to float and take the maximum
                            elevation = max([float(num) for num in numbers])
                        except ValueError:
                            elevation = None

                # wmoid (WMO/WIGOS No.) - convert to integer only if it's a
                # pure number
                wmo_text = cells[5].get_text(strip=True)
                wmoid = None
                if wmo_text:
                    # Try to convert directly to integer (no extraction,
                    # strict check)
                    try:
                        wmoid = int(wmo_text)
                    except ValueError:
                        # If it contains non-numeric characters, set to None
                        wmoid = None

                data['idstation'].append(idstation)
                data['name'].append(name)
                data['latitude'].append(latitude)
                data['longitude'].append(longitude)
                data['elevation'].append(elevation)
                data['wmoid'].append(wmoid)

        # Create DataFrame
        df = pd.DataFrame(data)

        # Remove rows where idstation is None or empty
        df = df[df['idstation'].notna() & (df['idstation'] != '')]

        # Convert wmoid to Int64 (pandas nullable integer type) but replace
        # pd.NA with None
        df['wmoid'] = df['wmoid'].astype('Int64')

        return df

    def _convert_na_to_none(self, value):
        """
        Convert pandas NA values to None for database compatibility.

        Args:
            value: Any value that might be pd.NA

        Returns:
            value converted to None if it's pd.NA, otherwise the original value
        """
        if pd.isna(value):
            return None
        return value

    def update_station_database(self):
        """
        Update the station table in the database with the latest GRUAN site
        information.
        Also refreshes the in-memory cache after updating.
        """
        try:
            # Scrape the latest station data
            logger.info("Scraping GRUAN station data from web...")
            df = self.scrape_gruan_sites()
            logger.info(f"Found {len(df)} stations")

            # Connect to database
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    # For each station in the scraped data, insert or update
                    for _, row in df.iterrows():
                        # Convert pandas NA values to None for database
                        # compatibility
                        name = self._convert_na_to_none(row['name'])
                        latitude = self._convert_na_to_none(row['latitude'])
                        longitude = self._convert_na_to_none(row['longitude'])
                        elevation = self._convert_na_to_none(row['elevation'])
                        wmoid = self._convert_na_to_none(row['wmoid'])
                        idstation = self._convert_na_to_none(row['idstation'])

                        # First, check if the station already exists
                        cursor.execute(
                            "SELECT id FROM station WHERE idstation = %s",
                            (idstation,)
                        )

                        existing_station = cursor.fetchone()

                        if existing_station:
                            # Update existing station
                            cursor.execute(
                                "UPDATE station SET name = %s, "
                                "latitude = %s,longitude = %s,elevation = %s,"
                                "wmoid = %s,network= 'GRUAN' "
                                "WHERE idstation = %s",
                                (name, latitude, longitude, elevation, wmoid,
                                 idstation))
                            logger.debug(f"Updated station: {idstation}")
                        else:
                            # Insert new station
                            cursor.execute(
                                "INSERT INTO station (idstation, name, "
                                "latitude, longitude, elevation, wmoid, "
                                "network) VALUES (%s, %s, %s, %s, %s, %s, "
                                "'GRUAN')",
                                (idstation, name, latitude, longitude,
                                 elevation, wmoid))
                            logger.debug(f"Inserted new station: {idstation}")

            logger.info("Station database updated successfully")

            # Refresh the cache after updating the database
            self.refresh_cache()

        except Exception as e:
            logger.error(f"Error updating station database: {e}")
            raise

    def load_stations(self):
        """
        Load all stations from database into memory cache.
        """
        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id, idstation, network "
                                   "FROM station")
                    stations = cursor.fetchall()

                    self._cache = {}
                    for station_id, idstation, network in stations:
                        key = (idstation, network)
                        self._cache[key] = station_id

                    logger.info(f"Loaded {len(stations)} stations into cache")

        except Exception as e:
            logger.error(f"Error loading station cache: {e}")
            # If we can't load stations, we should raise an exception
            # because we cannot process files without station information
            raise

    def get_station_id(self, idstation, network='GRUAN'):
        """
        Get station ID from cache based on idstation and network.

        Args:
            idstation (str): Station identifier
            network (str): Network name, defaults to 'GRUAN'

        Returns:
            int or None: Station ID if found, None otherwise
        """
        key = (idstation, network)
        return self._cache.get(key)

    def refresh_cache(self):
        """
        Refresh the station cache by reloading from database.
        """
        self.load_stations()

    def get_cache_size(self):
        """
        Get the current size of the station cache.

        Returns:
            int: Number of stations in cache
        """
        return len(self._cache)

    def is_station_in_cache(self, idstation, network='GRUAN'):
        """
        Check if a station exists in the cache.

        Args:
            idstation (str): Station identifier
            network (str): Network name

        Returns:
            bool: True if station exists in cache
        """
        key = (idstation, network)
        return key in self._cache


def get_station_manager():
    """
    Get the singleton instance of StationManager.

    Returns:
        StationManager: Singleton station manager instance
    """
    return StationManager()


def update_station_database():
    """
    Update the station database from GRUAN website.
    """
    manager = get_station_manager()
    manager.update_station_database()


def load_station_cache():
    """
    Load station cache from database.
    Convenience function for backward compatibility.
    """
    manager = get_station_manager()
    manager.load_stations()
