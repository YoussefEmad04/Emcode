import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"
WTF_CSRF_ENABLED = True
SESSION_COOKIE_SECURE = False
TALISMAN_ENABLED = True
ENABLE_CORS = True
CORS_OPTIONS = {"origins": ["https://tile.openstreetmap.org"]}
DECKGL_BASE_MAP = [["https://tile.openstreetmap.org/{z}/{x}/{y}.png", "OpenStreetMap"]]
MAPBOX_API_KEY = ""
DEFAULT_RELATIVE_START_TIME = "now"
DEFAULT_RELATIVE_END_TIME = "now"

TALISMAN_CONFIG = {
    "force_https": False,
    "session_cookie_secure": False,
    "content_security_policy": {
        "default-src": ["'self'"],
        "img-src": ["'self'", "blob:", "data:", "https://tile.openstreetmap.org"],
        "worker-src": ["'self'", "blob:"],
        "connect-src": ["'self'", "https://tile.openstreetmap.org"],
        "object-src": ["'none'"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "script-src": ["'self'", "'strict-dynamic'"],
    },
    "content_security_policy_nonce_in": ["script-src"],
}

# A local, single-process sandbox: disable result caching so live dashboard polling sees inserts.
DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
FILTER_STATE_CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}
EXPLORE_FORM_DATA_CACHE_CONFIG = {"CACHE_TYPE": "SimpleCache", "CACHE_DEFAULT_TIMEOUT": 300}

