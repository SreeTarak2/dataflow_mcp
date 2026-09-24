import os
import logging
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError, ConnectionFailure

load_dotenv()

logger = logging.getLogger(__name__)

# Configuration — Primary (ContestHopperDb)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "dataflow")

# Timeouts are DECOUPLED on purpose:
#   serverSelection = how long to hunt for a suitable server (must stay short)
#   socketTimeout   = how long a single operation may hold the socket (real
#                     aggregations/structuring writes exceed 5s — a tight value
#                     kills sockets mid-process and forces reconnect+re-auth)
#   connectTimeout  = TCP connect only
MONGO_SERVER_SELECTION_TIMEOUT = int(
    os.getenv("MONGO_SERVER_SELECTION_TIMEOUT", os.getenv("MONGO_TIMEOUT", "10000")))
MONGO_SOCKET_TIMEOUT = int(os.getenv("MONGO_SOCKET_TIMEOUT", "120000"))
MONGO_CONNECT_TIMEOUT = int(os.getenv("MONGO_CONNECT_TIMEOUT", "10000"))
MONGO_POOL_SIZE = int(os.getenv("MONGO_POOL_SIZE", "10"))
# pymongo requires maxIdleTimeMS > 0 (no "never" sentinel), so use 24h:
# connections stay warm for the life of an MCP session instead of being culled
# after 45s. Atlas authenticates every new connection (SCRAM handshake), so
# idle-culling turns each bursty tool call into a re-authentication storm.
# Pool is bounded by maxPoolSize anyway.
MONGO_MAX_IDLE_TIME = int(os.getenv("MONGO_MAX_IDLE_TIME", "86400000"))

# Configuration — Raw Data (CHRawdata, separate cluster)
RAW_MONGO_URI = os.getenv("RAW_MONGO_URI")
RAW_DB_NAME = os.getenv("RAW_DB_NAME", "CHRawdata")
RAW_COLLECTION = os.getenv("RAW_COLLECTION", "rawdata")

# Validate configuration
if not MONGO_URI:
    raise ValueError("MONGO_URI environment variable is required")
if not MONGO_DB_NAME:
    raise ValueError("MONGO_DB_NAME environment variable is required")
if not RAW_MONGO_URI:
    raise ValueError("RAW_MONGO_URI environment variable is required for raw data processing")

# Connection options for production
def _build_connection_options() -> dict:
    """Build standard connection options dict (reusable across connections)."""
    opts = {
            "serverSelectionTimeoutMS": MONGO_SERVER_SELECTION_TIMEOUT,
            "socketTimeoutMS": MONGO_SOCKET_TIMEOUT,
            "connectTimeoutMS": MONGO_CONNECT_TIMEOUT,
            "maxPoolSize": MONGO_POOL_SIZE,
            "minPoolSize": 2,
            "maxIdleTimeMS": MONGO_MAX_IDLE_TIME,
            "retryWrites": True,
            "retryReads": True,
            "w": "majority",
            "journal": True,
        }
    if os.getenv("MONGO_USE_TLS", "false").lower() == "true":
        opts["tls"] = True
        opts["tlsAllowInvalidCertificates"] = (
            os.getenv("MONGO_ALLOW_INVALID_CERTS", "false").lower() == "true"
        )
        if ca_cert_path := os.getenv("MONGO_CA_CERT_PATH"):
            opts["tlsCAFile"] = ca_cert_path
    return opts

try:
    client = MongoClient(MONGO_URI, **_build_connection_options())
    logger.info("\u2713 MongoDB client initialized successfully")
except Exception as e:
    logger.error(f"\u2717 Failed to initialize MongoDB client: {e}")
    raise

db = client[MONGO_DB_NAME]

# --- Raw data connection (separate cluster, lazy) ---
_raw_client: MongoClient | None = None
_raw_db = None


def get_raw_db():
    """Get the raw data database handle (cluster A / CHRawdata).
    Lazily initialized; reuses the client across calls."""
    global _raw_client, _raw_db
    if _raw_db is not None:
        return _raw_db
    try:
        _raw_client = MongoClient(RAW_MONGO_URI, **_build_connection_options())
        _raw_db = _raw_client[RAW_DB_NAME]
        logger.info(f"\u2713 Raw MongoDB client initialized (db={RAW_DB_NAME})")
        return _raw_db
    except Exception as e:
        logger.error(f"\u2717 Failed to initialize raw MongoDB client: {e}")
        raise


def close_raw_connection():
    """Close the raw data connection explicitly."""
    global _raw_client, _raw_db
    if _raw_client:
        _raw_client.close()
        _raw_client = None
        _raw_db = None
        logger.info("Raw MongoDB connection closed")


def ping_database() -> tuple[bool, str | None]:
    """Check whether MongoDB is reachable without failing app startup."""
    try:
        client.admin.command("ping")
        return True, None
    except (ServerSelectionTimeoutError, ConnectionFailure) as e:
        logger.warning(f"MongoDB ping failed: {e}")
        return False, str(e)
