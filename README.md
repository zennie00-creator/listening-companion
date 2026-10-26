# Listening Companion · Streamlit

감상 원문·사진·청취 환경·청음 실험을 개인 기록으로 보관하는 **별도 Streamlit 앱**입니다. 기존 일지 에이전트와 저장소, 데이터, 앱 설정을 공유하지 않습니다.

## 운영 구조

- GitHub `zennie00-creator/listening-companion`: 코드와 PR·Merge 이력.
- Streamlit Community Cloud: Python 앱 실행.
- 사용자 소유 PostgreSQL: 감상, 사진 원본, 수정 이력을 영구 보관.
- 기존 Listening Companion의 OpenAI API 키: 앱의 Secrets에만 등록.
- 개인 백업 ZIP: GitHub에 올리지 않고 앱에서 직접 가져오기.

Cloudflare 계정이나 Sites 배포 도구는 필요하지 않습니다. 기존 Sites의 인증 헤더도 사용하지 않습니다. 한 사람의 개인 감상실이며, 앱 비밀번호로 기록을 보호합니다. 여러 사람이 서로 다른 기록을 저장하는 서비스로 확장하려면 별도의 사용자 인증과 소유권 설계가 필요합니다.

## 포함한 기능

- 음반을 지정하지 않아도 감상·음악적 느낌·오디오적 포인트 저장.
- 음반·아티스트 검색: Apple iTunes 카탈로그와 MusicBrainz.
- Apple Music / TIDAL **앨범 공유 주소 직접 연결**. 직접 입력 정보임을 표시.
- 재킷 사진을 AI로 읽어 검색 단서를 만드는 기능. 모델이 생성한 음반 ID·URL은 사용하지 않음.
- 오늘의 요청·장르·청취 경험에 따른 AI 검색 계획과 실제 카탈로그 대조.
- 개인 감상 원문을 AI에 참고시키는 기능은 명시적인 선택 사항.
- 기존 13개 리뷰 음반과 출처·감상 포인트 비교. 리뷰 자료는 AI 입력에 사용하지 않음.
- 감상 보완과 이전 작성 이력, 기록 당시 환경 스냅샷 보존.
- 청취 환경 등록·보완, A/B 청음 실험과 결과, 한 번 정정과 원래 결과 보존.
- ZIP 전체 내보내기 / 가져오기. 기록 충돌·사진 누락·손상은 가져오기 전에 검증.

## 로컬 실행

