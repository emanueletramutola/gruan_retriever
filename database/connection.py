from contextlib import contextmanager

import psycopg2

from config import DB_CONFIG

# Global connection pool instance
_connection_pool = None


def get_connection_pool():
    """
    Get or create the global connection pool instance.

    Returns:
        ConnectionPool: Singleton connection pool instance
    """
    global _connection_pool
    if _connection_pool is None:
        _connection_pool = ConnectionPool()
    return _connection_pool


class ConnectionPool:
    """
    Simple connection pool for managing database connections.
    Reuses connections to improve performance and resource usage.
    """

    def __init__(self, max_connections=40):
        """
        Initialize the connection pool.

        Args:
            max_connections (int): Maximum number of connections to keep in
            the pool
        """
        self.max_connections = max_connections
        self._connections = []  # Pool of available connections

    def get_connection(self):
        """
        Get a connection from the pool.
        Creates new connection if pool is empty.

        Returns:
            psycopg2.connection: Database connection object
        """
        if self._connections:
            # Reuse existing connection from pool
            return self._connections.pop()
        else:
            # Create new connection
            conn = psycopg2.connect(**DB_CONFIG)
            conn.autocommit = False  # Use transactions by default
            return conn

    def return_connection(self, conn):
        """
        Return a connection to the pool for reuse.
        Closes connection if pool is full or connection is invalid.

        Args:
            conn: Database connection to return to the pool
        """
        if len(self._connections) < self.max_connections and not conn.closed:
            # Add connection back to pool if there's space and it's still open
            self._connections.append(conn)
        else:
            # Close connection if pool is full or connection is closed
            try:
                conn.close()
            except Exception:
                # Ignore errors during connection closure
                pass


@contextmanager
def get_connection():
    """
    Context manager for database connections.
    Automatically handles connection retrieval, transaction management,
    and connection return to the pool.

    Yields:
        psycopg2.connection: Database connection for use within context

    Example:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM table")
    """
    pool = get_connection_pool()
    conn = pool.get_connection()
    try:
        # Yield connection to the context block
        yield conn
        # Commit transaction if no exceptions occurred
        if not conn.closed:
            conn.commit()
    except Exception:
        # Rollback transaction on error
        if not conn.closed:
            conn.rollback()
        raise  # Re-raise the exception
    finally:
        # Always return connection to pool
        if not conn.closed:
            pool.return_connection(conn)
