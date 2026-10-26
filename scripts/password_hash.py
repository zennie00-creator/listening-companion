"""Run locally; only a one-way hash is printed. Never enter the password in chat."""
import getpass
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from listening.auth import hash_password
if __name__ == '__main__':
    first = getpass.getpass('새 앱 비밀번호 (12자 이상): ')
    if first != getpass.getpass('비밀번호 확인: '):
        raise SystemExit('두 입력이 다릅니다.')
    print(hash_password(first))
