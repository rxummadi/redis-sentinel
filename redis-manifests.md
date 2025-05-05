# Redis Sentinel Kubernetes Manifests

This set of Kubernetes manifests creates a production-ready Redis Sentinel cluster. The deployment consists of:

1. ConfigMaps for Redis and Sentinel configurations
2. Secret for Redis authentication
3. StatefulSet for Redis instances (1 master, 2 replicas)
4. StatefulSet for Sentinel instances (3 pods)
5. Services for Redis and Sentinel communication
6. PodDisruptionBudget for availability during maintenance

## 1. Namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: redis
```

## 2. Redis Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: redis-secret
  namespace: redis
type: Opaque
data:
  # Value is 'YourStrongRedisPassword' encoded in base64
  redis-password: WW91clN0cm9uZ1JlZGlzUGFzc3dvcmQ=
```

## 3. Redis ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: redis-config
  namespace: redis
data:
  redis.conf: |
    # Basic settings
    port 6379
    bind 0.0.0.0
    protected-mode no
    dir /data
    
    # Persistence
    appendonly yes
    appendfsync everysec
    auto-aof-rewrite-percentage 100
    auto-aof-rewrite-min-size 64mb
    rdb-save-incremental-fsync yes
    save 900 1
    save 300 10
    save 60 10000
    
    # Memory management
    maxmemory 4gb
    maxmemory-policy volatile-lru
    activedefrag yes
    active-defrag-threshold-lower 10
    active-defrag-threshold-upper 100
    active-defrag-cycle-min 1
    active-defrag-cycle-max 25
    active-defrag-ignore-bytes 100mb
    
    # Security - password will be added via command arguments
    
    # Replication - master/replica role will be configured via command arguments
    replica-read-only yes
    repl-diskless-sync yes
    repl-diskless-sync-delay 5
    repl-backlog-size 100mb
    repl-backlog-ttl 3600
    
    # Performance tuning
    tcp-keepalive 60
    timeout 0
    tcp-backlog 511
    io-threads 4
    no-appendfsync-on-rewrite yes
    
    # Latency monitoring
    latency-monitor-threshold 100
    slowlog-log-slower-than 10000
    slowlog-max-len 1000
    
    # Client handling
    client-output-buffer-limit normal 0 0 0
    client-output-buffer-limit replica 512mb 128mb 60
    client-output-buffer-limit pubsub 32mb 8mb 60
    client-query-buffer-limit 32mb
    
    # Memory optimization
    lazyfree-lazy-eviction yes
    lazyfree-lazy-expire yes
    lazyfree-lazy-server-del yes
    replica-lazy-flush yes
```

## 4. Sentinel ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: sentinel-config
  namespace: redis
data:
  sentinel.conf: |
    port 26379
    bind 0.0.0.0
    dir /data
    sentinel deny-scripts-reconfig yes
    
    # Monitoring
    sentinel monitor mymaster redis-0.redis-headless.redis.svc.cluster.local 6379 2
    sentinel down-after-milliseconds mymaster 5000
    sentinel failover-timeout mymaster 60000
    sentinel parallel-syncs mymaster 1
    
    # These will be added by init script
    # sentinel auth-pass mymaster password
    
    # Advanced settings
    sentinel client-reconfig-script mymaster /data/scripts/update-config.sh
    sentinel resolve-hostnames yes
    sentinel announce-hostnames yes
  
  init-sentinel.sh: |
    #!/bin/sh
    REDIS_PASSWORD=$(cat /etc/redis-password/redis-password)
    echo "sentinel auth-pass mymaster $REDIS_PASSWORD" >> /etc/redis/sentinel.conf
    exec "$@"
```

## 5. Redis Scripts ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: redis-scripts
  namespace: redis
data:
  setup-master.sh: |
    #!/bin/sh
    REDIS_PASSWORD=$(cat /etc/redis-password/redis-password)
    
    # Check if this is redis-0 (the initial master)
    if [ "$(hostname)" == "redis-0" ]; then
      echo "This is redis-0, configuring as master"
      exec redis-server /etc/redis/redis.conf \
        --requirepass "$REDIS_PASSWORD" \
        --masterauth "$REDIS_PASSWORD"
    else
      echo "This is a replica, configuring to replicate redis-0"
      exec redis-server /etc/redis/redis.conf \
        --requirepass "$REDIS_PASSWORD" \
        --masterauth "$REDIS_PASSWORD" \
        --replicaof redis-0.redis-headless.redis.svc.cluster.local 6379
    fi
  
  update-config.sh: |
    #!/bin/sh
    # This script would be called by Sentinel when a failover happens
    # Just a placeholder - in production, this would update some configuration or notify external systems
    echo "Failover event detected: $@" >> /data/failover.log
```

## 6. Redis Headless Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: redis-headless
  namespace: redis
  labels:
    app: redis
spec:
  clusterIP: None  # Headless service
  ports:
  - port: 6379
    targetPort: redis
    name: redis
  selector:
    app: redis
```

## 7. Redis Client Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: redis
  namespace: redis
  labels:
    app: redis
spec:
  ports:
  - port: 6379
    targetPort: redis
    name: redis
  selector:
    app: redis
