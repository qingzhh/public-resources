"""Private container CLI: use restricted credentials, output safe API results."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import seedkeep_configuration as configuration


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('status', 'run', 'enable', 'disable', 'check'))
    args = parser.parse_args()
    try:
        settings = json.loads(Path(os.environ.get('SEEDKEEP_CONFIG', '/data/docker_settings.json')).read_text())
        auth_path = Path(os.environ.get('SEEDKEEP_CONFIG', '/data/docker_settings.json')).parent / 'web_auth.json'
        fallback = json.loads((auth_path if auth_path.exists() else Path(settings['source_config'])).read_text(encoding='utf-8-sig')) if not settings.get('tr_credentials_file') else None
        source = configuration.tr_credentials(settings, fallback)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base = 'http://127.0.0.1:' + os.environ.get('WEB_PORT', '8786')
        def post(path, payload):
            with opener.open(urllib.request.Request(base + path, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'X-Seedkeep-Request': '1'}), timeout=90) as response:
                return json.load(response)
        post('/api/login', {key: source[key] for key in ('username', 'password')})
        if args.action in ('enable', 'disable'):
            post('/api/automation', {'enabled': args.action == 'enable'})
        elif args.action != 'status':
            post('/api/' + args.action, {})
        with opener.open(base + '/api/status', timeout=90) as response:
            result = json.load(response)
        result.pop('destination', None)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        print(json.dumps({'error': 'web_http_' + str(code)}))
        return 1
    except Exception as error:
        print(json.dumps({'error': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
