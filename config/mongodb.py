import os
import logging
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError, ConnectionFailure

load_dotenv()

logger = logging.getLogger(__name__)

# Configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "dataflow")
MONGO_TIMEOUT = int(os.getenv("MONGO_TIMEOUT", "5000"))
MONGO_POOL_SIZE = int(os.getenv("MONGO_POOL_SIZE", "10"))
MONGO_MAX_IDLE_TIME = int(os.getenv("MONGO_MAX_IDLE_TIME", "45000"))

# Validate configuration
if not MONGO_URI:
    raise ValueError("MONGO_URI environment variable is required")
if not MONGO_DB_NAME:
    raise ValueError("MONGO_DB_NAME environment variable is required")

# Connection options for production
connection_options = {
    "serverSelectionTimeoutMS": MONGO_TIMEOUT,
    "socketTimeoutMS": MONGO_TIMEOUT,
    "connectTimeoutMS": MONGO_TIMEOUT,
    "maxPoolSize": MONGO_POOL_SIZE,
    "minPoolSize": 2,
    "maxIdleTimeMS": MONGO_MAX_IDLE_TIME,
    "retryWrites": True,
    "w": "majority",  # Write concern: majority
    "journal": True,  # Enable journaling
}

# Add SSL/TLS if needed (for production cloud deployments)
if os.getenv("MONGO_USE_TLS", "false").lower() == "true":
    connection_options["tls"] = True
    connection_options["tlsAllowInvalidCertificates"] = os.getenv(
        "MONGO_ALLOW_INVALID_CERTS", "false"
    ).lower() == "true"
    if ca_cert_path := os.getenv("MONGO_CA_CERT_PATH"):
        connection_options["tlsCAFile"] = ca_cert_path

try:
    client = MongoClient(MONGO_URI, **connection_options)
    # Test connection
    client.admin.command("ping")
    logger.info("✓ MongoDB connection established successfully")
except (ServerSelectionTimeoutError, ConnectionFailure) as e:
    logger.error(f"✗ Failed to connect to MongoDB: {e}")
    raise

db = client[MONGO_DB_NAME]