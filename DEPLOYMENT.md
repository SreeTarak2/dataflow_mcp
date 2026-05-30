# Production Deployment Guide

## Overview
This document provides step-by-step instructions for deploying the DataFlow MCP Server to production environments.

## Pre-Deployment Checklist

- [ ] MongoDB instance provisioned with authentication enabled
- [ ] SSL/TLS certificates obtained
- [ ] Environment configuration files prepared
- [ ] Logging destination configured
- [ ] Monitoring/alerting system ready
- [ ] Backup strategy implemented
- [ ] Load testing completed
- [ ] Security audit passed

## System Requirements

- Python 3.12 or higher
- MongoDB 4.4+ with authentication enabled
- 2GB RAM minimum
- 10GB disk space minimum

## Traditional Linux Deployment

### 1. System Preparation
```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python & dependencies
sudo apt install -y python3.12 python3-pip python3-venv curl git

# Create app directory
sudo mkdir -p /opt/dataflow-mcp
sudo chown $USER:$USER /opt/dataflow-mcp
cd /opt/dataflow-mcp
```

### 2. Clone & Setup Application
```bash
# Clone repository (or copy files)
git clone <repo-url> .

# Create virtual environment
python3.12 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e .
```

### 3. Configure Environment
```bash
# Copy example config
cp .env.example .env

# Edit with your MongoDB connection
nano .env
```

Example `.env`:
```env
MONGO_URI=mongodb://username:password@mongodb-host:27017/dataflow
MONGO_DB_NAME=dataflow
MONGO_TIMEOUT=5000
MONGO_POOL_SIZE=10
MONGO_USE_TLS=true
LOGS_DIR=/var/log/dataflow-mcp
LOG_LEVEL=INFO
```

### 4. Create Systemd Service
```bash
# Create service file
sudo nano /etc/systemd/system/dataflow-mcp.service
```

```ini
[Unit]
Description=DataFlow MCP Server
After=network.target
Wants=mongodb.service

[Service]
Type=simple
User=dataflow
WorkingDirectory=/opt/dataflow-mcp
Environment="PATH=/opt/dataflow-mcp/venv/bin"
ExecStart=/opt/dataflow-mcp/venv/bin/python /opt/dataflow-mcp/main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 5. Enable and Start Service
```bash
# Create dedicated user
sudo useradd -r -s /bin/bash dataflow || true

# Set permissions
sudo chown -R dataflow:dataflow /opt/dataflow-mcp
sudo mkdir -p /var/log/dataflow-mcp
sudo chown dataflow:dataflow /var/log/dataflow-mcp

# Create logs directory
mkdir -p /opt/dataflow-mcp/logs
sudo chown dataflow:dataflow /opt/dataflow-mcp/logs

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable dataflow-mcp
sudo systemctl start dataflow-mcp

# Check status
sudo systemctl status dataflow-mcp
sudo journalctl -u dataflow-mcp -f
```

## SSL/TLS Configuration

### 1. Obtain Certificates (Let's Encrypt)
```bash
# Using certbot
sudo apt install certbot python3-certbot-nginx -y
sudo certbot certonly --standalone -d yourdomain.com
```

### 2. Configure Nginx Reverse Proxy
```nginx
# /etc/nginx/sites-available/dataflow-mcp
upstream dataflow_mcp {
    server localhost:5000;
}

server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://dataflow_mcp;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$server_name$request_uri;
}
```

```bash
sudo systemctl enable nginx
sudo systemctl start nginx
```

## Monitoring & Logging

### 1. View Application Logs
```bash
# Real-time logs
sudo journalctl -u dataflow-mcp -f

# Last 100 lines
sudo journalctl -u dataflow-mcp -n 100

# Logs from specific time
sudo journalctl -u dataflow-mcp --since "2024-01-01 12:00:00"
```

### 2. Log Rotation
Logs are automatically rotated daily in `/var/log/dataflow-mcp/` with:
- Daily rotation
- Maximum 10 backup files
- 10MB maximum file size

### 3. MongoDB Monitoring
```bash
# Check MongoDB connection
mongo "mongodb://username:password@mongodb-host:27017/dataflow"

# View active operations
db.currentOp()

# Check slow queries
db.system.profile.find().sort({ts: -1}).limit(10).pretty()

# Index statistics
db.collection.aggregate([{$indexStats: {}}])
```

## Backup Strategy

### MongoDB Backup
```bash
# Manual backup
mongodump --uri="mongodb://user:pass@host:27017/dataflow" --out=/backups/mongo_$(date +%Y%m%d_%H%M%S)

