#!/usr/bin/env python
"""
Redis Enterprise Cluster Key Rotation Manager

This module provides a RedisKeyManager class that handles connection to Redis Enterprise Cluster
with support for primary and secondary access keys, and automatic failover between them.
It also provides functionality to update the primary key after rotation.

Usage:
    from redis_key_manager import RedisKeyManager
    
    # Initialize with your Redis Enterprise Cluster credentials
    redis_manager = RedisKeyManager(
        hostname="your-redis-cluster.azure.redis.cache.windows.net",
        primary_key="your-primary-access-key",
        secondary_key="your-secondary-access-key",
        port=10000  # Redis Enterprise Cluster typically uses port 10000
    )
    
    # Use Redis commands as normal
    redis_manager.write_data("test:key", "Hello, Redis!")
    value = redis_manager.read_data("test:key")
    
    # When the primary key is rotated, update it
    redis_manager.update_primary_key("new-primary-key")
"""

import redis
import redis.cluster
import logging
import time
from typing import Optional, Any, Dict, Union

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RedisKeyManager:
    """
    A class to manage Azure Cache for Redis connections with support for key rotation.
    
    This class provides automatic failover to the secondary key if the primary key
    is rotated, and allows updating the primary key after rotation.
    """
    
    def __init__(
        self,
        hostname: str,
        primary_key: str,
        secondary_key: str,
        port: int = 10000,  # Default to port 10000 for Redis Enterprise Cluster
        ssl: bool = True,
        db: int = 0,
        socket_timeout: int = 5,
        socket_connect_timeout: int = 5,
        max_retries: int = 3,
        retry_on_timeout: bool = True,
        cluster_mode: bool = True  # Enable cluster mode by default for Redis Enterprise
    ):
        """
        Initialize Redis connection manager with primary and secondary keys.
        
        Args:
            hostname: Redis hostname (e.g., 'myinstance.redis.cache.windows.net')
            primary_key: Primary access key
            secondary_key: Secondary access key
            port: Redis port (default: 6380 for SSL connections)
            ssl: Whether to use SSL (default: True)
            db: Redis database number (default: 0)
            socket_timeout: Socket timeout in seconds (default: 5)
            socket_connect_timeout: Connection timeout in seconds (default: 5)
            max_retries: Maximum number of retries for operations (default: 3)
            retry_on_timeout: Whether to retry on timeout (default: True)
        """
        self.hostname = hostname
        self.primary_key = primary_key
        self.secondary_key = secondary_key
        self.port = port
        self.ssl = ssl
        self.db = db
        self.socket_timeout = socket_timeout
        self.socket_connect_timeout = socket_connect_timeout
        self.max_retries = max_retries
        self.retry_on_timeout = retry_on_timeout
        self.cluster_mode = cluster_mode
        
        self.client: Optional[Union[redis.Redis, redis.cluster.RedisCluster]] = None
        self.using_primary = True
        
        # Initialize connection with primary key
        self.connect()
    
    def connect(self) -> None:
        """Create a new Redis connection with the current active key."""
        key = self.primary_key if self.using_primary else self.secondary_key
        key_type = "primary" if self.using_primary else "secondary"
        
        try:
            logger.info(f"Connecting to Redis Enterprise Cluster using {key_type} key")
            
            # Close existing connection if any
            if self.client:
                try:
                    self.client.close()
                except Exception as e:
                    logger.warning(f"Error closing existing Redis connection: {e}")
            
            # Connection parameters
            connection_params = {
                "host": self.hostname,
                "port": self.port,
                "password": key,
                "ssl": self.ssl,
                "socket_timeout": self.socket_timeout,
                "socket_connect_timeout": self.socket_connect_timeout,
                "retry_on_timeout": self.retry_on_timeout,
                "decode_responses": True,  # Auto-decode responses to strings
            }
            
            # Only use db parameter for non-cluster mode
            if not self.cluster_mode:
                connection_params["db"] = self.db
            
            # Create connection based on cluster mode
            if self.cluster_mode:
                self.client = redis.cluster.RedisCluster(**connection_params)
            else:
                self.client = redis.Redis(**connection_params)
            
            # Test connection
            self.client.ping()
            logger.info(f"Successfully connected to Redis Enterprise Cluster using {key_type} key")
        except (redis.exceptions.ConnectionError, redis.exceptions.AuthenticationError,
                redis.exceptions.ResponseError) as e:
            logger.error(f"Failed to connect with {key_type} key: {e}")
            if self.using_primary:
                logger.info("Switching to secondary key")
                self.using_primary = False
                self.connect()
            else:
                logger.critical("Both primary and secondary keys failed to connect")
                raise
    
    def execute_with_failover(self, command_func, *args, **kwargs) -> Any:
        """
        Execute a Redis command with automatic failover to secondary key if needed.
        
        Args:
            command_func: The Redis command function to execute
            *args: Arguments to pass to the command function
            **kwargs: Keyword arguments to pass to the command function
            
        Returns:
            The result of the Redis command
        """
        for attempt in range(self.max_retries):
            try:
                return command_func(*args, **kwargs)
            except (redis.exceptions.ConnectionError, redis.exceptions.AuthenticationError,
                   redis.exceptions.ResponseError) as e:
                # Check if it's a CROSSSLOT error (common in cluster mode)
                if isinstance(e, redis.exceptions.ResponseError) and "CROSSSLOT" in str(e):
                    logger.error(f"CROSSSLOT error: {e}. Keys in this operation must hash to the same slot.")
                    raise  # CROSSSLOT errors can't be solved by key rotation, so raise immediately
                
                # Check if it's an authentication error - which indicates key rotation has occurred
                if (isinstance(e, redis.exceptions.AuthenticationError) or 
                    (isinstance(e, redis.exceptions.ResponseError) and 
                     ("NOAUTH" in str(e) or "invalid password" in str(e).lower()))):
                    logger.warning(f"Authentication error detected: {e}")
                    
                    # If using primary key, switch to secondary immediately
                    if self.using_primary:
                        logger.info("Primary key appears to have been rotated. Switching to secondary key")
                        self.using_primary = False
                        self.connect()
                        
                        # Retry the command immediately with the new connection
                        try:
                            return command_func(*args, **kwargs)
                        except Exception as retry_e:
                            logger.warning(f"Retry after key switch failed: {retry_e}")
                            # Continue to normal retry logic
                    
                # For other connection errors or if secondary key also failed
                logger.warning(f"Connection error on attempt {attempt+1}: {e}")
                if attempt < self.max_retries - 1:
                    # Apply exponential backoff for retry
                    retry_delay = 0.5 * (2 ** attempt)  # Exponential backoff
                    logger.info(f"Retrying in {retry_delay:.2f} seconds... (attempt {attempt+1}/{self.max_retries})")
                    time.sleep(retry_delay)
                    
                    # Reconnect before retrying
                    self.connect()
            except redis.exceptions.TimeoutError as e:
                logger.warning(f"Timeout error on attempt {attempt+1}: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))  # Exponential backoff
                else:
                    raise
        
        # If we've exhausted all retries
        raise redis.exceptions.ConnectionError("Failed to execute Redis command after multiple retries")
    
    def write_data_continuously(self, key_prefix: str, start_id: int = 0, 
                            count: int = 100, interval: float = 0.5,
                            callback=None):
        """
        Continuously write data to Redis, handling key rotation automatically.
        
        This method will write data in a loop, and will automatically switch to the
        secondary key if the primary key is rotated in the Azure portal.
        
        Args:
            key_prefix: Prefix for Redis keys
            start_id: Starting ID for the data
            count: Number of writes to perform
            interval: Time between writes in seconds
            callback: Optional callback function(id, success, using_primary) called after each write
            
        Returns:
            dict: Statistics about the operation (total, successful, failed, using_primary)
        """
        stats = {
            "total": 0,
            "successful": 0,
            "failed": 0,
            "key_switches": 0,
            "final_key": "primary" if self.using_primary else "secondary"
        }
        
        logger.info(f"Starting continuous write operation: {count} items, interval {interval}s")
        
        for i in range(start_id, start_id + count):
            key = f"{key_prefix}:{i}"
            value = f"data-{i}-{time.time()}"
            
            # Track if we were using primary key before this operation
            was_using_primary = self.using_primary
            
            try:
                success = self.write_data(key, value)
                stats["successful"] += 1
                logger.info(f"Write {i} succeeded using {'primary' if self.using_primary else 'secondary'} key")
                
                # If we switched keys during this operation
                if was_using_primary != self.using_primary:
                    stats["key_switches"] += 1
                    logger.warning(f"Key switch detected during operation {i}: "
                                 f"primary → secondary")
            except Exception as e:
                stats["failed"] += 1
                logger.error(f"Write {i} failed: {e}")
            
            stats["total"] += 1
            
            # Call the callback if provided
            if callback:
                callback(i, stats["successful"] > stats["failed"], self.using_primary)
            
            # Sleep before next write unless this is the last iteration
            if i < start_id + count - 1:
                time.sleep(interval)
        
        stats["final_key"] = "primary" if self.using_primary else "secondary"
        logger.info(f"Continuous write operation completed: {stats}")
        return stats
    
    def write_data(self, key: str, value: str, expire: Optional[int] = None) -> bool:
        """
        Write data to Redis with automatic failover.
        
        Args:
            key: Redis key
            value: Value to store
            expire: Optional expiration time in seconds
            
        Returns:
            bool: Success status
        """
        if not self.client:
            self.connect()
            
        def _write():
            result = self.client.set(key, value)
            if expire is not None:
                self.client.expire(key, expire)
            return result
            
        return self.execute_with_failover(_write)
    
    def read_data(self, key: str) -> Optional[str]:
        """
        Read data from Redis with automatic failover.
        
        Args:
            key: Redis key
            
        Returns:
            Value or None if not found/error
        """
        if not self.client:
            self.connect()
            
        return self.execute_with_failover(self.client.get, key)
    
    def delete_data(self, key: str) -> bool:
        """
        Delete data from Redis with automatic failover.
        
        Args:
            key: Redis key
            
        Returns:
            bool: Success status (True if key was deleted)
        """
        if not self.client:
            self.connect()
            
        return bool(self.execute_with_failover(self.client.delete, key))
    
    def update_primary_key(self, new_primary_key: str) -> None:
        """
        Update the primary key (after rotation).
        
        This method updates the primary key and attempts to reconnect with it
        if currently using the secondary key.
        
        Args:
            new_primary_key: New primary access key
        """
        old_primary = self.primary_key
        self.primary_key = new_primary_key
        logger.info("Primary key has been updated")
        
        # If we're currently using the secondary key, try switching back to primary
        if not self.using_primary:
            logger.info("Attempting to switch back to primary key")
            temp_client = None
            try:
                # Test the new primary key
                if self.cluster_mode:
                    temp_client = redis.cluster.RedisCluster(
                        host=self.hostname,
                        port=self.port,
                        password=new_primary_key,
                        ssl=self.ssl,
                        socket_timeout=self.socket_timeout,
                        socket_connect_timeout=self.socket_connect_timeout
                    )
                else:
                    temp_client = redis.Redis(
                        host=self.hostname,
                        port=self.port,
                        password=new_primary_key,
                        db=self.db,
                        ssl=self.ssl,
                        socket_timeout=self.socket_timeout,
                        socket_connect_timeout=self.socket_connect_timeout
                    )
                temp_client.ping()
                
                # If successful, switch back to primary
                self.using_primary = True
                self.connect()
                logger.info("Successfully switched back to primary key")
            except (redis.exceptions.ConnectionError, redis.exceptions.AuthenticationError, 
                    redis.exceptions.ResponseError):
                logger.warning("New primary key validation failed, staying on secondary")
            finally:
                if temp_client:
                    temp_client.close()


    def close(self) -> None:
        """Close the Redis connection."""
        if self.client:
            self.client.close()
            self.client = None
            logger.info("Redis connection closed")
    
    try:
        # Example of writing data
        redis_manager.write_data("test:key1", "Hello, Redis!")
        
        # Read the data back
        value = redis_manager.read_data("test:key1")
        print(f"Retrieved value: {value}")
        
        # Simulate a key rotation scenario
        print("\nSimulating primary key rotation...")
        
        # This would fail with the original primary key
        redis_manager.primary_key = "intentionally-invalid-key"
        redis_manager.using_primary = True
        
        # The write operation should automatically fail over to the secondary key
        success = redis_manager.write_data("test:key2", "Using secondary key now!")
        print(f"Write with failover successful: {success}")
        
        # Later, when you've rotated the primary key in Azure Portal
        NEW_PRIMARY_KEY = "your-new-primary-key-after-rotation"
        redis_manager.update_primary_key(NEW_PRIMARY_KEY)
        
    finally:
        # Clean up
        redis_manager.close()