```

## 8. Redis StatefulSet

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis
  namespace: redis
spec:
  serviceName: redis-headless
  replicas: 3  # 1 master and 2 replicas
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      securityContext:
        fsGroup: 1000
        runAsUser: 1000
        runAsNonRoot: true
      initContainers:
      - name: system-init
        image: busybox:1.36
        command: ['sh', '-c', 'echo never > /host-sys/kernel/mm/transparent_hugepage/enabled || true']
        securityContext:
          privileged: true
        volumeMounts:
        - name: host-sys
          mountPath: /host-sys
      containers:
      - name: redis
        image: redis:7.2-alpine
        command: ["/scripts/setup-master.sh"]
        securityContext:
          allowPrivilegeEscalation: false
        ports:
        - name: redis
          containerPort: 6379
        volumeMounts:
        - name: redis-data
          mountPath: /data
        - name: redis-config
          mountPath: /etc/redis
        - name: redis-password
          mountPath: /etc/redis-password
          readOnly: true
        - name: redis-scripts
          mountPath: /scripts
          readOnly: true
        resources:
          requests:
            cpu: 200m
            memory: 1Gi
          limits:
            cpu: 1000m
            memory: 5Gi
        livenessProbe:
          exec:
            command:
            - sh
            - -c
            - redis-cli -a $(cat /etc/redis-password/redis-password) ping
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          successThreshold: 1
          failureThreshold: 3
        readinessProbe:
          exec:
            command:
            - sh
            - -c
            - redis-cli -a $(cat /etc/redis-password/redis-password) ping
          initialDelaySeconds: 5
          periodSeconds: 5
          timeoutSeconds: 2
          successThreshold: 1
          failureThreshold: 3
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchExpressions:
              - key: app
                operator: In
                values:
                - redis
            topologyKey: "kubernetes.io/hostname"
      volumes:
      - name: redis-config
        configMap:
          name: redis-config
      - name: redis-password
        secret:
          secretName: redis-secret
      - name: redis-scripts
        configMap:
          name: redis-scripts
          defaultMode: 0755
      - name: host-sys
        hostPath:
          path: /sys
  volumeClaimTemplates:
  - metadata:
      name: redis-data
    spec:
      accessModes: [ "ReadWriteOnce" ]
      storageClassName: "standard"  # Use your storage class
      resources:
        requests:
          storage: 10Gi
```

## 9. Sentinel Headless Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: sentinel-headless
  namespace: redis
  labels:
    app: redis-sentinel
spec:
  clusterIP: None  # Headless service
  ports:
  - port: 26379
    targetPort: sentinel
    name: sentinel
  selector:
    app: redis-sentinel
```

## 10. Sentinel Client Service

```yaml
apiVersion: v1
kind: Service
metadata:
  name: redis-sentinel
  namespace: redis
  labels:
    app: redis-sentinel
spec:
  ports:
  - port: 26379
    targetPort: sentinel
    name: sentinel
  selector:
    app: redis-sentinel
```

## 11. Sentinel StatefulSet

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis-sentinel
  namespace: redis
spec:
  serviceName: sentinel-headless
  replicas: 3  # Three Sentinel instances for reliable quorum
  selector:
    matchLabels:
      app: redis-sentinel
  template:
    metadata:
      labels:
        app: redis-sentinel
    spec:
      securityContext:
        fsGroup: 1000
        runAsUser: 1000
        runAsNonRoot: true
      initContainers:
      - name: init-sentinel
        image: redis:7.2-alpine
        command: ["/bin/sh", "/etc/redis/init-sentinel.sh", "redis-sentinel", "/etc/redis/sentinel.conf"]
        volumeMounts:
        - name: sentinel-config
          mountPath: /etc/redis
        - name: redis-password
          mountPath: /etc/redis-password
          readOnly: true
        - name: sentinel-data
          mountPath: /data
      containers:
      - name: sentinel
        image: redis:7.2-alpine
        command: ["redis-sentinel", "/etc/redis/sentinel.conf"]
        securityContext:
          allowPrivilegeEscalation: false
        ports:
        - name: sentinel
          containerPort: 26379
        volumeMounts:
        - name: sentinel-data
          mountPath: /data
        - name: sentinel-config
          mountPath: /etc/redis
        - name: redis-scripts
          mountPath: /data/scripts
          readOnly: true
        resources:
          requests:
            cpu: 100m
            memory: 256Mi
          limits:
            cpu: 500m
            memory: 512Mi
        livenessProbe:
          exec:
            command:
            - sh
            - -c
            - redis-cli -p 26379 ping
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          successThreshold: 1
          failureThreshold: 3
        readinessProbe:
          exec:
            command:
            - sh
            - -c
            - redis-cli -p 26379 ping
          initialDelaySeconds: 5
          periodSeconds: 5
          timeoutSeconds: 2
          successThreshold: 1
          failureThreshold: 3
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchExpressions:
              - key: app
                operator: In
                values:
                - redis-sentinel
            topologyKey: "kubernetes.io/hostname"
      volumes:
      - name: sentinel-config
        configMap:
          name: sentinel-config
          defaultMode: 0755
      - name: redis-password
        secret:
          secretName: redis-secret
      - name: redis-scripts
        configMap:
          name: redis-scripts
          defaultMode: 0755
  volumeClaimTemplates:
  - metadata:
      name: sentinel-data
    spec:
      accessModes: [ "ReadWriteOnce" ]
      storageClassName: "standard"  # Use your storage class
      resources:
        requests:
          storage: 1Gi
```

## 12. Pod Disruption Budget for Redis

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: redis-pdb
  namespace: redis
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: redis
```

## 13. Pod Disruption Budget for Sentinel

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: sentinel-pdb
  namespace: redis
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: redis-sentinel
```

## Applying the Configuration

Save each YAML section into separate files or combine them into a single file with `---` separators between sections. Apply the configuration using kubectl:

```bash
kubectl apply -f redis-sentinel-manifests.yaml
```

After deployment, Redis-0 will initially be the master, and Redis-1 and Redis-2 will be replicas. The Sentinel pods will monitor the Redis instances and handle failover if needed.