# Scheduled daily backup
0 2 * * * mongodump --uri="mongodb://user:pass@host:27017/dataflow" --out=/backups/mongo_$(date +\%Y\%m\%d) >> /var/log/mongo-backup.log 2>&1

# Keep 30 days of backups
find /backups -name "mongo_*" -mtime +30 -exec rm -rf {} \;
```

### Automated Snapshots
- MongoDB Atlas: Enable automatic backups
- Self-managed: Use mongodump with cron jobs
- Cloud platforms: Use native snapshot features

## Performance Tuning

### Application Configuration
```env
# Increase timeout for large datasets
MONGO_TIMEOUT=10000

# Adjust connection pool based on load
MONGO_POOL_SIZE=20

# Reduce max idle time for high-traffic
MONGO_MAX_IDLE_TIME=30000
```

### Database Optimization
```bash
# Check indexes
mongosh "mongodb://user:pass@host:27017/dataflow"
db.collection.getIndexes()

# Rebuild indexes if needed
db.collection.reIndex()

# Analyze query plans
db.collection.find({query}).explain("executionStats")
```

## Security Hardening

### Network Security
- Restrict MongoDB access to app servers only
- Use firewall rules to limit connections
- Disable public MongoDB access
- Use VPC/security groups

### Application Security
- Update dependencies regularly: `pip list --outdated`
- Rotate API credentials every 90 days
- Monitor error logs for attacks
- Use rate limiting (already configured at 100 req/min)

### MongoDB Access Control
- Use strong credentials (mix of upper, lower, numbers, symbols)
- Create role-specific users (read-only, read-write, admin)
- Rotate credentials every 90 days
- Enable MongoDB audit logging
- Restrict collections per user/role

Example MongoDB user creation:
```bash
mongosh "mongodb://admin:password@host:27017/admin"

db.createUser({
  user: "app_user",
  pwd: "strong_password_here",
  roles: [
    { role: "readWrite", db: "dataflow" }
  ]
})
```

## Disaster Recovery

### RTO/RPO Targets
- RTO (Recovery Time Objective): 1 hour
- RPO (Recovery Point Objective): 15 minutes

### Recovery Procedures
1. **Database Failure**: Restore from latest backup
   ```bash
   mongorestore --uri="mongodb://user:pass@host:27017" /backups/mongo_backup_folder
   ```

2. **Server Failure**: Restart application
   ```bash
   sudo systemctl restart dataflow-mcp
   ```

3. **Partial Data Loss**: Point-in-time restore (if available with MongoDB Atlas)

## Troubleshooting Production Issues

### High CPU Usage
```bash
# Check slow queries
mongosh "mongodb://user:pass@host:27017/dataflow"
db.setProfilingLevel(1, {slowms: 1000})
db.system.profile.find().sort({ts: -1}).limit(5).pretty()

# Check process
top -p $(pgrep -f "main.py")
```

### High Memory Usage
```bash
# Reduce connection pool
MONGO_POOL_SIZE=5

# Check memory
ps aux | grep "main.py" | grep -v grep

# Restart if needed
sudo systemctl restart dataflow-mcp
```

### Connection Timeouts
```bash
# Increase timeout
MONGO_TIMEOUT=10000

# Check network
ping mongodb-host
nc -zv mongodb-host 27017

# Check logs
sudo journalctl -u dataflow-mcp -n 50
```

### Application Won't Start
```bash
# Check Python version
python3 --version

# Check dependencies
pip list

# Check logs
sudo journalctl -u dataflow-mcp -n 100 --no-pager
```

## Post-Deployment Verification

- [ ] Health check endpoint responds: `curl http://localhost:5000/health`
- [ ] Logs are being written: `ls -la /var/log/dataflow-mcp/`
- [ ] MongoDB connection is stable
- [ ] SSL certificate is valid (if using Nginx)
- [ ] Rate limiting is applied
- [ ] Backups are being created
- [ ] Process runs as dedicated user
- [ ] Service starts on system boot

## Scaling Considerations

### Horizontal Scaling
- Deploy multiple instances behind load balancer
- Use connection pooling for MongoDB
- Distribute traffic evenly

### Database Scaling
- Enable MongoDB sharding for large datasets
- Use read replicas for read-heavy workloads
- Create indexes for common queries

### Resource Management
- Monitor CPU, memory, disk usage
- Rotate logs to prevent disk full
- Archive old logs regularly

---

For detailed documentation, refer to the main README.md and troubleshooting section.
