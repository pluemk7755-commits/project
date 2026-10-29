"""Launch Roo Nai Gor Dai with optional local SMTP credentials.

Credentials are read only from data/smtp.env on the owner's computer.
Never commit that file to a public repository.
"""
from pathlib import Path
import os

SMTP_KEYS = ('RNGD_SMTP_HOST', 'RNGD_SMTP_PORT', 'RNGD_SMTP_USERNAME',
             'RNGD_SMTP_PASSWORD', 'RNGD_SMTP_FROM')


def load_local_smtp():
    settings_file = Path(__file__).resolve().parent / 'data' / 'smtp.env'
    if not settings_file.is_file():
        return
    for line in settings_file.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key not in SMTP_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            os.environ[key] = value


load_local_smtp()

from webapp.server import start
import argparse
if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Roo Nai Gor Dai Web')
    parser.add_argument('--host', default='127.0.0.1',help='127.0.0.1 local; 0.0.0.0 LAN only')
    parser.add_argument('--port', type=int,default=8000)
    args=parser.parse_args()
    start(args.host,args.port)
