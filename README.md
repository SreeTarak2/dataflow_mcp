# DataFlow MCP Server - Production Grade

A secure, production-ready Model Context Protocol (MCP) server with MongoDB integration, featuring comprehensive security controls, CRUD operations, logging, and monitoring.

## 📋 Features

### Security
- ✅ **Input Validation & Sanitization** - Prevents NoSQL injection attacks
- ✅ **MongoDB SSL/TLS Support** - Secure cloud deployments
- ✅ **Rate Limiting** - Protects against abuse (100 req/min default)
- ✅ **Connection Pooling** - Optimized for performance
- ✅ **Document Size Limits** - Prevents resource exhaustion
- ✅ **Field Name Validation** - Blacklists dangerous operators

### Operations
- ✅ **CRUD Operations** - Create, Read, Update, Delete documents
- ✅ **Filtering & Pagination** - Flexible data retrieval with limits
- ✅ **Sorting Support** - Sort by any field (ascending/descending)
- ✅ **Bulk Operations Ready** - Extensible architecture

### Monitoring & Observability
- ✅ **Comprehensive Logging** - File & console with rotation
- ✅ **Health Checks** - Service health status endpoint
- ✅ **Metrics Tracking** - Request counts, success rates
- ✅ **Error Handling** - Detailed error reporting

### Production Ready
- ✅ **Security First** - SSL/TLS support, input validation
- ✅ **Environment Config** - 12-factor app ready
- ✅ **Graceful Shutdown** - Proper resource cleanup

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- Docker & Docker Compose (optional)
- MongoDB (or use Docker Compose)

### Local Development

1. **Clone and setup:**
```bash
cd dataflow_mcp
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -e .
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your MongoDB connection
```

3. **Run the server:**
```bash
python main.py
```



## 📡 API Tools

### Health Check
Get server status and metrics.

```json
{
  "status": "healthy",
  "uptime_seconds": 123.45,
  "metrics": {
    "total_requests": 42,
    "successful_requests": 40,
    "failed_requests": 2,
    "success_rate": 95.24
  }
}
```

### Read Collection
Retrieve documents with filtering, pagination, and sorting.

**Parameters:**
- `collection_name` (required): Collection name
- `filter_query`: JSON string with MongoDB filter
- `limit`: Max documents (default: 100, max: 1000)
- `skip`: Skip N documents (default: 0)
- `sort_by`: Field to sort by

**Example:**
```json
{
  "collection_name": "users",
  "filter_query": "{\"status\": \"active\"}",
  "limit": 10,
  "skip": 0,
  "sort_by": "created_at"
}
```

### Get Document
Retrieve a single document by ID.

**Parameters:**
- `collection_name`: Collection name
- `document_id`: MongoDB ObjectId as string

### Create Document
Create a new document in a collection.

**Parameters:**
- `collection_name`: Collection name
- `document_json`: JSON string representing the document

**Example:**
```json
{
  "collection_name": "users",
  "document_json": "{\"name\": \"John\", \"email\": \"john@example.com\", \"status\": \"active\"}"
}
```

### Update Document
Update an existing document.

**Parameters:**
- `collection_name`: Collection name
- `document_id`: MongoDB ObjectId as string
- `update_json`: JSON with fields to update

**Example:**
```json
{
  "collection_name": "users",
  "document_id": "65f8a1b2c3d4e5f6g7h8i9j0",
  "update_json": "{\"status\": \"inactive\", \"updated_at\": \"2024-01-01T12:00:00Z\"}"
}
```

### Delete Document
Delete a document from a collection.

**Parameters:**
- `collection_name`: Collection name
- `document_id`: MongoDB ObjectId as string

## 🔒 Security Features

### Input Validation
- Collection names: Alphanumeric, dash, underscore only
- Field names: Prevents dangerous operators ($where, $function, etc.)
- Filters: Maximum 10KB, blacklist dangerous operations
- Documents: Maximum 1MB, enforced size limits

### MongoDB Security
- **Connection Options:**
  - Connection pooling (default: 10 connections)
  - Retry writes enabled
  - Write concern: majority
  - Journaling enabled
  - SSL/TLS for cloud deployments

- **Environment Variables:**
  ```env
  MONGO_USE_TLS=true
  MONGO_CA_CERT_PATH=/path/to/ca.pem
  MONGO_ALLOW_INVALID_CERTS=false
  ```

### Rate Limiting
- 100 requests per 60 seconds (configurable)
- Per-client tracking
- Returns clear error on limit exceeded

