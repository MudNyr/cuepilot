"""
Claude on Google Cloud (Vertex AI) for CuePilot.

Vertex doesn't accept API keys. It uses Application Default Credentials (ADC): the Google
sign-in saved on this PC when you run

    gcloud auth application-default login

This module turns that saved sign-in into a short-lived access token (refreshed automatically)
and sends Claude requests in Vertex's format:
  * the model goes in the URL, not the request body
  * "anthropic_version": "vertex-2023-10-16" goes in the body
  * the response is the normal Claude Messages format

No extra packages are needed for a normal personal sign-in. Service-account key files need the
optional 'google-auth' package. No Qt dependency, so it can be unit-tested on its own.
"""
from __future__ import annotations

BUILD = "2026.10.21-8"  # must match APP_BUILD in cuepilot.py

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
VERTEX_VERSION = "vertex-2023-10-16"
SIGN_IN_HELP = ("Install the Google Cloud CLI (cloud.google.com/sdk/docs/install), then run "
                "'gcloud auth application-default login' and sign in with your Google account.")


class GoogleAuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------- sign-in
def adc_path() -> Path:
    """Where 'gcloud auth application-default login' saves the sign-in."""
    env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "gcloud" / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


_cache = {"token": None, "expires": 0.0, "quota_project": None}
_lock = threading.Lock()


def access_token(force_refresh: bool = False):
    """Return (access_token, quota_project_or_None), refreshing when it's about to expire."""
    with _lock:
        if not force_refresh and _cache["token"] and time.time() < _cache["expires"] - 60:
            return _cache["token"], _cache["quota_project"]
        token, expires_in, quota = _fetch_token()
        _cache.update(token=token, expires=time.time() + float(expires_in), quota_project=quota)
        return token, quota


def clear_cached_token():
    with _lock:
        _cache.update(token=None, expires=0.0)


def _fetch_token():
    path = adc_path()
    info = None
    if path.exists():
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise GoogleAuthError(f"Couldn't read your Google sign-in file ({path}): {exc}") from None
    kind = (info or {}).get("type")
    quota = (info or {}).get("quota_project_id")
    if kind == "authorized_user":
        token, expires = _refresh_user(info)
        return token, expires, quota
    if kind == "service_account":
        token, expires = _service_account_token(path)
        return token, expires, quota
    # Other sign-in types (impersonation, workforce identity) or no file: let gcloud handle it.
    token, expires = _gcloud_token(have_file=info is not None)
    return token, expires, quota


