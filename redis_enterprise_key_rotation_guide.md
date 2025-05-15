# Redis Enterprise Cluster Access Key Rotation Guide

## Introduction

This document provides guidance on implementing access key rotation for Redis Enterprise Cluster in production environments. It includes a Python implementation that enables seamless key rotation without service disruption and explains best practices for maintaining high availability during the key rotation process.

## Why Key Rotation Matters

Access key rotation is a security best practice that helps:

- Minimize the risk window if credentials are compromised
- Comply with security policies and compliance requirements
- Maintain separation of duties in operational environments
- Enable secure credential management in CI/CD pipelines

## Key Rotation Challenges

When rotating access keys for Redis Enterprise Cluster, several challenges must be addressed:

1. **Service Continuity**: Applications must continue to function during key rotation
2. **Distributed Systems**: Multiple applications may use the same Redis instance
3. **Connection Pooling**: Most applications use connection pools that must be updated
4. **Error Handling**: Robust error handling is required to prevent service degradation
5. **Cluster Specifics**: Redis Enterprise Cluster has unique considerations compared to standalone Redis

## Solution: Redis Enterprise Key Rotation Manager

The solution provided here implements a Python class called `RedisKeyManager` that:

1. Manages both primary and secondary access keys
2. Automatically switches to the secondary key if the primary key fails
3. Provides a method to update the primary key after rotation
4. Implements proper error handling and retry logic
5. Supports the specific requirements of Redis Enterprise Cluster

### Implementation Details

The `RedisKeyManager` class provides:

- **Automatic Failover**: Detects authentication failures and switches to the secondary key
- **Transparent Recovery**: Continues operations without requiring application restart
- **Proper Error Handling**: Manages cluster-specific errors and connection failures
- **Connection Pooling**: Manages Redis connections efficiently
- **Exponential Backoff**: Implements best practices for retry mechanisms

## How Key Rotation Works

The key rotation process with this solution follows these steps:

1. **Normal Operation**: The application uses the primary key to connect to Redis
2. **Key Rotation Start**: The administrator generates a new key in the Azure portal
3. **Automatic Detection**: When the primary key is invalidated, the application automatically switches to the secondary key
4. **Update Primary Key**: The administrator updates the application with the new primary key
5. **Resume Normal Operation**: The application switches back to using the primary key

## Code Implementation

```python
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
    A class to manage Redis Enterprise Cluster connections with support for key rotation.
    
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
            hostname: Redis hostname (e.g., 'mycluster.redis.cache.windows.net')
            primary_key: Primary access key
            secondary_key: Secondary access key
            port: Redis port (default: 10000 for Redis Enterprise)
            ssl: Whether to use SSL (default: True)
            db: Redis database number (default: 0)
            socket_timeout: Socket timeout in seconds (default: 5)
            socket_connect_timeout: Connection timeout in seconds (default: 5)
            max_retries: Maximum number of retries for operations (default: 3)
            retry_on_timeout: Whether to retry on timeout (default: True)
            cluster_mode: Whether to use Redis Cluster client (default: True)
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
                
                logger.warning(f"Connection/Authentication error on attempt {attempt+1}: {e}")
                
                # On first attempt, if using primary key, try switching to secondary
                if attempt == 0 and self.using_primary:
                    logger.info("Primary key may have been rotated. Switching to secondary key")
                    self.using_primary = False
                    self.connect()
                # On subsequent attempts or if already using secondary, try reconnecting
                else:
                    logger.info(f"Retrying connection... (attempt {attempt+1}/{self.max_retries})")
                    time.sleep(0.5 * (2 ** attempt))  # Exponential backoff
                    self.connect()
            except redis.exceptions.TimeoutError as e:
                logger.warning(f"Timeout error on attempt {attempt+1}: {e}")
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (2 ** attempt))  # Exponential backoff
                else:
                    raise
        
        # If we've exhausted all retries
        raise redis.exceptions.ConnectionError("Failed to execute Redis command after multiple retries")
    
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
```

## Implementation Guide

### Prerequisites

1. **Python Environment**: Python 3.6+ with the following packages:
   - `redis` (install with `pip install redis`)

2. **Redis Enterprise Cluster Credentials**:
   - Hostname (e.g., `mycluster.redis.cache.windows.net`)
   - Primary access key
   - Secondary access key

### Step 1: Install Dependencies

```bash
pip install redis
```

### Step 2: Import the Key Manager

Create a file named `redis_key_manager.py` with the code provided above, then import it in your application:

```python
from redis_key_manager import RedisKeyManager
```

### Step 3: Initialize the Key Manager

```python
redis_manager = RedisKeyManager(
    hostname="your-redis-cluster.redis.cache.windows.net",
    primary_key="your-primary-access-key",
    secondary_key="your-secondary-access-key",
    port=10000,
    cluster_mode=True
)
```

### Step 4: Use Redis Operations

```python
# Write data
redis_manager.write_data("customer:1001", "John Doe", expire=3600)  # Expires in 1 hour

# Read data
customer_data = redis_manager.read_data("customer:1001")

# Delete data
redis_manager.delete_data("customer:1001")
```

### Step 5: Handle Key Rotation

When rotating keys in the Azure Portal:

1. Generate a new key (this becomes the new secondary key)
2. Update your application with the new key:

```python
redis_manager.update_primary_key("new-primary-key")
```

3. Now, both your application and Azure are using the same keys

## Best Practices for Key Rotation

1. **Automated Rotation**: Implement automated key rotation on a regular schedule
2. **Gradual Rollout**: For large systems, roll out key changes gradually across instances
3. **Monitoring**: Implement monitoring to detect failed authentications
4. **Secret Management**: Use a secure vault service (Azure Key Vault, HashiCorp Vault) to store keys
5. **Centralized Configuration**: Use a central configuration service to update keys across services
6. **Testing**: Test key rotation procedures in non-production environments regularly

## Redis Enterprise Cluster Specific Considerations

1. **Cluster Slots**: In clustered mode, keys that operate together must hash to the same slot
2. **Connection Pooling**: Configure appropriate pool sizes for your workload
3. **Timeouts**: Adjust timeouts based on your application's sensitivity to latency
4. **SSL/TLS**: Always use SSL/TLS for encrypted communications

## Troubleshooting

### Common Issues and Resolutions

1. **Connection Refused Errors**:
   - Check network connectivity
   - Verify firewall rules allow access to port 10000

2. **Authentication Failures**:
   - Verify keys are correct and not expired
   - Check if Azure active directory authentication is enabled

3. **CROSSSLOT Errors**:
   - Ensure keys used in multi-key operations hash to the same slot
   - Consider using hash tags to force keys into the same slot

4. **Timeouts**:
   - Increase socket_timeout and socket_connect_timeout values
   - Check Redis Enterprise Cluster load and performance

## Alternative Authentication Methods

While this guide focuses on access key rotation, consider these alternatives:

1. **Microsoft Entra ID Authentication**: Azure Cache for Redis supports Microsoft Entra ID (formerly Azure Active Directory) for password-free authentication, which offers superior security
   
2. **Managed Identities**: For applications hosted in Azure, consider using managed identities

## Conclusion

Implementing proper access key rotation for Redis Enterprise Cluster is essential for maintaining security in production environments. The solution provided in this document enables seamless key rotation without service disruption and can be integrated into existing applications with minimal effort.

By following the best practices outlined in this document, organizations can enhance their security posture while maintaining high availability for their Redis Enterprise Cluster deployments.
