"""Detectors are calibrated before they are trusted (epistemic-toolkit, component 6).

The secret scanner gates what may be sent to model providers, so both error rates matter:
a false positive aborts a legitimate CI run, a false negative leaks a credential.
Rates are certified with Wilson-95 bounds, not point estimates, on seeds disjoint from
the ones used while tuning the scanner.
"""
import random
import string

from verification_compiler.static_checks import secret_scan

from . import fakes

wilson = fakes.toolkit().instrument.wilson
trials_needed = fakes.toolkit().instrument.trials_needed

MAX_FALSE_POSITIVE_RATE = 0.01
MIN_TRUE_POSITIVE_RATE = 0.97
HELD_OUT_SEEDS = (1013, 2027)


def _rnd(r, n, alphabet=string.ascii_letters + string.digits + "_-"):
    return "".join(r.choice(alphabet) for _ in range(n))


# Benign lines that *look* credential-related and appear in ordinary auth services.
BENIGN = [
    lambda r: f'token_url = "/api/v{r.randint(1, 3)}/auth/{r.choice(["token", "login", "refresh"])}"',
    lambda r: f'SECRET_KEY = os.environ["{r.choice(["SECRET_KEY", "JWT_SECRET", "APP_SECRET"])}"]',
    lambda r: f'{r.choice(["jwt_secret", "api_key", "db_password"])} = os.getenv("{_rnd(r, 8, string.ascii_uppercase)}", "")',
    lambda r: f'return {{"access_token": token, "token_type": "{r.choice(["bearer", "Bearer"])}"}}',
    lambda r: f"password: str = Field(min_length={r.randint(8, 16)})",
    lambda r: 'api_key_header = APIKeyHeader(name="X-API-Key")',
    lambda r: f'raise HTTPException(401, detail="{r.choice(["Invalid token", "Token expired", "Bad credentials"])}")',
    lambda r: f"TOKEN_TTL_SECONDS = {r.randint(60, 86400)}",
    lambda r: f"secret = secrets.token_urlsafe({r.choice([16, 32, 64])})",
    lambda r: f'commit = "{_rnd(r, 40, "0123456789abcdef")}"',
    lambda r: f'{r.choice(["token_type", "grant_type"])} = "{r.choice(["access_token", "refresh_token", "client_credentials"])}"',
    lambda r: f'password_reset_path = "{r.choice(["password-reset", "reset_password", "forgot-password"])}"',
    lambda r: f'{r.choice(["token_header", "api_key_name", "secret_field"])} = "{r.choice(["Authorization", "X-Api-Key-Id", "client_secret"])}"',
    lambda r: f'algorithm = "{r.choice(["HS256", "RS256", "ES256"])}"',
    lambda r: f'# The {r.choice(["api_key", "token", "secret"])} is loaded from the environment',
]

# Hardcoded credentials in the forms generated code actually uses, including weak placeholders.
SECRET = [
    lambda r: f'JWT_SECRET = "{_rnd(r, r.randint(16, 48))}"',
    lambda r: f'{r.choice(["api_key", "API_KEY", "apiKey"])} = "{_rnd(r, 32)}"',
    lambda r: f'config = {{"password": "{_rnd(r, r.randint(10, 20), string.ascii_letters + string.digits + "!@#")}"}}',
    lambda r: f'aws_access_key_id = "AKIA{_rnd(r, 16, string.ascii_uppercase + string.digits)}"',
    lambda r: f'GITHUB = "ghp_{_rnd(r, 36, string.ascii_letters + string.digits)}"',
    lambda r: f'client = OpenAI(api_key="sk-{_rnd(r, 40)}")',
    lambda r: "-----BEGIN RSA PRIVATE KEY-----",
    lambda r: f'SIGNING_KEY = "{_rnd(r, 64, "0123456789abcdef")}"',
    lambda r: f'db_password = "{r.choice(["hunter2hunter", "correcthorsebattery", "P@ssw0rd2024!"])}"',
    lambda r: f'{r.choice(["SECRET_KEY", "JWT_SECRET"])} = "{r.choice(["super-secret-key", "change-me-in-production"])}"',
    lambda r: f'settings = dict(secret_key="{_rnd(r, 24)}")',
]


def _flagged(generators, n, seed):
    r = random.Random(seed)
    lines = [r.choice(generators)(r) for _ in range(n)]
    return [line for line in lines if not secret_scan([{"path": "app.py", "content": line}]).passed], n


def test_false_positive_rate_is_certified():
    n = 2 * trials_needed(MAX_FALSE_POSITIVE_RATE)       # 381 clean trials certify 1%; use twice that
    hits, n = _flagged(BENIGN, n, HELD_OUT_SEEDS[0])
    upper = wilson(len(hits), n)[1]
    assert upper <= MAX_FALSE_POSITIVE_RATE, f"FPR upper bound {upper:.4f}; false positives: {sorted(set(hits))[:5]}"


def test_true_positive_rate_is_certified():
    hits, n = _flagged(SECRET, 600, HELD_OUT_SEEDS[1])
    lower = wilson(len(hits), n)[0]
    assert lower >= MIN_TRUE_POSITIVE_RATE, f"TPR lower bound {lower:.4f}"