def _refresh_user(info):
    missing = [k for k in ("client_id", "client_secret", "refresh_token") if not info.get(k)]
    if missing:
        raise GoogleAuthError("Your Google sign-in file is incomplete. " + SIGN_IN_HELP)
    form = urllib.parse.urlencode({
        "client_id": info["client_id"], "client_secret": info["client_secret"],
        "refresh_token": info["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=form, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "CuePilot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        reason = ""
        try:
            reason = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
        except Exception:
            pass
        if reason == "invalid_grant":
            raise GoogleAuthError("Your Google sign-in has expired or was revoked. Run "
                                  "'gcloud auth application-default login' again.") from None
        raise GoogleAuthError(f"Google refused to refresh your sign-in (HTTP {exc.code} "
                              f"{reason}).") from None
    except urllib.error.URLError as exc:
        raise GoogleAuthError(f"Couldn't reach Google to refresh your sign-in: {exc.reason}.") from None
    token = data.get("access_token")
    if not token:
        raise GoogleAuthError("Google didn't return an access token. " + SIGN_IN_HELP)
    return token, data.get("expires_in", 3600)


def _service_account_token(path):
    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as gtr
    except ImportError:
        raise GoogleAuthError("Your sign-in file is a service-account key, which needs the "
                              "'google-auth' and 'requests' packages (pip install google-auth "
                              "requests). Or sign in as yourself instead: "
                              "'gcloud auth application-default login'.") from None
    try:
        creds = service_account.Credentials.from_service_account_file(str(path), scopes=[SCOPE])
        creds.refresh(gtr.Request())
    except Exception as exc:
        raise GoogleAuthError(f"The service-account key didn't work: {exc}") from None
    expires = 3300
    if getattr(creds, "expiry", None):
        expires = max(60, int(creds.expiry.timestamp() - time.time()))
    return creds.token, expires


def _gcloud_token(have_file: bool):
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        if have_file:
            raise GoogleAuthError("Your Google sign-in needs the Google Cloud CLI to refresh. "
                                  + SIGN_IN_HELP)
        raise GoogleAuthError("No Google sign-in found on this PC. " + SIGN_IN_HELP)
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flash
    try:
        out = subprocess.run([exe, "auth", "application-default", "print-access-token"],
                             capture_output=True, text=True, timeout=30, creationflags=flags)
    except Exception as exc:
        raise GoogleAuthError(f"Couldn't run the Google Cloud CLI: {exc}") from None
    token = (out.stdout or "").strip().splitlines()[-1:] or [""]
    token = token[0].strip()
    if out.returncode != 0 or not token:
        msg = (out.stderr or "").strip().splitlines()[-1:] or ["no details"]
        raise GoogleAuthError(f"The Google Cloud CLI couldn't get a token ({msg[0][:160]}). "
                              + SIGN_IN_HELP)
    return token, 3000


# --------------------------------------------------------------------------- embeddings
EMBED_WORKERS = 6         # gemini-embedding-001 takes one text per request, so send several at once
EMBED_RETRY_CODES = (429, 500, 502, 503, 504)
TASK_TYPES = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}


def vertex_embed_url(project: str, region: str, model: str) -> str:
    region = (region or "us-central1").strip()
    host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    return (f"https://{host}/v1/projects/{urllib.parse.quote(project.strip())}/locations/{region}"
            f"/publishers/google/models/{urllib.parse.quote(model.strip(), safe='@.-_')}:predict")


def vertex_embed(cfg: dict, texts, input_type=None, timeout: float = 60, retries: int = 4):
    """Embed texts with a Google model on Vertex AI using your Google sign-in. Returns one vector
    per text, in order. Rate limits (429) and temporary errors are retried with a backoff."""
    project = (cfg.get("project") or "").strip()
    model = (cfg.get("model") or "gemini-embedding-001").strip()
    if not project:
        raise RuntimeError("Enter your Google Cloud project ID for document search.")
    url = vertex_embed_url(project, cfg.get("region") or "us-central1", model)
    task = TASK_TYPES.get(input_type or "")
    try:
        access_token()  # sign in once up front, so parallel requests share the token
    except GoogleAuthError as exc:
        raise RuntimeError(str(exc)) from None

    def one(text):
        instance = {"content": text}
        if task:
            instance["task_type"] = task
        body = {"instances": [instance]}
        attempt = 0
        refreshed = False
        while True:
            token, quota = access_token()
            try:
                data = _post(url, body, token, quota, timeout)
                return data["predictions"][0]["embeddings"]["values"]
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not refreshed:
                    refreshed = True
                    try:
                        access_token(force_refresh=True)
                    except GoogleAuthError as auth_exc:
                        raise RuntimeError(str(auth_exc)) from None
                    continue
                if exc.code in EMBED_RETRY_CODES and attempt < retries:
                    wait = None
                    try:
                        wait = float(exc.headers.get("Retry-After"))
                    except Exception:
                        pass
                    time.sleep(max(0.5, min(wait if wait is not None else 2.0 ** attempt, 30.0)))
                    attempt += 1
                    continue
                raise RuntimeError("Document search: " + _vertex_error(exc).replace(
                    "the model ID (copy it from Model Garden)", "the embeddings model name")) from None
            except urllib.error.URLError as exc:
                raise RuntimeError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: "
                                   f"{exc.reason}.") from None
            except (KeyError, IndexError, TypeError):
                raise RuntimeError("Google Cloud sent an unexpected embeddings reply. Check the "
                                   "model name.") from None

    texts = list(texts)
    if len(texts) <= 1:
        return [one(t) for t in texts]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
        return list(pool.map(one, texts))


# --------------------------------------------------------------------------- Claude on Vertex
def vertex_url(project: str, region: str, model: str, base_override: str | None = None) -> str:
    region = (region or "global").strip()
    host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    base = (base_override or f"https://{host}").rstrip("/")
    return (f"{base}/v1/projects/{urllib.parse.quote(project.strip())}/locations/{region}"
            f"/publishers/anthropic/models/{urllib.parse.quote(model.strip(), safe='@.-_')}:rawPredict")


def _post(url, body, token, quota_project, timeout):
    headers = {"Content-Type": "application/json", "User-Agent": "CuePilot/1.0",
               "Authorization": "Bearer " + token}
    if quota_project:
        headers["x-goog-user-project"] = quota_project
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _vertex_error(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(payload, list) and payload:
            payload = payload[0]
        err = payload.get("error", payload) if isinstance(payload, dict) else payload
        detail = err.get("message", "") if isinstance(err, dict) else str(err)
    except Exception:
        pass
    hint = {
        401: "your Google sign-in was rejected; run 'gcloud auth application-default login' again",
        403: "your Google account needs permission to use Vertex AI in this project, and Claude "
             "must be enabled for the project in Vertex AI Model Garden",
        404: "check the project ID, the model ID (copy it from Model Garden) and the region; "
             "'global' works for most recent models",
        429: "Vertex quota or rate limit reached; wait a moment, or request more quota",
    }.get(exc.code, "")
    msg = f"Google Cloud returned HTTP {exc.code}"
    if detail:
        msg += f": {detail[:300]}"
    if hint:
        msg += f" ({hint})"
    return msg


def vertex_complete(cfg: dict, system: str, user: str, max_tokens: int = 1500,
                    timeout: float = 120) -> str:
    """Send one Claude request through Vertex AI and return the reply text."""
    project = (cfg.get("project") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not project:
        raise RuntimeError("Enter your Google Cloud project ID.")
    if not model:
        raise RuntimeError("Enter the Claude model ID from Vertex AI Model Garden.")
    url = vertex_url(project, cfg.get("region") or "global", model, cfg.get("host_override"))
    body = {"anthropic_version": VERTEX_VERSION, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    try:
        token, quota = access_token()
    except GoogleAuthError as exc:
        raise RuntimeError(str(exc)) from None
    for attempt in (1, 2):
        try:
            data = _post(url, body, token, quota, timeout)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                try:  # the token may have just expired: refresh once and retry
                    token, quota = access_token(force_refresh=True)
                    continue
                except GoogleAuthError as auth_exc:
                    raise RuntimeError(str(auth_exc)) from None
            raise RuntimeError(_vertex_error(exc)) from None
        except urllib.error.URLError as exc:
            host = urllib.parse.urlsplit(url).netloc
            raise RuntimeError(f"Could not reach {host}: {exc.reason}. Check your internet "
                               "connection.") from None
        except (TimeoutError, OSError) as exc:
            raise RuntimeError(f"Google Cloud didn't answer in time ({exc}). Try again.") from None
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise RuntimeError("Google Cloud returned an empty reply. Try again or pick another model.")
    return text.strip()