Python 3.11 이상을 권장합니다.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
LC_DEPLOYMENT_MODE=local streamlit run streamlit_app.py --server.address 127.0.0.1
```

로컬 모드는 이 컴퓨터의 `data/listening.sqlite3`에 저장하며 앱 비밀번호를 생략합니다. 로컬 모드를 인터넷에 공개하지 마세요. 외부 PostgreSQL을 테스트하려면 `DATABASE_URL` 환경 변수를 추가할 수 있습니다.

## 기존 기록 가져오기

1. 기존 사이트에서 받은 개인 백업 ZIP을 안전한 개인 폴더에 보관합니다.
2. 새 앱의 **백업·가져오기**에서 ZIP을 선택합니다.
3. 표시되는 감상·사진·이력·환경·실험 건수를 확인하고 **이 백업을 내 앱으로 가져오기**를 누릅니다.
4. 감상 기록에서 긴 원문, 사진, 수정 이력을 확인합니다. 환경과 실험도 확인합니다.

가져오기는 한 트랜잭션으로 처리됩니다. 동일한 기록은 중복 생성하지 않으며, 같은 ID의 다른 내용이 이미 있으면 전체 가져오기를 중단합니다. 과거 원문을 덮어쓰지 않습니다. 사진이 빠진 ZIP도 거부합니다.

Sites 내보내기를 앱 백업 형식으로 바꾸는 `scripts/convert_sites_backup.py`도 포함했습니다. 변환 대상은 인증된 기존 사이트에서 읽은 원문과 실제 사진 파일이어야 합니다. 화면에 일부가 생략된 데이터베이스 조회 결과는 전체 백업으로 사용하면 안 됩니다.

## Streamlit Community Cloud에 별도 앱으로 배포

**코드를 Merge하는 것만으로 최초 앱과 저장소가 자동 생성되지는 않습니다.** 첫 배포에서 아래 연결이 필요합니다.

1. 이 폴더의 소스를 `listening-companion` 저장소의 루트에 올립니다. 개인 백업, SQLite 파일, 비밀 설정은 제외합니다.
2. 영구 저장용 PostgreSQL 데이터베이스를 사용자 계정에 준비합니다. 기존 일지 에이전트의 데이터베이스나 인증 정보는 사용하지 않습니다. 사진도 같은 전용 DB에 바이너리로 저장하므로 별도의 파일 저장 계정은 필요하지 않습니다.
3. 로컬에서 앱 비밀번호의 해시를 생성합니다. 비밀번호 입력은 화면에 표시되지 않습니다.

```sh
python scripts/password_hash.py
```

4. `share.streamlit.io`에서 **Create app**을 선택합니다.
5. 저장소 `zennie00-creator/listening-companion`, 브랜치 `main`, 실행 파일 `streamlit_app.py`를 지정합니다.
6. 이 앱의 **Secrets**에 `.streamlit/secrets.toml.example`에 적힌 항목을 등록합니다.
   - `OPENAI_API_KEY`: 기존 Listening Companion 키를 재사용. 채팅·GitHub에 붙여 넣지 않음.
   - `OPENAI_MODEL`: 기본 `gpt-4.1-mini`, 기존 모델 설정이 있으면 유지 가능.
   - `DATABASE_URL`: 전용 PostgreSQL 연결 주소. TLS를 제공하는 공급자의 안내에 따라 `sslmode=require` 등 적용.
   - `APP_PASSWORD_HASH`: 위 명령의 해시 결과. 원래 비밀번호를 GitHub에 저장하지 않음.
   - `LC_DEPLOYMENT_MODE = "cloud"`
7. **Deploy** 후 앱 비밀번호로 접속하고 백업을 가져옵니다. 앱 공유 범위도 개인 사용에 맞게 제한합니다.
8. 감상·사진·수정 이력과 새 기록 저장을 확인한 뒤 Sites 종료 여부를 결정합니다.

Streamlit Cloud는 로컬 파일의 영구 보관을 보장하지 않습니다. 그래서 이 앱은 Cloud 모드에서 SQLite를 허용하지 않으며, 영구 DB가 설정되기 전에는 기록 화면을 열지 않습니다.

공식 참고:
- https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy
- https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management
- https://docs.streamlit.io/develop/concepts/connections/connecting-to-data

## 코드 변경 → PR → Merge → 앱 반영

최초 배포 후 Streamlit이 `main`을 보도록 연결되어 있으면 PR을 Merge하여 업데이트할 수 있습니다. 앱 관리 화면에서 실행 상태와 로그를 확인합니다. 앱을 종료·재시작해도 영구 DB의 기록은 유지됩니다.

현재 GitHub 앱 권한으로 업로드가 거부되면 GitHub **Settings → Applications → Installed GitHub Apps → 해당 ChatGPT/OpenAI 앱의 Configure → Repository access**에서 `listening-companion`을 선택합니다. 기존 저장소 선택은 유지합니다. `All repositories`로 넓힐 필요는 없습니다.

## 검사

```sh
python -m unittest discover -s tests -v
```

- 코어 검사는 SQLite와 표준 라이브러리로 실행됩니다.
- Streamlit을 설치하면 AppTest 화면 검사도 실행됩니다.
- 실제 OpenAI 호출이나 운영 개인 데이터는 CI에 사용하지 않습니다.
- 이전 앱의 운영 데이터는 저장소와 테스트에 포함하지 않습니다.

## 현재 이전 상태

독립 앱 코드와 백업 가져오기 기능을 준비했습니다. 로컬 코어 검사와 실제 개인 백업의 복원 검증 결과는 별도 이전 보고서에 기록합니다. Streamlit 설치·브라우저 UI 검사·PostgreSQL 실서비스 연결·클라우드 배포는 완료 결과를 확인하기 전까지 검증 완료로 간주하지 않습니다. 기존 Sites는 새 앱의 복원이 확인되기 전에는 삭제하지 않습니다.
