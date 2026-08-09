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
├── core.py            # FastMCP instance, rate limiter, metrics, prompt loading, normalization
├── server.py          # tool registration + mcp.run()
├── tools/
│   ├── health.py      # health_check, database_status
│   ├── crud.py        # generic MongoDB CRUD tools
│   ├── images.py      # contest banner pipeline (missing/broken images, cover prompts)
│   ├── migration.py   # v4.0 schema migration/backfill tools
│   ├── contests.py    # structuring + full generation + detail generation
│   ├── events.py      # events pipeline (fetch → structure → submit → query)
│   ├── raw_data.py    # raw scraped data bridge + overview
│   ├── validation.py  # chatbot-driven web validation pipeline
│   └── audit.py       # duplicate audit + discrepancy flagging
config/
├── mongodb.py           # MongoDB connection with pooling
├── security.py          # Validation and rate limiting
└── logging_config.py    # Logging setup
tools/                   # service layer (DataManager, generators, dedup gate, validators)
prompts/                 # prompt files (descriptive names + Prompts*.txt aliases)
main.py                  # thin entry point → dataflow_mcp.server
├── pyproject.toml      # Dependencies and config (console script: dataflow-mcp)
└── .env.example        # Environment template
```

## 🎪 Events Pipeline

The MCP server includes a full events pipeline so AI chatbots can harvest and
structure participatory events (conferences, summits, workshops, webinars,
meetups, trainings, …):

```
1. get_records_for_events(source=..., limit=10)  → raw URLs + events-v1.1 prompt
2. [chatbot researches each URL and outputs event JSON]
3. submit_structured_events(events_json)         → persists to the Events collection
4. get_events(event_type=..., upcoming_only=true) → read structured events back
5. get_events_overview()                          → counts by type/status
6. get_events_for_detail_generation(batch_size=10) → events + event-details-v1.0.txt prompt
7. [chatbot researches and writes event details]
8. submit_event_details(event_id, details_json)  → versioned event_details saved
9. get_event_detail_status()                       → coverage metrics (remaining events to generate)
```

Event **detail pages** mirror the contest detail flow: `EventDetailGenerator`
(`tools/event_detail_generator.py`) provides the priority queue, quality
validation, and versioned `event_details` storage.

Prompt files were renamed to descriptive names (`contest-structuring-v4.0.txt`,
`event-structuring-v1.1.txt`, …) with the old `Prompts*.txt` names kept as
aliases. See `TOOLS_REFERENCE.md` for the full tool reference.

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
# dataflow_mcp
