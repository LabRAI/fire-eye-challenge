"""External account/database check; never generates artificial user activity."""
import argparse
import datetime as dt
import json
import time
import urllib.request
from pathlib import Path

DEFAULT = 'https://eqgdsqomvegnmsuwxnjo.supabase.co/functions/v1/fire-eye-account/health'

def check(endpoint=DEFAULT):
    if endpoint != DEFAULT:
        raise ValueError('Use the configured public account health endpoint.')
    for attempt in range(3):
        try:
            request = urllib.request.Request(endpoint, headers={'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(request, timeout=25) as response:
                body = json.loads(response.read(8192))
            if body.get('ready') is True and body.get('storage_ready') is True:
                return {'status': 'healthy', 'database_checked': True, 'attempts': attempt + 1}
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 ** attempt)
    return {'status': 'unavailable', 'database_checked': False, 'attempts': 3}

def main():
    p = argparse.ArgumentParser(); p.add_argument('--output', required=True); args = p.parse_args()
    result = {'checked_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), **check()}
    path = Path(args.output); path.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(path.read_text()) if path.exists() else {}
    if result['status'] == 'healthy':
        result['last_success_at_utc'] = result['checked_at_utc']
    elif previous.get('last_success_at_utc'):
        result['last_success_at_utc'] = previous['last_success_at_utc']
    path.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result['status'] == 'healthy' else 1

if __name__ == '__main__':
    raise SystemExit(main())
