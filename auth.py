"""Small HMAC-signed bearer-token authentication for the dashboard."""
import base64, hashlib, hmac, json, os, time
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

SECRET = os.getenv('AUTH_SECRET', 'CHANGE_THIS_AUTH_SECRET_BEFORE_PRODUCTION')
security = HTTPBearer(auto_error=False)

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')

def issue_token(username: str, role: str) -> str:
    payload = _b64(json.dumps({'sub': username, 'role': role, 'exp': int(time.time()) + 8*60*60}).encode())
    sig = _b64(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest())
    return payload + '.' + sig

def current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials or credentials.scheme.lower() != 'bearer':
        raise HTTPException(status_code=401, detail='Please log in to continue.')
    try:
        payload, signature = credentials.credentials.split('.', 1)
        expected = _b64(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected): raise ValueError('signature')
        data = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
        if data['exp'] < int(time.time()): raise ValueError('expired')
        return {'username': data['sub'], 'role': data['role']}
    except Exception:
        raise HTTPException(status_code=401, detail='Session expired or invalid. Please log in again.')

def require_roles(*roles):
    def checker(user=Depends(current_user)):
        if user['role'] not in roles:
            raise HTTPException(status_code=403, detail='Your role does not have permission for this action.')
        return user
    return checker

# Passwords are salted PBKDF2 hashes, never stored as plain text.
def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or _b64(os.urandom(16))
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 240000)
    return salt + '$' + _b64(digest)

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split('$', 1)
        return hmac.compare_digest(password_hash(password, salt), stored)
    except Exception:
        return False