### Error Handling
- Safe error messages (no sensitive data leaks)
- Detailed internal logging
- Graceful degradation

## 📊 Environment Variables

### Required
```env
MONGO_URI=mongodb://user:password@host:port/database
MONGO_DB_NAME=dataflow
```

### Optional (with defaults)
```env
MONGO_TIMEOUT=5000              # Connection timeout (ms)
MONGO_POOL_SIZE=10              # Connection pool size
MONGO_MAX_IDLE_TIME=45000       # Max idle time (ms)
MONGO_USE_TLS=false             # Enable TLS
MONGO_CA_CERT_PATH=             # CA certificate path
LOGS_DIR=./logs                 # Log directory
LOG_LEVEL=INFO                  # Logging level
```

## 📁 Project Structure

```
dataflow_mcp/
├── config/
│   ├── mongodb.py           # MongoDB connection with pooling
│   ├── security.py          # Validation and rate limiting
│   └── logging_config.py    # Logging setup
├── tools/
│   └── data_manager.py      # CRUD operations and update logic (DataManager)
├── scripts/
├── main.py                  # MCP server and tools
├── pyproject.toml          # Dependencies and config
└── .env.example            # Environment template
```

## 🔧 Configuration for Cloud Deployment

### AWS Deployment
```env
MONGO_URI=mongodb+srv://user:password@cluster.mongodb.net/dataflow
MONGO_USE_TLS=true
MONGO_ALLOW_INVALID_CERTS=false
```

### Azure Deployment
```env
MONGO_URI=mongodb://user:password@host.mongo.cosmos.azure.com:10255/database
MONGO_USE_TLS=true
MONGO_CA_CERT_PATH=/etc/ssl/certs/ca-certificates.crt
```

### GCP Deployment
```env
MONGO_URI=mongodb://user:password@instance:27017/database
MONGO_USE_TLS=true
```

## 🚨 Production Checklist

- [ ] MongoDB backups configured
- [ ] SSL/TLS certificates installed
- [ ] Environment variables set securely (not in code)
- [ ] Logs redirected to centralized logging
- [ ] Health checks configured in load balancer
- [ ] Rate limits adjusted for your use case
- [ ] MongoDB indexes optimized
- [ ] Connection pool size tuned
- [ ] Monitoring/alerting setup
- [ ] Graceful shutdown tested

## 📈 Performance Optimization

### MongoDB Indexes
Pre-created indexes in `scripts/mongo-init.js`:
- User email: unique constraint
- Timestamps: for sorting and TTL
- Status: for filtering

### Connection Pooling
- Default pool size: 10 (adjust via `MONGO_POOL_SIZE`)
- Min connections: 2 (automatically maintained)
- Max idle time: 45 seconds

### Request Limits
- Max filter size: 10KB
- Max document size: 1MB
- Max page size: 1000 documents
- Rate limit: 100 req/min

## 🧪 Testing & Development

### Install dev dependencies:
```bash
pip install -e ".[dev]"
```

### Run tests:
```bash
pytest --cov=tools --cov=config
```

### Code formatting:
```bash
black .
flake8 .
mypy .
```

## 📝 Logging

Logs are written to:
- **File:** `./logs/mcp_server_YYYYMMDD.log` (rotated daily, max 10MB)
- **Console:** Real-time output

Log levels:
- `DEBUG` - Detailed diagnostic info
- `INFO` - General events
- `WARNING` - Warning messages
- `ERROR` - Error events

## 🐛 Troubleshooting

### MongoDB Connection Failed
```
Check MONGO_URI and credentials
Verify MongoDB is running: mongosh "mongodb://..."
Check network connectivity and firewall
```

### Rate Limit Exceeded
```
Default: 100 requests per 60 seconds
Increase MONGO_POOL_SIZE and optimize queries
Implement request queuing on client
```

### High Memory Usage
```
Reduce MONGO_POOL_SIZE
Lower MONGO_MAX_IDLE_TIME
Check for large result sets (use pagination)
```

## 📚 References

- [FastMCP Documentation](https://mcp.run)
- [MongoDB Security](https://docs.mongodb.com/manual/security/)
- [Connection String Format](https://docs.mongodb.com/manual/reference/connection-string/)
- [Docker Best Practices](https://docs.docker.com/develop/dev-best-practices/)

## 📄 License

MIT License - See LICENSE file for details

## 👤 Support

For issues and questions:
1. Check troubleshooting section
2. Review logs in `./logs/`
3. Check MongoDB connection
4. Verify environment variables

---

**Built for production-grade data operations with security-first design.**
